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
from services.bamawl_import_parser import BamawlTemplateError, validate_bamawl_template
from services.embedding_service import EmbeddingService
from services.excel_service import ExcelService
from services.kb_template_service import build_template_workbook as _build_template_workbook
from services.kb_upload_service import upload_and_embed_files
from utils.permissions import require_roles
from utils.team_storage import team_folders_for_team_id

logger = logging.getLogger(__name__)

_BAMAWL_TEAM_NAME = "Bamawl Team"

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


def _is_bamawl_team() -> bool:
    """Return whether the current session's team is Bamawl Team.

    Checked by name (not id/slug), matching how
    ``utils/migrations/bamawl_import_export_config.py`` looks Bamawl
    Team up — so this stays correct regardless of that team's id/slug.
    """
    from repositories.team_repository import TeamRepository

    team = TeamRepository(current_app.config["MHES_DB_PATH"]).get_by_id(session["team_id"])
    return team is not None and team["name"] == _BAMAWL_TEAM_NAME


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
    """Download a blank knowledge-file template with the expected columns.

    The columns match what ``services.excel_parser`` looks for
    (flexibly, by keyword), so a file filled in from this template is
    guaranteed to be parsed and embedded correctly on upload.
    """
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

    # Bamawl Team accepts only its own Excel template (see
    # services/bamawl_import_parser.py) — validated here, before saving,
    # so an invalid file is rejected outright with a clear message
    # instead of being saved and only failing embedding later with a
    # generic error. Every other team's upload flow is unaffected.
    if _is_bamawl_team() and column_mapping:
        accepted_files = []
        for file in files:
            if file.filename in (None, ""):
                continue
            try:
                validate_bamawl_template(file.stream, column_mapping)
                accepted_files.append(file)
            except BamawlTemplateError as e:
                logger.warning("Rejected '%s' for Bamawl Team: %s", file.filename, e)
                flash(f"Rejected '{file.filename}': {e}", "danger")
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

    if _is_bamawl_team() and column_mapping:
        try:
            validate_bamawl_template(kb_path, column_mapping)
        except BamawlTemplateError as e:
            logger.warning("Rejected re-embed of '%s' for Bamawl Team: %s", filename, e)
            flash(f"Re-embedding failed: {e}", "danger")
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
