"""Preview route blueprint for MHES.

Handles knowledge base data preview and browsing.
"""

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from scheduler.temp_data_service import TempDataService
from utils.pagination import parse_page_param, total_pages_for
from utils.permissions import require_login, roles_required

preview_bp = Blueprint("preview", __name__)
# Any logged-in role can reach this blueprint by default -- Temporary
# Data List/Detail (below) stay open to Admin for cross-team oversight
# (matching Export History's own Admin-sees-every-team pattern via
# _team_id_filter). Only the actual estimate-editing screen
# (preview_page, "/") additionally requires Team Manager -- see its
# own @roles_required decorator, since Admin manages teams/config
# rather than doing estimation work.
preview_bp.before_request(require_login)


def _temp_data_service() -> TempDataService:
    return TempDataService(db_path=current_app.config["MHES_DB_PATH"])


def _team_id_filter() -> int | None:
    """Return the ``team_id`` to scope Temporary Data reads to.

    None means "no filter" (see every team's stashes) — Admin only.
    Every other role only ever sees their own team's stashes. Mirrors
    ``routes/export.py``'s identical ``_team_id_filter`` for Export
    History.
    """
    if session.get("role") == "Admin":
        return None
    return session.get("team_id")


def _team_id_list_filter() -> int | None:
    """Return the effective ``team_id`` filter for the Temporary Data
    LIST page specifically, honoring an Admin's optional Team dropdown
    selection (``?team_id=``) on top of ``_team_id_filter``'s base rule.

    A Team Manager's ``team_id`` in the query string, if any, is always
    ignored -- their filter stays locked to their own team regardless
    of what's in the URL, so they can never see another team's
    stashes by hand-editing it. Only when ``_team_id_filter`` already
    returned None (Admin) does a query-string ``team_id`` take effect,
    narrowing "every team" down to one specific team; an absent or
    unparseable value leaves it at "every team".
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


@preview_bp.route("/", methods=["GET"])
@roles_required("Team Manager", redirect_endpoint="dashboard")
def preview_page() -> str:
    """Render the preview page.

    Passes the current team's name and, for teams whose export template
    has a fixed phase-column set (SGL/SSD), that fixed set of activity
    labels. The page uses these to tailor team-specific UI:

    - Only Bamawl Team's export consumes a task's buffer hours, so the
      editable Buffer field is shown only for teams whose export uses it
      (see BUFFER_EXPORT_TEAMS in preview.html).
    - SGL/SSD tasks must have exactly the template's phase columns as
      their activities (an activity whose name isn't a template phase
      has no column to export into, so its hours would silently drop).
      Passing FIXED_PHASES lets the page normalize every task to that
      set, drop the free-text "Add Activity", and auto-fill new tasks —
      so the Preview can never produce an un-exportable activity.

    Returns:
        Rendered preview template.
    """
    team_name = _current_team_name() or ""
    return render_template(
        "preview.html",
        team_name=team_name,
        fixed_phases=_fixed_phases_for_team(team_name),
        phase_formula=_phase_formula_for_team(team_name),
    )


def _current_team_name() -> str | None:
    """Return the current session's team's name, or None if it can't be resolved."""
    from repositories.team_repository import TeamRepository

    team = TeamRepository(current_app.config["MHES_DB_PATH"]).get_by_id(session["team_id"])
    return team["name"] if team is not None else None


def _fixed_phases_for_team(team_name: str) -> list[str]:
    """The team's export fixed phase labels, or [] for teams whose
    export has no fixed phase set (Bamawl/default, whose activities are
    free-form). Resolved from the same source the team's export builder
    uses, so Preview and export always agree on the phase set:

    - SGL/SSD read the labels from their export template's phase columns.
    - KiKan reads them from its DB column-mapping's ``phase_columns``
      (its export matches activities to those fixed columns by label, so
      an activity whose name isn't one of them has nowhere to export to).

    A missing/unreadable source yields [] (Preview then leaves
    activities free-form rather than guessing).
    """
    root = current_app.root_path
    if team_name == "SGL Team":
        from services.sgl_export_builder import SglExportBuilder
        return SglExportBuilder.fixed_phase_labels(root)
    if team_name == "SSD Team":
        from services.ssd_export_builder import SsdExportBuilder
        return SsdExportBuilder.fixed_phase_labels(root)
    if team_name == "KiKan Team":
        return _kikan_fixed_phases()
    return []


def _kikan_fixed_phases() -> list[str]:
    """KiKan Team's fixed phase labels, read from its configured
    column-mapping's ``phase_columns`` — the exact set its export writes
    (see services/kikan_export_builder.py's ``_phase_value``). [] if the
    mapping isn't seeded yet."""
    from services.kikan_export_builder import KikanExportBuilder

    try:
        mapping = KikanExportBuilder.resolve_column_mapping(
            current_app.config["MHES_DB_PATH"], session["team_id"],
        )
    except Exception:
        return []
    if not mapping:
        return []
    labels = []
    for phase in mapping.get("phase_columns", []):
        label = (phase or {}).get("label")
        if label and str(label).strip():
            labels.append(str(label).strip())
    return labels


