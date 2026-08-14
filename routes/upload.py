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

def _effective_team_id() -> int | None:
    """Return the team_id whose knowledge base this request operates on.

    Team Manager: always locked to their own team (``session["team_id"]``)
    — never overridable by anything in the request, so a Team Manager
    can never touch another team's knowledge base by adding a team_id
    to the URL or form.

    Admin: has no "home" team for knowledge-base purposes, so must
    explicitly choose one via a ``team_id`` value in the request --
    checked via ``request.values`` (covers both a query string
    ``?team_id=`` on a GET/fetch URL and a form field on a POST), so
    every route here (page load, template download, duplicate check,
    template pre-validation, upload, delete, re-embed) reads the same
    selection the same way regardless of how each specific request
    happens to carry it. Returns None if Admin hasn't chosen a team
    yet -- every route below checks for that and prompts/redirects
    instead of touching a team-scoped folder or service with no team.
    """
    if session.get("role") != "Admin":
        return session.get("team_id")
    raw = (request.values.get("team_id") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return None


def _team_folders(team_id: int) -> tuple[str, str, str]:
    """Resolve (kb_folder, embeddings_folder, team_slug) for ``team_id``."""
    return team_folders_for_team_id(
        current_app.config["TEAMS_FOLDER"],
        current_app.config["MHES_DB_PATH"],
        team_id,
    )


def _excel_service(team_id: int) -> ExcelService:
    kb_folder, _, _ = _team_folders(team_id)
    return ExcelService(kb_folder=kb_folder)


def _embedding_service(team_id: int) -> EmbeddingService:
    _, embeddings_folder, team_slug = _team_folders(team_id)
    return EmbeddingService(
        model_name=current_app.config["EMBEDDING_MODEL"],
        embeddings_folder=embeddings_folder,
        team_slug=team_slug,
    )


def _team_column_mapping(team_id: int) -> dict[str, str] | None:
    """Return ``team_id``'s configured Excel column mapping (Phase 7),
    or None if that team has no import configuration — in which case
    ``excel_parser`` falls back to generic keyword matching, unchanged
    from before Phase 7.
    """
    from repositories.team_import_config_repository import TeamImportConfigRepository

    repo = TeamImportConfigRepository(current_app.config["MHES_DB_PATH"])
    config = repo.get_by_team_id(team_id)
    return config["column_mapping"] if config else None


def _current_team_name(team_id: int) -> str | None:
    """Return ``team_id``'s team name, or None if it can't be resolved."""
    from repositories.team_repository import TeamRepository

    team = TeamRepository(current_app.config["MHES_DB_PATH"]).get_by_id(team_id)
    return team["name"] if team is not None else None


def _current_team_template_spec(team_id: int):
    """Return ``team_id``'s registered ``TeamTemplateSpec`` (see
    ``services/team_template_registry.py``), or None if that team has
    no strictly-validated template of its own -- in which case its
    upload keeps using the existing generic, lenient, keyword-based
    column matching, unaffected by any of this.

    This is the single point where "resolve team_id to a name, then
    look up the template assigned to it" happens — every other
    function here just uses whatever this returns.
    """
    team_name = _current_team_name(team_id)
    return get_team_template_spec(team_name) if team_name else None


def _all_teams_kb_files(teams: list) -> list[dict]:
    """Return every team's knowledge files combined into one list, each
    tagged with its own ``team_id``/``team_name`` -- for Admin's
    upload page before a specific team is chosen (see ``upload_page``).

    One ``ExcelService``/``EmbeddingService`` pair per team, same as a
    normal single-team page load, just looped -- a team whose KB
    folder fails to read (e.g. permissions, corrupt index) is skipped
    with a warning rather than failing the whole aggregate listing.
    Sorted newest-first by ``uploaded_at`` across all teams combined,
    matching a single team's own list ordering.
    """
    combined: list[dict] = []
    for team in teams:
        try:
            svc = _excel_service(team["id"])
            emb = _embedding_service(team["id"])
            files = svc.list_knowledge_files()
            emb.annotate_files_with_embedding_status(files)
        except Exception:
            logger.exception(
                "Failed to list knowledge files for team_id=%s (%s) in the "
                "all-teams aggregate view.", team["id"], team.get("name"),
            )
            continue
        for f in files:
            f["team_id"] = team["id"]
            f["team_name"] = team["name"]
        combined.extend(files)
    combined.sort(key=lambda f: f.get("uploaded_at") or "", reverse=True)
    return combined


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
    """Render the upload page with the list of imported files.

    Admin must choose a team via the page's own Team dropdown before
    uploading, downloading a template, or deleting/re-embedding a file
    -- see ``_effective_team_id``. Without one selected, the page still
    shows every team's knowledge files together (each row tagged with
    its own team, via ``team_id``/``team_name`` -- see
    ``_all_teams_kb_files``), just with the Upload form and Download
    Template button hidden, since adding a NEW file still needs a
    specific team's folder to save it into.
    """
    is_admin = session.get("role") == "Admin"
    team_id = _effective_team_id()

    teams = None
    if is_admin:
        from repositories.team_repository import TeamRepository

        teams = TeamRepository(current_app.config["MHES_DB_PATH"]).list_all()

    if is_admin and team_id is None:
        kb_files = _all_teams_kb_files(teams)
        return render_template(
            "upload.html", kb_files=kb_files, teams=teams, selected_team_id=None, no_team_selected=True,
        )

    svc = _excel_service(team_id)
    emb = _embedding_service(team_id)
    kb_files = svc.list_knowledge_files()
    emb.annotate_files_with_embedding_status(kb_files)
    for f in kb_files:
        f["team_id"] = team_id
    return render_template(
        "upload.html", kb_files=kb_files, teams=teams, selected_team_id=team_id, no_team_selected=False,
    )


@upload_bp.route("/template", methods=["GET"])
def download_template():
    """Download ``team_id``'s knowledge-file template.

    A team with its own registered ``TeamTemplateSpec`` (see
    ``services/team_template_registry.py``) that also has a sample
    path configured downloads that instead of the generic template --
    e.g. Bamawl Team gets its own real-structure, sanitized sample
    workbook (``import/bamawl/bamawl_import_template.xlsx``), never the
    internal template MHES actually validates/builds against
    server-side. Every other team keeps the existing generic behavior
    below, unaffected.
    """
    team_id = _effective_team_id()
    if team_id is None:
        flash("Please select a team before downloading a template.", "warning")
        return redirect(url_for("upload.upload_page"))

    dedicated_path = _team_sample_template_path(_current_team_template_spec(team_id))
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

    Expects a JSON body: ``{"filenames": ["a.xlsx", "b.xlsx"]}``, and
    (for Admin) a ``?team_id=`` query string value -- see
    ``_effective_team_id``.

    Returns:
        JSON with ``{"duplicates": ["a.xlsx"]}`` (only the ones that exist).
    """
    team_id = _effective_team_id()
    if team_id is None:
        return jsonify({"error": "No team selected."}), 400

    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames", [])

    if not isinstance(filenames, list) or not all(isinstance(n, str) for n in filenames):
        return jsonify({"error": "'filenames' must be a list of strings."}), 400

    duplicates = _excel_service(team_id).find_existing_filenames(filenames)
    return jsonify({"duplicates": duplicates})


@upload_bp.route("/validate-template", methods=["POST"])
def validate_template():
    """Structurally validate one file against ``team_id``'s registered
    template, without saving or embedding anything.

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

    Expects a single-file multipart form body: ``files``, plus (for
    Admin) a ``team_id`` field -- see ``_effective_team_id``.

    Returns:
        JSON ``{"valid": true}``, or ``{"valid": false, "message": ...,
        "reason": ...}`` with the same user-facing wording
        ``upload_files``'s rejection flash uses.
    """
    team_id = _effective_team_id()
    if team_id is None:
        return jsonify({"valid": True})

    spec = _current_team_template_spec(team_id)
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
        ``team_id``: Required for Admin -- see ``_effective_team_id``.

    After each successful save the embedding service is called automatically.
    """
    team_id = _effective_team_id()
    if team_id is None:
        flash("Please select a team before uploading.", "warning")
        return redirect(url_for("upload.upload_page"))

    files = request.files.getlist("files")
    duplicate_action = request.form.get("duplicate_action", "rename")

    if not files or all(f.filename in (None, "") for f in files):
        flash("No files selected.", "warning")
        return redirect(url_for("upload.upload_page", team_id=team_id))

    column_mapping = _team_column_mapping(team_id)

    # A team with its own registered TeamTemplateSpec (see
    # services/team_template_registry.py) accepts only its own official
    # Excel template — validated here, before saving, so an invalid
    # file is rejected outright with a clear message instead of being
    # saved and only failing embedding later with a generic error.
    # Every team without a registered spec is completely unaffected.
    spec = _current_team_template_spec(team_id)
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
            return redirect(url_for("upload.upload_page", team_id=team_id))

    result = upload_and_embed_files(
        files,
        duplicate_action=duplicate_action,
        excel_service=_excel_service(team_id),
        embedding_service=_embedding_service(team_id),
        column_mapping=column_mapping,
        team_name=_current_team_name(team_id),
    )
    for message in result.messages:
        flash(message.text, message.category)

    return redirect(url_for("upload.upload_page", team_id=team_id))


@upload_bp.route("/delete/<filename>", methods=["POST"])
def delete_file(filename: str) -> str:
    """Delete a knowledge base file and its embeddings.

    Args:
        filename: Name of the file to remove.

    Form fields:
        ``team_id``: Required for Admin -- see ``_effective_team_id``.
    """
    team_id = _effective_team_id()
    if team_id is None:
        flash("Please select a team first.", "warning")
        return redirect(url_for("upload.upload_page"))

    if not ExcelService.is_safe_filename(filename):
        logger.warning("Rejected unsafe filename for delete: %r", filename)
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("upload.upload_page", team_id=team_id))

    svc = _excel_service(team_id)
    emb = _embedding_service(team_id)

    try:
        emb.delete_index(filename)
        if svc.delete_file(filename):
            flash(f"Deleted: {filename}", "success")
        else:
            flash(f"File not found: {filename}", "warning")
    except Exception as e:
        logger.error("Delete failed for '%s': %s", filename, e)
        flash(f"Delete failed: {e}", "danger")

    return redirect(url_for("upload.upload_page", team_id=team_id))


