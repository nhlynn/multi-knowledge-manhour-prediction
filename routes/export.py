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
from services.bamawl_export_builder import BamawlExportBuilder
from services.base_export_service import ExportContext
from services.export_history_service import ExportHistoryService
from services.export_pipeline_service import (
    build_export_filename,
    sanitize_project_name_for_filename,
    upload_export_with_retry,
)
from services.export_strategies import (
    BamawlExportError,
    DefaultExportStrategy,
    KikanExportError,
    SglExportError,
    get_export_strategy_class,
)
from services.kikan_export_builder import KikanExportBuilder
from services.sgl_export_builder import SglExportBuilder
from services.export_workbook_service import DEFAULT_EXPORT_TEMPLATE
from services.gcs_service import (
    GCSError,
    download_excel_bytes,
    generate_signed_download_url,
    is_local_path,
    list_existing_export_object_paths,
)
from utils.pagination import parse_page_param, total_pages_for
from utils.permissions import require_login

logger = logging.getLogger(__name__)

export_bp = Blueprint("export", __name__)
# Any logged-in role can export results.
export_bp.before_request(require_login)


def _team_id_filter() -> int | None:
    """Return the ``team_id`` to scope Export History reads to (Phase 6).

    None means "no filter" (see every team's exports) — Admin only.
    Every other role only ever sees their own team's exports.
    """
    if session.get("role") == "Admin":
        return None
    return session.get("team_id")


