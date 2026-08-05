"""Upload route blueprint for MHES.

Handles Excel file upload, duplicate detection, and knowledge base management.
Routes are kept thin — all logic lives in the service layer.
"""

import logging
import os

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from services.embedding_service import EmbeddingService
from services.excel_service import ExcelService
from services.kb_template_service import build_template_workbook as _build_template_workbook
from services.kb_upload_service import upload_and_embed_files
from services.team_template_registry import get_team_template_spec
from services.team_template_validator import TeamTemplateError, validate_team_template
from utils.permissions import require_roles
from utils.team_storage import team_folders_for_team_id

logger = logging.getLogger(__name__)

upload_bp = Blueprint("upload", __name__)
# Knowledge Base management is an Admin / Team Manager capability (see
# docs/ARCHITECTURE.md §5d) — every route in this blueprint requires it.
upload_bp.before_request(require_roles("Admin", "Team Manager"))


# ------------------------------------------------------------------
# Service helpers
# ------------------------------------------------------------------

def _team_folders() -> tuple[str, str, str]:
    """Resolve (kb_folder, embeddings_folder, team_slug) for the current session's team."""
    return team_folders_for_team_id(
        current_app.config["TEAMS_FOLDER"],
        current_app.config["MHES_DB_PATH"],
        session["team_id"],
    )


def _excel_service() -> ExcelService:
    kb_folder, _, _ = _team_folders()
    return ExcelService(kb_folder=kb_folder)


def _embedding_service() -> EmbeddingService:
    _, embeddings_folder, team_slug = _team_folders()
    return EmbeddingService(
        model_name=current_app.config["EMBEDDING_MODEL"],
        embeddings_folder=embeddings_folder,
        team_slug=team_slug,
    )


def _team_column_mapping() -> dict[str, str] | None:
    """Return the current session's team's configured Excel column
    mapping (Phase 7), or None if that team has no import configuration
    — in which case ``excel_parser`` falls back to generic keyword
    matching, unchanged from before Phase 7.
    """
    from repositories.team_import_config_repository import TeamImportConfigRepository

    repo = TeamImportConfigRepository(current_app.config["MHES_DB_PATH"])
    config = repo.get_by_team_id(session["team_id"])
    return config["column_mapping"] if config else None


def _current_team_name() -> str | None:
    """Return the current session's team's name, or None if it can't be resolved."""
    from repositories.team_repository import TeamRepository

    team = TeamRepository(current_app.config["MHES_DB_PATH"]).get_by_id(session["team_id"])
    return team["name"] if team is not None else None


def _current_team_template_spec():
    """Return the current session's team's registered
    ``TeamTemplateSpec`` (see ``services/team_template_registry.py``),
    or None if that team has no strictly-validated template of its own
    -- in which case its upload keeps using the existing generic,
    lenient, keyword-based column matching, unaffected by any of this.

    This is the single point where "detect the current user's team,
    then look up the template assigned to it" happens — every other
    function here just uses whatever this returns.
    """
    team_name = _current_team_name()
    return get_team_template_spec(team_name) if team_name else None


def _invalid_template_message(spec) -> str:
    return f"Invalid import template. Please download and use the latest {spec.team_name} import template."


def _team_template_error_flash_message(spec, error: TeamTemplateError) -> str:
    """Build the user-facing flash text for a rejected upload: the
    generic friendly message, plus the short reason category if one is
    available (e.g. "Missing worksheet: ALL_Detail", "Invalid column
    order") -- never the exception's full technical detail (worksheet
    lists, exact row/column positions, etc.), which is logged
    separately instead.
    """
    base = _invalid_template_message(spec)
    return f"{base} ({error.reason})" if error.reason else base