# KiKan Team's per-phase auto-calculation, mirroring the coefficients and
# base references hard-coded in its Excel template's 工数詳細 sheet (row 2
# coefficients + the row-5 formulas). Only 実装工数/Development is entered
# by hand; every other phase is derived from it (or from another derived
# phase), so Preview can make Development the single editable field and
# recompute the rest exactly as the workbook would. The list order is
# dependency-safe: each phase's inputs appear before it (Review needs Test
# Specification; Management needs Review). If KiKan's template changes its
# coefficients or formula shape, update this to match.
_KIKAN_PHASE_FORMULA = {
    "base": "Development",  # 実装工数 (h) — the only editable phase
    "derived": [
        {"label": "Code Review",         "of": ["Development"], "coef": 0.10},   # =F*0.1
        {"label": "Spec Understanding",  "of": ["Development"], "coef": 0.10},   # =F*0.1
        {"label": "QA",                  "of": ["Development"], "coef": 0.05},   # =F*0.05
        {"label": "Test Specification",  "of": ["Development"], "coef": 0.30},   # =F*0.3
        {"label": "Review",              "of": ["Test Specification"], "coef": 0.15},  # =J*0.15
        {"label": "Implementation",      "of": ["Development"], "coef": 0.30},   # =F*0.3
        {"label": "Test Data Creation",  "of": ["Development"], "coef": 0.10},   # =F*0.1
        {"label": "Accidental Work",     "of": ["Development"], "coef": 0.02},   # =F*0.02
        # =(F+H+J+L)*0.03 — Development + Spec Understanding + Test Spec + Implementation
        {"label": "Risk", "of": ["Development", "Spec Understanding", "Test Specification", "Implementation"], "coef": 0.03},
        # =SUM(F:M)*0.05 — Development through Test Data Creation (8 phases, excludes Accidental/Risk/Management)
        {"label": "Management Manhours", "of": ["Development", "Code Review", "Spec Understanding", "QA", "Test Specification", "Review", "Implementation", "Test Data Creation"], "coef": 0.05},
    ],
}


def _phase_formula_for_team(team_name: str):
    """The team's per-phase auto-calculation spec (base phase + derived
    phases with their input phases and coefficient), or None for teams
    with no such formula. KiKan and Bamawl both derive every phase from
    a single Development input; Preview uses this to make Development the
    only editable phase and compute the rest."""
    if team_name == "KiKan Team":
        return _KIKAN_PHASE_FORMULA
    if team_name == "Bamawl Team":
        return _BAMAWL_PHASE_FORMULA
    return None


