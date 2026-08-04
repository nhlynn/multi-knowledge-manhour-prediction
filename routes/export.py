"""Export route blueprint for MHES.

Handles data export to Excel format matching the simple_resource template.

Generated workbooks are staged locally only long enough to be uploaded to
Google Cloud Storage (see services/gcs_service.py) — the local file is
deleted right after upload, and the bucket is the only persistent copy
from then on. Downloads are served via short-lived signed URLs instead of
streaming the file from local disk.

Export history rows created before this migration still have a local
absolute path in ``file_path`` — ``is_local_path`` (services.gcs_service)
tells those apart from the GCS object paths new rows use, so old records
keep working unchanged.
"""

import logging
import io
import os
from datetime import datetime

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
from services.bamawl_export_builder import BamawlExportError, build_bamawl_workbook
from services.export_detail_service import read_export_detail as _read_export_detail
from services.export_history_service import ExportHistoryService
from services.export_pipeline_service import (
    build_export_filename,
    sanitize_project_name_for_filename,
    upload_export_with_retry,
)
from services.export_workbook_service import DEFAULT_EXPORT_TEMPLATE
from services.export_workbook_service import build_workbook as _build_workbook
from services.gcs_service import (
    GCSError,
    download_excel_bytes,
    generate_signed_download_url,
    is_local_path,
    list_existing_export_object_paths,
)
from services.remark_html import sanitize_remark_html
from utils.pagination import parse_page_param, total_pages_for
from utils.permissions import require_login

logger = logging.getLogger(__name__)

export_bp = Blueprint("export", __name__)
# Any logged-in role (Member and above) can export results.
export_bp.before_request(require_login)

_BAMAWL_TEAM_NAME = "Bamawl Team"


def _team_id_filter() -> int | None:
    """Return the ``team_id`` to scope Export History reads to (Phase 6).

    None means "no filter" (see every team's exports) — Admin only.
    Every other role only ever sees their own team's exports.
    """
    if session.get("role") == "Admin":
        return None
    return session.get("team_id")


def _team_export_template() -> dict:
    """Return the current session's team's export template (Phase 8).

    Falls back to ``DEFAULT_EXPORT_TEMPLATE`` — which reproduces the
    exact pre-Phase-8 column layout — for any team with no configured
    template, so existing exports are completely unaffected.
    """
    from repositories.team_export_template_repository import TeamExportTemplateRepository

    repo = TeamExportTemplateRepository(current_app.config["MHES_DB_PATH"])
    config = repo.get_by_team_id(session["team_id"])
    return config["template_config"] if config else DEFAULT_EXPORT_TEMPLATE


def _is_bamawl_team() -> bool:
    """Return whether the current session's team is Bamawl Team.

    Checked by name, matching ``routes/upload.py``'s helper of the same
    name and how ``utils/migrations/bamawl_import_export_config.py``
    looks Bamawl Team up.
    """
    from repositories.team_repository import TeamRepository

    team = TeamRepository(current_app.config["MHES_DB_PATH"]).get_by_id(session["team_id"])
    return team is not None and team["name"] == _BAMAWL_TEAM_NAME


def _bamawl_import_column_mapping() -> dict | None:
    """Return Bamawl Team's configured import column mapping.

    The export builder reuses this (rather than a separate config) —
    it already describes exactly which worksheet/columns
    ``ALL_Detail``'s data lives in, the same mapping
    ``services/bamawl_import_parser.py`` reads it with.
    """
    from repositories.team_import_config_repository import TeamImportConfigRepository

    repo = TeamImportConfigRepository(current_app.config["MHES_DB_PATH"])
    config = repo.get_by_team_id(session["team_id"])
    return config["column_mapping"] if config else None


def _bamawl_template_path() -> str:
    """Path to Bamawl Team's real export template workbook."""
    return os.path.join(
        current_app.root_path, "simple_resource", "bamawl_import_export_format.xlsx",
    )