def _team_sample_template_path(spec) -> str | None:
    """Return ``spec``'s sample template's absolute path, or None if
    that team has no dedicated sample -- in which case the generic
    ``download_template`` fallback applies.
    """
    if spec is None or not spec.sample_template_path:
        return None
    return os.path.join(current_app.root_path, *spec.sample_template_path)


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@upload_bp.route("/", methods=["GET"])
def upload_page() -> str:
    """Render the upload page with the list of imported files."""
    svc = _excel_service()
    emb = _embedding_service()
    kb_files = svc.list_knowledge_files()
    emb.annotate_files_with_embedding_status(kb_files)
    return render_template("upload.html", kb_files=kb_files)


@upload_bp.route("/template", methods=["GET"])
def download_template():
    """Download the current session's team's knowledge-file template.

    A team with its own registered ``TeamTemplateSpec`` (see
    ``services/team_template_registry.py``) that also has a sample
    path configured downloads that instead of the generic template --
    e.g. Bamawl Team gets its own real-structure, sanitized sample
    workbook (``import/bamawl/bamawl_import_template.xlsx``), never the
    internal template MHES actually validates/builds against
    server-side. Every other team keeps the existing generic behavior
    below, unaffected.
    """
    dedicated_path = _team_sample_template_path(_current_team_template_spec())
    if dedicated_path:
        return send_file(
            dedicated_path, as_attachment=True, download_name=os.path.basename(dedicated_path),
        )

    # Generic fallback: columns match what ``services.excel_parser``
    # looks for (flexibly, by keyword), so a file filled in from this
    # template is guaranteed to be parsed and embedded correctly on upload.
    filename = "MHES_KB_Template.xlsx"
    filepath = os.path.join(current_app.instance_path, filename)
    os.makedirs(current_app.instance_path, exist_ok=True)

    _build_template_workbook(filepath)

    return send_file(filepath, as_attachment=True, download_name=filename)


@upload_bp.route("/check-duplicates", methods=["POST"])
def check_duplicates() -> tuple:
    """Check which of the selected filenames already exist in kb_knowledge.

    Expects a JSON body: ``{"filenames": ["a.xlsx", "b.xlsx"]}``.

    Returns:
        JSON with ``{"duplicates": ["a.xlsx"]}`` (only the ones that exist).
    """
    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames", [])

    if not isinstance(filenames, list) or not all(isinstance(n, str) for n in filenames):
        return jsonify({"error": "'filenames' must be a list of strings."}), 400

    duplicates = _excel_service().find_existing_filenames(filenames)
    return jsonify({"duplicates": duplicates})


@upload_bp.route("/validate-template", methods=["POST"])
def validate_template():
    """Structurally validate one file against the current session's
    team's registered template, without saving or embedding anything.

    Called client-side right after a file is selected (before the user
    even clicks Upload), so an invalid file can be flagged immediately
    with a specific reason and a "Download Latest Template" link — all
    without a page reload, and without the file ever reaching disk.
    ``upload_files`` below still re-validates at submit time as a
    server-side backstop; this endpoint only improves the UX of that
    same check.

    A team with no registered ``TeamTemplateSpec`` (every team except
    Bamawl today) always gets ``{"valid": true}`` immediately — this
    endpoint has no effect on their upload flow.

    Expects a single-file multipart form body: ``files``.

    Returns:
        JSON ``{"valid": true}``, or ``{"valid": false, "message": ...,
        "reason": ...}`` with the same user-facing wording
        ``upload_files``'s rejection flash uses.
    """
    spec = _current_team_template_spec()
    if spec is None:
        return jsonify({"valid": True})

    file = request.files.get("files")
    if file is None or file.filename in (None, ""):
        return jsonify({"valid": True})

    try:
        validate_team_template(file.stream, spec)
        return jsonify({"valid": True})
    except TeamTemplateError as e:
        logger.info("Pre-check rejected '%s' for %s: %s", file.filename, spec.team_name, e)
        return jsonify({
            "valid": False,
            "message": _invalid_template_message(spec),
            "reason": e.reason,
        })


