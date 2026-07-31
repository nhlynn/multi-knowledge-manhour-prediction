"""Preview route blueprint for MHES.

Handles knowledge base data preview and browsing.
"""

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from scheduler.temp_data_service import TempDataService
from services.remark_html import sanitize_remark_html
from utils.pagination import parse_page_param, total_pages_for
from utils.permissions import require_login

preview_bp = Blueprint("preview", __name__)
# Any logged-in role (Member and above) can create/preview estimates.
preview_bp.before_request(require_login)


def _temp_data_service() -> TempDataService:
    return TempDataService(db_path=current_app.config["MHES_DB_PATH"])


@preview_bp.route("/", methods=["GET"])
def preview_page() -> str:
    """Render the preview page.

    Returns:
        Rendered preview template.
    """
    return render_template("preview.html")


@preview_bp.route("/temp", methods=["GET"])
def temp_data_page() -> str:
    """Render the temporary data list page.

    Shows a master list of Preview data that was stashed on the server
    when the user navigated to the AI Chatbot from the nav menu while
    Preview had data. Each row links to the detail page for the full
    estimate breakdown.

    Returns:
        Rendered temporary data list template.
    """
    return render_template("temp_data.html")


@preview_bp.route("/temp/<stash_id>", methods=["GET"])
def temp_data_detail_page(stash_id: str) -> str:
    """Render the full estimate detail for a single stashed snapshot."""
    if not _temp_data_service().exists(stash_id):
        flash("Temporary data not found. It may have already been restored or discarded.", "warning")
        return redirect(url_for("preview.temp_data_page"))
    return render_template("temp_data_detail.html", stash_id=stash_id)


@preview_bp.route("/temp/stashes", methods=["GET"])
def list_stashes():
    """Return all stashed Preview snapshots as JSON."""
    return jsonify(_temp_data_service().list_stashes())


TEMP_STASHES_PER_PAGE = 10


@preview_bp.route("/temp/stashes/page", methods=["GET"])
def list_stashes_page():
    """Return one page of stashed Preview snapshots as JSON, newest first.

    Supports server-side pagination (``page``) combined with From Date /
    To Date / Project Name filters, so only one page of stashes is ever
    loaded from the database per request.
    """
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    project_name = (request.args.get("project_name") or "").strip()
    page = parse_page_param(request.args.get("page"))

    service = _temp_data_service()
    items, total = service.list_stashes_page(
        page=page,
        per_page=TEMP_STASHES_PER_PAGE,
        from_date=from_date or None,
        to_date=to_date or None,
        project_name=project_name or None,
    )
    total_pages = total_pages_for(total, TEMP_STASHES_PER_PAGE)
    if page > total_pages:
        page = total_pages
        items, total = service.list_stashes_page(
            page=page,
            per_page=TEMP_STASHES_PER_PAGE,
            from_date=from_date or None,
            to_date=to_date or None,
            project_name=project_name or None,
        )

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "per_page": TEMP_STASHES_PER_PAGE,
    })


@preview_bp.route("/temp/stashes/<stash_id>", methods=["GET"])
def get_stash(stash_id: str):
    """Return a single stashed Preview snapshot as JSON."""
    stash = _temp_data_service().get_by_key(stash_id)
    if stash is None:
        return jsonify({"error": "Stash not found."}), 404
    return jsonify(stash)


@preview_bp.route("/temp/stashes", methods=["POST"])
def create_stash():
    """Stash a Preview snapshot on the server.

    Body: {"categories": [...], "totals": {...}, "projectName": "...",
    "createdBy": "...", "projectRemark": "..."}
    """
    data = request.get_json(silent=True) or {}
    categories = data.get("categories") or []

    if not isinstance(categories, list) or not categories:
        return jsonify({"error": "No categories to stash."}), 400

    stash = _temp_data_service().add_stash(
        categories=categories,
        totals=data.get("totals") or {},
        project_name=data.get("projectName") or "",
        created_by=data.get("createdBy") or "",
        project_remark=sanitize_remark_html(data.get("projectRemark") or ""),
    )
    return jsonify(stash), 201


@preview_bp.route("/temp/stashes/<stash_id>", methods=["DELETE"])
def delete_stash(stash_id: str):
    """Remove a single stash by id."""
    removed = _temp_data_service().remove_stash(stash_id)
    if not removed:
        return jsonify({"error": "Stash not found."}), 404
    return jsonify({"ok": True})


# TODO: Add route for paginated data preview
# TODO: Add route for file-specific preview
# TODO: Add route for search/filter within preview