@export_bp.route("/excel", methods=["POST"])
def export_excel():
    """Export preview data to an Excel file, stored in Google Cloud Storage.

    Expects JSON body with ``projectName`` and ``categories``. Builds the
    workbook to a temporary local file, uploads it to GCS, deletes the
    temp file, records the export in the history database, and returns
    the file as a download (served from the in-memory bytes already read
    for the upload, so no second disk read is needed).
    """
    data = request.get_json(silent=True) or {}
    project_name = (data.get("projectName") or "").strip()
    created_by = (data.get("createdBy") or "").strip()
    project_remark = sanitize_remark_html(data.get("projectRemark") or "")
    categories = data.get("categories", [])

    if not project_name:
        return jsonify({"error": "Project name is required."}), 400

    if not created_by:
        return jsonify({"error": "Created By is required."}), 400

    if not isinstance(categories, list) or not categories:
        return jsonify({"error": "No data to export."}), 400

    safe_name = sanitize_project_name_for_filename(project_name)
    temp_dir = _export_folder()
    os.makedirs(temp_dir, exist_ok=True)
    build_path = os.path.join(temp_dir, build_export_filename(safe_name))

    bamawl_mapping = _bamawl_import_column_mapping() if _is_bamawl_team() else None

    try:
        if bamawl_mapping:
            # Bamawl Team exports onto its own real template workbook
            # (see services/bamawl_export_builder.py) instead of the
            # generic from-scratch column-layout builder every other
            # team uses.
            build_bamawl_workbook(
                build_path, categories, bamawl_mapping, _bamawl_template_path(),
            )
        else:
            _build_workbook(
                build_path, project_name, created_by, project_remark, categories,
                template_config=_team_export_template(),
            )
        with open(build_path, "rb") as f:
            file_bytes = f.read()
    except BamawlExportError as e:
        # A specific, user-actionable problem (e.g. too many tasks for
        # the template's fixed row block) -- surfaced directly rather
        # than the generic 500 message below.
        logger.warning("Bamawl export rejected for project=%r: %s", project_name, e)
        return jsonify({"error": str(e)}), 400
    except Exception:
        # Logged with full traceback for diagnosis; the client only ever
        # gets a generic message — the underlying exception could
        # otherwise surface internal details (file paths, library
        # internals) that have no business reaching the browser.
        logger.exception("Failed to build export workbook for project=%r.", project_name)
        return jsonify({"error": "Failed to generate the export file. Please try again."}), 500
    finally:
        try:
            os.remove(build_path)
        except OSError:
            logger.warning("Could not remove temporary export file: %s", build_path)

    try:
        filename, object_path = upload_export_with_retry(temp_dir, safe_name, file_bytes)
    except GCSError:
        logger.exception("Failed to upload export file to GCS for project=%r.", project_name)
        return jsonify({"error": "Failed to save the export file to cloud storage. Please try again."}), 502

    logger.info(
        "Export succeeded: file=%s project_name=%r gcs_path=%s",
        filename, project_name, object_path,
    )
    _export_history_service().record_export_result(
        categories=categories,
        file_name=filename,
        file_url=url_for("export.download_export", filename=filename),
        file_path=object_path,
        file_size=len(file_bytes),
        project_name=project_name,
        created_by=created_by,
        team_id=session["team_id"],
        created_by_user_id=session.get("user_id"),
    )

    return send_file(
        io.BytesIO(file_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _export_folder() -> str:
    """Local scratch directory used only to stage a workbook before it's
    uploaded to GCS — files here are temporary and deleted right after
    upload (see export_excel). Not where exports are persisted anymore.
    """
    return os.path.join(current_app.root_path, "exports")


def _export_history_service() -> ExportHistoryService:
    return ExportHistoryService(db_path=current_app.config["MHES_DB_PATH"])


EXPORTS_PER_PAGE = 10


@export_bp.route("/files", methods=["GET"])
def list_exports() -> str:
    """Render the Export History page from SQLite metadata (no folder scan).

    Supports server-side pagination (``page``) combined with the From
    Date / To Date / Project Name filters, so only one page of records is
    ever loaded from the database per request.
    """
    # On a fresh visit (no query string at all) default both date fields to
    # today, so the page opens already scoped to "today's exports" instead
    # of loading the entire history. Once the user has interacted with the
    # filter form — including clearing the dates via Reset, which passes
    # explicit empty values — the field is present in the query string
    # (even if empty), so the default is not re-applied and their choice
    # (including "no date filter") is respected.
    today_str = datetime.now().strftime("%Y-%m-%d")
    from_date = (request.args.get("from_date", today_str) or "").strip()
    to_date = (request.args.get("to_date", today_str) or "").strip()
    project_name = (request.args.get("project_name") or "").strip()
    page = parse_page_param(request.args.get("page"))

    service = _export_history_service()
    team_filter = _team_id_filter()
    try:
        history, total = service.get_history_page(
            page=page,
            per_page=EXPORTS_PER_PAGE,
            team_id=team_filter,
            from_date=from_date or None,
            to_date=to_date or None,
            project_name=project_name or None,
        )
        total_pages = total_pages_for(total, EXPORTS_PER_PAGE)
        if page > total_pages:
            # Requested page is past the last one (e.g. stale bookmark
            # after records were deleted) — re-fetch the actual last page
            # instead of showing an empty table under a wrong page number.
            page = total_pages
            history, total = service.get_history_page(
                page=page,
                per_page=EXPORTS_PER_PAGE,
                team_id=team_filter,
                from_date=from_date or None,
                to_date=to_date or None,
                project_name=project_name or None,
            )
    except Exception:
        logger.exception("Failed to load export history from database.")
        history, total, total_pages = [], 0, 1

    # Admin sees exports across every team — enrich each row with the
    # owning team's name so that's visible in the list (team_filter is
    # None only for Admin; see _team_id_filter).
    team_names_by_id = {}
    if team_filter is None and history:
        from repositories.team_repository import TeamRepository

        team_names_by_id = {
            t["id"]: t["name"]
            for t in TeamRepository(current_app.config["MHES_DB_PATH"]).list_all()
        }

    export_folder = _export_folder()
    # One GCS listing for the whole page instead of one blob_exists()
    # network round trip per GCS-path row below — only fetched if this
    # page actually has any GCS-path rows to check.
    existing_gcs_paths = None
    if any(r.get("file_path") and not is_local_path(r["file_path"]) for r in history):
        existing_gcs_paths = list_existing_export_object_paths()

    for record in history:
        if team_filter is None:
            record["team_name"] = team_names_by_id.get(record.get("team_id"), "Unknown")
        file_path = record.get("file_path")
        if file_path and is_local_path(file_path):
            # Pre-migration row — still a real local export, check disk directly.
            record["file_exists"] = os.path.isfile(file_path)
        elif file_path:
            # Post-migration row — file_path is a GCS object path.
            record["file_exists"] = file_path in existing_gcs_paths
        else:
            # Oldest rows, from before the file_path column existed at
            # all — fall back to reconstructing the (local) path.
            local_path = os.path.join(export_folder, record["file_name"])
            record["file_exists"] = os.path.isfile(local_path)
        record["size_kb"] = round(record["file_size"] / 1024, 1) if record.get("file_size") else 0
        if not record["file_exists"]:
            logger.warning(
                "Export history file missing: %s (history id=%s)",
                record["file_name"], record["id"],
            )

    range_start = (page - 1) * EXPORTS_PER_PAGE + 1 if total else 0
    range_end = min(page * EXPORTS_PER_PAGE, total)

    filter_args = {}
    if from_date:
        filter_args["from_date"] = from_date
    if to_date:
        filter_args["to_date"] = to_date
    if project_name:
        filter_args["project_name"] = project_name

    return render_template(
        "exported_files.html",
        files=history,
        page=page,
        total_pages=total_pages,
        total=total,
        range_start=range_start,
        range_end=range_end,
        from_date=from_date,
        to_date=to_date,
        project_name=project_name,
        filter_args=filter_args,
        has_filters=bool(filter_args),
    )


def _is_safe_export_filename(filename: str) -> bool:
    """Reject path traversal / directory separators without mangling the
    filename itself (unlike ``werkzeug.secure_filename``, which replaces
    spaces with underscores and would no longer match real export
    filenames — these are generated by this app, e.g. "Project 1_manhour.xlsx",
    not arbitrary user-uploaded names, so we only need to guard against
    escaping the exports folder, not normalize the name).
    """
    return (
        bool(filename)
        and filename.lower().endswith(".xlsx")
        and os.path.basename(filename) == filename
        and ".." not in filename
    )


@export_bp.route("/files/<filename>", methods=["GET"])
def download_export(filename: str):
    """Download a previously exported Excel file.

    Looks up where the file actually lives via its export_history row: a
    GCS object path for exports created after the storage migration (in
    which case the browser is redirected to a short-lived signed URL, so
    the file is streamed directly from GCS rather than through this
    server), or a local absolute path for older, pre-migration exports
    (served directly from disk, as before).
    """
    if not _is_safe_export_filename(filename):
        logger.warning("Rejected unsafe export filename for download: %r", filename)
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("export.list_exports"))

    record = _export_history_service().get_history_by_file_name(filename, team_id=_team_id_filter())
    if record is None:
        # Either the file truly doesn't exist, or it exists but belongs
        # to another team (Phase 6) — treated identically as "not found".
        # No local-path-reconstruction fallback here: guessing a path
        # for an unscoped record could otherwise leak a legacy local
        # export across teams.
        logger.warning("Export history record not found or not owned by caller's team: %s", filename)
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("export.list_exports"))

    # Rows created before the file_path column existed at all have a
    # legitimate record but no file_path — reconstruct the (local) path
    # the same way list_exports does for those.
    file_path = record.get("file_path") or os.path.join(_export_folder(), filename)

    if is_local_path(file_path):
        if not os.path.isfile(file_path):
            logger.warning("Export file missing on disk for download: %s", filename)
            flash(f"File not found: {filename}", "warning")
            return redirect(url_for("export.list_exports"))
        try:
            return send_file(file_path, as_attachment=True, download_name=filename)
        except Exception:
            logger.exception("Failed to send export file for download: %s", filename)
            flash(f"Could not download '{filename}'.", "danger")
            return redirect(url_for("export.list_exports"))

    try:
        signed_url = generate_signed_download_url(file_path, download_name=filename)
    except GCSError:
        logger.exception("Failed to generate signed download URL for: %s", filename)
        flash(f"Could not download '{filename}'.", "danger")
        return redirect(url_for("export.list_exports"))

    return redirect(signed_url)