def _team_id_list_filter() -> int | None:
    """Return the effective ``team_id`` filter for the Export History
    LIST page specifically, honoring an Admin's optional Team dropdown
    selection (``?team_id=``) on top of ``_team_id_filter``'s base rule.

    Mirrors ``routes/preview.py``'s identical ``_team_id_list_filter``
    for Temporary Data: a Team Manager's ``team_id`` in the query
    string, if any, is always ignored — their filter stays locked to
    their own team regardless of what's in the URL. Only when
    ``_team_id_filter`` already returned None (Admin) does a
    query-string ``team_id`` take effect, narrowing "every team" down
    to one specific team; an absent or unparseable value leaves it at
    "every team".
    """
    base = _team_id_filter()
    if base is not None:
        return base
    raw = (request.args.get("team_id") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return None


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


def _current_team_name() -> str | None:
    """Return the current session's team's name, or None if it can't be resolved."""
    from repositories.team_repository import TeamRepository

    team = TeamRepository(current_app.config["MHES_DB_PATH"]).get_by_id(session["team_id"])
    return team["name"] if team is not None else None


def _select_export_strategy(
    build_path: str, project_name: str, created_by: str, categories: list,
) -> tuple:
    """Select this export's Strategy Pattern object and build its
    ``ExportContext`` (see ``services/base_export_service.py``,
    ``services/export_strategies.py``).

    A team's own dedicated strategy (looked up by name via
    ``get_export_strategy_class`` -- Bamawl Team, KiKan Team, SGL Team
    today) only actually applies once that team's own config is
    confirmed present (``bamawl_mapping``/``kikan_mapping``, each
    resolved via that strategy class's own ``resolve_column_mapping``,
    non-empty) -- a team named "Bamawl Team"/"KiKan Team" whose config
    hasn't been seeded yet still falls back to ``DefaultExportStrategy``,
    the same safety net this dispatch had before the Strategy Pattern
    refactor. SGL Team is the exception: its export doesn't use a
    DB-configured ``column_mapping`` at all (see
    ``SglExportBuilder``'s own docstring), so it's dispatched
    unconditionally, with no config-presence gate. This route has no
    team-specific config-resolution logic of its own left -- every
    team-specific detail lives in that team's own ``BaseExportService``
    subclass (``BamawlExportBuilder``, ``KikanExportBuilder``,
    ``SglExportBuilder``).
    """
    strategy_cls = get_export_strategy_class(_current_team_name())

    if strategy_cls is BamawlExportBuilder:
        bamawl_mapping = BamawlExportBuilder.resolve_column_mapping(
            current_app.config["MHES_DB_PATH"], session["team_id"],
        )
        if bamawl_mapping:
            return BamawlExportBuilder(), ExportContext(
                filepath=build_path, categories=categories, project_name=project_name,
                created_by=created_by,
                column_mapping=bamawl_mapping,
                template_path=BamawlExportBuilder.template_path(current_app.root_path),
            )
    elif strategy_cls is KikanExportBuilder:
        kikan_mapping = KikanExportBuilder.resolve_column_mapping(
            current_app.config["MHES_DB_PATH"], session["team_id"],
        )
        if kikan_mapping:
            return KikanExportBuilder(), ExportContext(
                filepath=build_path, categories=categories, project_name=project_name,
                created_by=created_by,
                column_mapping=kikan_mapping,
                template_path=KikanExportBuilder.template_path(current_app.root_path),
            )
    elif strategy_cls is SglExportBuilder:
        return SglExportBuilder(), ExportContext(
            filepath=build_path, categories=categories, project_name=project_name,
            created_by=created_by,
            template_path=SglExportBuilder.template_path(current_app.root_path),
        )

    return DefaultExportStrategy(), ExportContext(
        filepath=build_path, categories=categories, project_name=project_name,
        created_by=created_by,
        template_config=_team_export_template(),
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

    strategy, context = _select_export_strategy(build_path, project_name, created_by, categories)

    try:
        strategy.build(context)
        with open(build_path, "rb") as f:
            file_bytes = f.read()
    except BamawlExportError as e:
        # A specific, user-actionable problem (e.g. too many tasks for
        # the template's fixed row block) -- surfaced directly rather
        # than the generic 500 message below.
        logger.warning("Bamawl export rejected for project=%r: %s", project_name, e)
        return jsonify({"error": str(e)}), 400
    except KikanExportError as e:
        # Same reasoning as BamawlExportError above, independently for
        # KiKan Team's own export template.
        logger.warning("KiKan export rejected for project=%r: %s", project_name, e)
        return jsonify({"error": str(e)}), 400
    except SglExportError as e:
        # Same reasoning as BamawlExportError above, independently for
        # SGL Team's own export template.
        logger.warning("SGL export rejected for project=%r: %s", project_name, e)
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
    team_filter = _team_id_list_filter()
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
    # owning team's name so that's visible in the list, regardless of
    # whether they've also narrowed team_filter down to one specific
    # team via the Team dropdown (team_filter is only None for Admin
    # with no team chosen; role is the right check here, not that).
    team_names_by_id = {}
    is_admin = session.get("role") == "Admin"
    if is_admin and history:
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
        if is_admin:
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

    team_id_param = (request.args.get("team_id") or "").strip() if is_admin else ""

    filter_args = {}
    if from_date:
        filter_args["from_date"] = from_date
    if to_date:
        filter_args["to_date"] = to_date
    if project_name:
        filter_args["project_name"] = project_name
    if team_id_param:
        filter_args["team_id"] = team_id_param

    teams = None
    if is_admin:
        from repositories.team_repository import TeamRepository

        teams = TeamRepository(current_app.config["MHES_DB_PATH"]).list_all()

    return render_template(
        "exported_files.html",
        files=history,
        teams=teams,
        team_id=team_id_param,
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


def _resolve_export_file_or_404(filename: str):
    """Look up an export's history record and confirm its file actually
    exists, for the two read-only "view" endpoints below.

    Returns:
        ``(record, file_path)`` on success, or ``(None, None)`` --
        callers translate that into whatever not-found response fits
        their route (a flashed redirect for the HTML page, a JSON 404
        for the raw-bytes endpoint).
    """
    if not _is_safe_export_filename(filename):
        return None, None
    record = _export_history_service().get_history_by_file_name(filename, team_id=_team_id_filter())
    if record is None:
        # Either the file truly doesn't exist, or it belongs to another
        # team (Phase 6) — treated identically as "not found".
        return None, None
    file_path = record.get("file_path") or os.path.join(_export_folder(), filename)
    if is_local_path(file_path) and not os.path.isfile(file_path):
        return None, None
    return record, file_path


@export_bp.route("/files/<filename>/view", methods=["GET"])
def view_export(filename: str) -> str:
    """Render the online Excel Preview page for an exported file.

    The page itself only needs the project name (for its header) and
    the filename -- the actual workbook is fetched and rendered
    entirely client-side (see ``export_file_raw`` below and
    ``templates/export_detail.html``'s script), so this route never
    reads the file's content, and nothing here can modify it.
    """
    record, _file_path = _resolve_export_file_or_404(filename)
    if record is None:
        flash(f"File not found: {filename}", "warning")
        return redirect(url_for("export.list_exports"))

    return render_template(
        "export_detail.html", filename=filename, project_name=record.get("project_name") or filename,
    )


@export_bp.route("/files/<filename>/raw", methods=["GET"])
def export_file_raw(filename: str):
    """Serve an exported workbook's raw bytes for the online Excel
    Preview to fetch and render client-side (see
    ``templates/export_detail.html``).

    Served ``as_attachment=False`` (inline) and only ever fetched via
    JavaScript (never a top-level browser navigation), so this never
    triggers a download/Save-As prompt — the workbook is displayed, not
    downloaded. Purely a read: nothing here writes back to the file,
    the export_history row, or anywhere else.
    """
    record, file_path = _resolve_export_file_or_404(filename)
    if record is None:
        return jsonify({"error": f"File not found: {filename}"}), 404

    try:
        if is_local_path(file_path):
            with open(file_path, "rb") as f:
                file_bytes = f.read()
        else:
            file_bytes = download_excel_bytes(file_path)
    except GCSError:
        logger.exception("Failed to download export file from GCS for preview: %s", filename)
        return jsonify({"error": "Could not load the file for preview. Please try again."}), 502
    except Exception:
        logger.exception("Failed to read export file for preview: %s", filename)
        return jsonify({"error": "Could not load the file for preview. Please try again."}), 500

    return send_file(
        io.BytesIO(file_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=False,
        download_name=filename,
    )