# Bamawl Team's per-phase auto-calculation, mirroring the coefficients
# and formulas in its Excel template's ALL_Detail sheet (row-2
# coefficients + row-5 formulas). Only D/Development man-hours is entered
# by hand; every other phase is derived from it (or from another derived
# phase — e.g. DB Design Review←DB Design←ERD←Development). Labels are
# Bamawl's config phase-column labels (unique, unlike the sheet's
# repeated column headers). List order is dependency-safe. If Bamawl's
# template changes its coefficients/shape, update this to match.
# NB: the template's リスク cell has a stray "AA47" reference (an empty
# cell = 0); it's intentionally omitted here as it contributes nothing.
_BAMAWL_PHASE_FORMULA = {
    "base": "Development",
    "derived": [
        {"label": "Code Review",                  "of": ["Development"], "coef": 0.07},
        {"label": "Prototype",                    "of": ["Development"], "coef": 0.15},
        {"label": "Prototype Review",             "of": ["Prototype"], "coef": 0.05},
        {"label": "Business Flow",                "of": ["Development"], "coef": 0.04},
        {"label": "Business Flow Review",         "of": ["Business Flow"], "coef": 0.20},
        {"label": "ERD",                          "of": ["Development"], "coef": 0.03},
        {"label": "ERD Review",                   "of": ["ERD"], "coef": 0.02},
        {"label": "DFD",                          "of": ["Development"], "coef": 0.0},
        {"label": "DFD Review",                   "of": ["DFD"], "coef": 0.0},
        {"label": "DB Design",                    "of": ["ERD"], "coef": 0.20},
        {"label": "DB Design Review",             "of": ["DB Design"], "coef": 0.20},
        {"label": "Screen/Form/Function",         "of": ["Development"], "coef": 0.40},
        {"label": "Screen/Form/Function Review",  "of": ["Screen/Form/Function"], "coef": 0.15},
        {"label": "Unit Test Specification",      "of": ["Screen/Form/Function"], "coef": 0.70},
        {"label": "Unit Test Review",             "of": ["Unit Test Specification"], "coef": 0.15},
        {"label": "Unit Test Implementation",     "of": ["Development"], "coef": 0.40},
        {"label": "Combined Test Specification",  "of": ["Screen/Form/Function"], "coef": 0.35},
        {"label": "Combined Test Review",         "of": ["Combined Test Specification"], "coef": 0.25},
        {"label": "Combined Test Implementation", "of": ["Development"], "coef": 0.40},
        {"label": "Comprehensive Test Implementation", "of": ["Development"], "coef": 0.25},
        {"label": "Test Data Creation",           "of": ["Development"], "coef": 0.0},
        {"label": "User Manual",                  "of": ["Development"], "coef": 0.05},
        {"label": "Accidental Work",              "of": ["Development"], "coef": 0.05},
        {"label": "Risk", "of": [
            "Development", "Business Flow", "ERD", "DFD", "DB Design",
            "Screen/Form/Function", "Unit Test Specification",
            "Combined Test Specification", "Combined Test Implementation",
            "Prototype", "Comprehensive Test Implementation",
        ], "coef": 0.05},
        {"label": "Management Manhours", "of": [
            "Development", "Code Review", "Prototype", "Prototype Review",
            "Business Flow", "Business Flow Review", "ERD", "ERD Review",
            "DFD", "DFD Review", "DB Design", "DB Design Review",
            "Screen/Form/Function", "Screen/Form/Function Review",
            "Unit Test Specification", "Unit Test Review",
            "Unit Test Implementation", "Combined Test Specification",
            "Combined Test Review", "Combined Test Implementation",
            "Comprehensive Test Implementation", "Test Data Creation",
            "User Manual",
        ], "coef": 0.15},
    ],
}


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
    teams = None
    if session.get("role") == "Admin":
        from repositories.team_repository import TeamRepository

        teams = TeamRepository(current_app.config["MHES_DB_PATH"]).list_all()
    return render_template("temp_data.html", teams=teams)


@preview_bp.route("/temp/<stash_id>", methods=["GET"])
def temp_data_detail_page(stash_id: str) -> str:
    """Render the full estimate detail for a single stashed snapshot."""
    if not _temp_data_service().exists(stash_id, team_id=_team_id_filter()):
        flash("Temporary data not found. It may have already been restored or discarded.", "warning")
        return redirect(url_for("preview.temp_data_page"))
    return render_template("temp_data_detail.html", stash_id=stash_id)


@preview_bp.route("/temp/stashes", methods=["GET"])
def list_stashes():
    """Return all stashed Preview snapshots as JSON, scoped to the
    caller's own team (every team's, for Admin)."""
    return jsonify(_temp_data_service().list_stashes(team_id=_team_id_filter()))


TEMP_STASHES_PER_PAGE = 10


@preview_bp.route("/temp/stashes/page", methods=["GET"])
def list_stashes_page():
    """Return one page of stashed Preview snapshots as JSON, newest first.

    Supports server-side pagination (``page``) combined with From Date /
    To Date / Project Name / Team filters, so only one page of stashes is
    ever loaded from the database per request. The Team filter
    (``?team_id=``) only has any effect for Admin -- see
    ``_team_id_list_filter``.
    """
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    project_name = (request.args.get("project_name") or "").strip()
    page = parse_page_param(request.args.get("page"))
    team_filter = _team_id_list_filter()

    service = _temp_data_service()
    items, total = service.list_stashes_page(
        team_id=team_filter,
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
            team_id=team_filter,
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
    stash = _temp_data_service().get_by_key(stash_id, team_id=_team_id_filter())
    if stash is None:
        return jsonify({"error": "Stash not found."}), 404
    return jsonify(stash)


@preview_bp.route("/temp/stashes", methods=["POST"])
def create_stash():
    """Stash a Preview snapshot on the server.

    Body: {"categories": [...], "totals": {...}, "projectName": "...",
    "createdBy": "..."}
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
        project_remark=data.get("projectRemark") or "",
        # Always the creating user's own team, never
        # _team_id_filter()'s "every team" (None for Admin) -- a
        # stash always belongs to exactly one real team, regardless of
        # who created it.
        team_id=session["team_id"],
    )
    return jsonify(stash), 201


@preview_bp.route("/temp/stashes/<stash_id>", methods=["DELETE"])
def delete_stash(stash_id: str):
    """Remove a single stash by id."""
    removed = _temp_data_service().remove_stash(stash_id, team_id=_team_id_filter())
    if not removed:
        return jsonify({"error": "Stash not found."}), 404
    return jsonify({"ok": True})


# TODO: Add route for paginated data preview
# TODO: Add route for file-specific preview
# TODO: Add route for search/filter within preview