@export_bp.route("/files/<filename>/view", methods=["GET"])
def view_export(filename: str) -> str:
    """Render a read-only in-browser detail view of an exported file.

    Reads the workbook from GCS (post-migration exports) or local disk
    (pre-migration exports), based on the export_history row's file_path —
    same local-vs-GCS distinction as ``download_export``.
    """
    if not _is_safe_export_filename(filename):
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("export.list_exports"))

    record = _export_history_service().get_history_by_file_name(filename, team_id=_team_id_filter())
    if record is None:
        # Either the file truly doesn't exist, or it belongs to another
        # team (Phase 6) — treated identically as "not found".
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("export.list_exports"))

    file_path = record.get("file_path") or os.path.join(_export_folder(), filename)

    try:
        if is_local_path(file_path):
            if not os.path.isfile(file_path):
                flash(f"File not found: {filename}", "warning")
                return redirect(url_for("export.list_exports"))
            detail = _read_export_detail(file_path)
        else:
            file_bytes = download_excel_bytes(file_path)
            detail = _read_export_detail(io.BytesIO(file_bytes))
    except GCSError:
        logger.exception("Failed to download export file from GCS for '%s'.", filename)
        flash(f"Could not open '{filename}' for viewing. Please try again.", "danger")
        return redirect(url_for("export.list_exports"))
    except Exception:
        logger.exception("Failed to read export detail for '%s'.", filename)
        flash(f"Could not open '{filename}' for viewing. Please try again.", "danger")
        return redirect(url_for("export.list_exports"))

    return render_template("export_detail.html", filename=filename, **detail)