@upload_bp.route("/reembed/<filename>", methods=["POST"])
def reembed_file(filename: str) -> str:
    """Re-generate embeddings for an existing knowledge base file.

    Args:
        filename: Name of the Excel file in kb_knowledge.

    Form fields:
        ``team_id``: Required for Admin -- see ``_effective_team_id``.
    """
    team_id = _effective_team_id()
    if team_id is None:
        flash("Please select a team first.", "warning")
        return redirect(url_for("upload.upload_page"))

    if not ExcelService.is_safe_filename(filename):
        logger.warning("Rejected unsafe filename for re-embed: %r", filename)
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("upload.upload_page", team_id=team_id))

    emb = _embedding_service(team_id)
    svc = _excel_service(team_id)
    kb_path = svc.get_kb_path(filename)
    column_mapping = _team_column_mapping(team_id)

    spec = _current_team_template_spec(team_id)
    if spec:
        try:
            validate_team_template(kb_path, spec)
        except TeamTemplateError as e:
            logger.warning("Rejected re-embed of '%s' for %s: %s", filename, spec.team_name, e)
            flash(_team_template_error_flash_message(spec, e), "danger")
            return redirect(url_for("upload.upload_page", team_id=team_id))

    try:
        result = emb.process_excel_file(
            kb_path, column_mapping=column_mapping, team_name=_current_team_name(team_id),
        )
        flash(
            f"Embeddings regenerated for '{filename}': "
            f"{result['num_vectors']} vectors.",
            "info",
        )
    except Exception as e:
        logger.error("Re-embedding failed for '%s': %s", filename, e)
        flash(f"Re-embedding failed: {e}", "danger")

    return redirect(url_for("upload.upload_page", team_id=team_id))