@upload_bp.route("/", methods=["POST"])
def upload_files() -> str:
    """Handle one or multiple Excel file uploads.

    Form fields:
        ``files``: One or more file inputs.
        ``duplicate_action``: ``"rename"`` (default) or ``"overwrite"``.

    After each successful save the embedding service is called automatically.
    """
    files = request.files.getlist("files")
    duplicate_action = request.form.get("duplicate_action", "rename")

    if not files or all(f.filename in (None, "") for f in files):
        flash("No files selected.", "warning")
        return redirect(url_for("upload.upload_page"))

    column_mapping = _team_column_mapping()

    # A team with its own registered TeamTemplateSpec (see
    # services/team_template_registry.py) accepts only its own official
    # Excel template — validated here, before saving, so an invalid
    # file is rejected outright with a clear message instead of being
    # saved and only failing embedding later with a generic error.
    # Every team without a registered spec is completely unaffected.
    spec = _current_team_template_spec()
    if spec:
        accepted_files = []
        for file in files:
            if file.filename in (None, ""):
                continue
            try:
                validate_team_template(file.stream, spec)
                accepted_files.append(file)
            except TeamTemplateError as e:
                # Full technical detail (worksheet list, exact
                # row/column, etc.) goes to the log only -- the user
                # sees a generic, friendly message plus a short reason
                # category, never a raw exception string.
                logger.warning("Rejected '%s' for %s: %s", file.filename, spec.team_name, e)
                flash(
                    f"'{file.filename}': {_team_template_error_flash_message(spec, e)}", "danger",
                )
        files = accepted_files
        if not files:
            return redirect(url_for("upload.upload_page"))

    result = upload_and_embed_files(
        files,
        duplicate_action=duplicate_action,
        excel_service=_excel_service(),
        embedding_service=_embedding_service(),
        column_mapping=column_mapping,
    )
    for message in result.messages:
        flash(message.text, message.category)

    return redirect(url_for("upload.upload_page"))


@upload_bp.route("/delete/<filename>", methods=["POST"])
def delete_file(filename: str) -> str:
    """Delete a knowledge base file and its embeddings.

    Args:
        filename: Name of the file to remove.
    """
    if not ExcelService.is_safe_filename(filename):
        logger.warning("Rejected unsafe filename for delete: %r", filename)
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("upload.upload_page"))

    svc = _excel_service()
    emb = _embedding_service()

    try:
        emb.delete_index(filename)
        if svc.delete_file(filename):
            flash(f"Deleted: {filename}", "success")
        else:
            flash(f"File not found: {filename}", "warning")
    except Exception as e:
        logger.error("Delete failed for '%s': %s", filename, e)
        flash(f"Delete failed: {e}", "danger")

    return redirect(url_for("upload.upload_page"))


@upload_bp.route("/reembed/<filename>", methods=["POST"])
def reembed_file(filename: str) -> str:
    """Re-generate embeddings for an existing knowledge base file.

    Args:
        filename: Name of the Excel file in kb_knowledge.
    """
    if not ExcelService.is_safe_filename(filename):
        logger.warning("Rejected unsafe filename for re-embed: %r", filename)
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("upload.upload_page"))

    emb = _embedding_service()
    svc = _excel_service()
    kb_path = svc.get_kb_path(filename)
    column_mapping = _team_column_mapping()

    spec = _current_team_template_spec()
    if spec:
        try:
            validate_team_template(kb_path, spec)
        except TeamTemplateError as e:
            logger.warning("Rejected re-embed of '%s' for %s: %s", filename, spec.team_name, e)
            flash(_team_template_error_flash_message(spec, e), "danger")
            return redirect(url_for("upload.upload_page"))

    try:
        result = emb.process_excel_file(kb_path, column_mapping=column_mapping)
        flash(
            f"Embeddings regenerated for '{filename}': "
            f"{result['num_vectors']} vectors.",
            "info",
        )
    except Exception as e:
        logger.error("Re-embedding failed for '%s': %s", filename, e)
        flash(f"Re-embedding failed: {e}", "danger")

    return redirect(url_for("upload.upload_page"))
