"""Admin route blueprint for MHES (Phase 3).

Thin views over data that already exists via
``repositories/team_repository.py`` and ``repositories/user_repository.py``
— no business logic is introduced here, just Admin-gated visibility into
it (see ``utils/permissions.py``) plus routing; validation, uniqueness,
and persistence for team/user mutations all live in
``services/team_service.py``/``services/user_service.py``. Team CRUD
(view/create/edit/delete) and User Management CRUD (view/create/edit/delete
plus an Admin Reset Password action) are both fully implemented.
"""

import logging

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from repositories.user_repository import VALID_ROLES, UserRepository
from routes.auth import _render_for_token_status
from services import admin_service
from services.auth_service import AuthService
from services.email_service import SmtpConfig
from services.team_service import (
    VALID_STATUSES,
    TeamDeletionBlockedError,
    TeamValidationError,
    create_team,
    delete_team,
    is_team_code_locked,
    update_team,
)
from services.user_service import (
    CREATABLE_ROLES,
    UserDeletionBlockedError,
    UserValidationError,
    create_user,
    delete_user,
    update_user,
    validate_email,
    validate_username,
)
from utils.permissions import require_roles_strict

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)
# Manage Users / Manage Teams (view/create/edit/delete) is an Admin-only
# capability. Uses the "strict" variant (hard HTTP 403 for a logged-in
# non-Admin, on every route in this blueprint) rather than the
# friendlier flash+redirect used elsewhere — see utils/permissions.py.
admin_bp.before_request(require_roles_strict("Admin"))

_TEAMS_PER_PAGE = 20
_USERS_PER_PAGE = 20
_USERS_SORTABLE_COLUMNS = ("username", "role", "status", "team_id", "created_at", "last_login")


@admin_bp.route("/users", methods=["GET"])
def list_users() -> str:
    """List users, with search-by-username, search-by-email, team/role/status
    filters, sorting, and pagination.

    All filters/sort/page are plain ``?query=string`` GET params, so the
    page stays link-able/bookmarkable and works with a normal (non-JS)
    submit — same approach as ``list_teams`` below.
    """
    db_path = current_app.config["MHES_DB_PATH"]

    username_query = (request.args.get("username") or "").strip()
    email_query = (request.args.get("email") or "").strip()
    team_query = (request.args.get("team_id") or "").strip()
    role_query = (request.args.get("role") or "").strip()
    status_query = (request.args.get("status") or "").strip()
    if role_query not in VALID_ROLES:
        role_query = ""
    if status_query not in VALID_STATUSES:
        status_query = ""

    try:
        team_id_filter = int(team_query) if team_query else None
    except ValueError:
        team_id_filter = None

    sort_by = request.args.get("sort_by", "created_at")
    if sort_by not in _USERS_SORTABLE_COLUMNS:
        sort_by = "created_at"
    sort_dir = "desc" if (request.args.get("sort_dir") or "").lower() == "desc" else "asc"

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = max(page, 1)

    users, total = admin_service.list_users_page(
        db_path,
        username=username_query or None,
        email=email_query or None,
        team_id=team_id_filter,
        role=role_query or None,
        status=status_query or None,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=_USERS_PER_PAGE,
    )
    total_pages = max(-(-total // _USERS_PER_PAGE), 1)  # ceil division
    page = min(page, total_pages)

    return render_template(
        "admin_users.html",
        users=users,
        teams=admin_service.list_teams(db_path),
        roles=VALID_ROLES,
        statuses=VALID_STATUSES,
        username_query=username_query,
        email_query=email_query,
        team_query=team_query,
        role_query=role_query,
        status_query=status_query,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=_USERS_PER_PAGE,
        current_user_id=session.get("user_id"),
    )


def _user_form_defaults(form: dict) -> dict:
    """Build the field values to re-populate the Create User form with.

    Shared by the GET (blank form) and the POST-with-errors (re-show
    what was submitted, don't make the admin retype everything) paths.
    Password fields are deliberately excluded — never echoed back.
    Role/Status aren't here at all — Create User has no fields for
    either; both are fixed server-side (see ``create_user_submit``).
    """
    return {
        "username": form.get("username", ""),
        "email": form.get("email", ""),
        "team_id": form.get("team_id", ""),
    }


@admin_bp.route("/users/create", methods=["GET"])
def create_user_page() -> str:
    """Render the Create User form."""
    db_path = current_app.config["MHES_DB_PATH"]
    return render_template(
        "admin_user_create.html",
        teams=admin_service.list_teams(db_path),
        errors={},
        **_user_form_defaults({}),
    )


@admin_bp.route("/users/create", methods=["POST"])
def create_user_submit() -> str:
    """Validate and create a new user account.

    Role and Status are NOT read from the submitted form at all — the
    Create User form has no fields for either. Every new account is
    created as role="Team Manager" (``CREATABLE_ROLES[0]``, the only
    creatable role) and status="Active", fixed here server-side, so
    nothing the client sends (or a hand-crafted request omits/tampers
    with) can influence either value.
    """
    db_path = current_app.config["MHES_DB_PATH"]

    username = request.form.get("username", "")
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    team_id_raw = request.form.get("team_id", "")

    try:
        team_id = int(team_id_raw)
    except ValueError:
        team_id = None

    try:
        user = create_user(
            db_path,
            username=username,
            email=email,
            password=password,
            confirm_password=confirm_password,
            team_id=team_id,
            role=CREATABLE_ROLES[0],
            status="Active",
            performed_by_user_id=session.get("user_id"),
            performed_by_username=session.get("username"),
        )
    except UserValidationError as e:
        return render_template(
            "admin_user_create.html",
            teams=admin_service.list_teams(db_path),
            errors=e.errors,
            **_user_form_defaults(request.form),
        )

    flash(f"User '{user['username']}' created.", "success")
    return redirect(url_for("admin.list_users"))


@admin_bp.route("/users/check-username", methods=["GET"])
def check_username_availability():
    """Lightweight real-time username-availability check for Create/Edit User.

    Read-only, GET-only (safe method — no CSRF token required, so a
    form's live-typing JS can call this on every keystroke without
    needing to read the page's CSRF meta tag). Does not touch
    ``create_user``/``update_user`` or their validation in any way —
    this is purely an additional, separate lookup.

    Query params:
        username: The candidate username (leading/trailing whitespace
            ignored, matched case-insensitively — same rules
            ``services.user_service.validate_username``/
            ``repositories.user_repository.UserRepository.username_exists``
            already use for Create/Edit User).
        exclude_id: Optional user id to ignore when checking (so the
            Edit User page can check a username against every *other*
            account without flagging the account's own current name
            as taken).

    Returns:
        Always the same JSON shape: ``{"username": <cleaned>,
        "available": <bool>, "reason": <str|null>}``. ``reason`` is a
        human-readable explanation whenever ``available`` is false —
        either a format problem or "Username already exists.".
    """
    raw_username = request.args.get("username", "")
    cleaned_username = raw_username.strip()

    exclude_id = request.args.get("exclude_id", type=int)

    format_error = validate_username(cleaned_username)
    if format_error:
        return jsonify({"username": cleaned_username, "available": False, "reason": format_error})

    exists = UserRepository(current_app.config["MHES_DB_PATH"]).username_exists(
        cleaned_username, exclude_id=exclude_id,
    )
    if exists:
        return jsonify({
            "username": cleaned_username, "available": False,
            "reason": "Username already exists.",
        })

    return jsonify({"username": cleaned_username, "available": True, "reason": None})


@admin_bp.route("/users/check-email", methods=["GET"])
def check_email_availability():
    """Lightweight real-time email-availability check for Create/Edit User.

    Same shape and rules as ``check_username_availability`` above,
    mirrored for the ``email`` field — read-only, GET-only (no CSRF
    token needed), and completely separate from ``create_user``/
    ``update_user``; this never touches that path.

    Query params:
        email: The candidate email address (leading/trailing
            whitespace ignored, matched case-insensitively — same
            rules ``services.user_service.validate_email``/
            ``repositories.user_repository.UserRepository.email_exists``
            already use for Create/Edit User). Email is an optional
            field on the ``users`` table, so a blank value is always
            reported available (nothing to conflict with).
        exclude_id: Optional user id to ignore when checking (so the
            Edit User page can check an email against every *other*
            account without flagging the account's own current email
            as taken).

    Returns:
        Always the same JSON shape: ``{"email": <cleaned>,
        "available": <bool>, "reason": <str|null>}``. ``reason`` is a
        human-readable explanation whenever ``available`` is false —
        either a format problem or "Email already exists.".
    """
    raw_email = request.args.get("email", "")
    cleaned_email = raw_email.strip()

    exclude_id = request.args.get("exclude_id", type=int)

    format_error = validate_email(cleaned_email)
    if format_error:
        return jsonify({"email": cleaned_email, "available": False, "reason": format_error})

    exists = UserRepository(current_app.config["MHES_DB_PATH"]).email_exists(
        cleaned_email, exclude_id=exclude_id,
    )
    if exists:
        return jsonify({
            "email": cleaned_email, "available": False,
            "reason": "Email already exists.",
        })

    return jsonify({"email": cleaned_email, "available": True, "reason": None})


def _get_user_or_404(user_id: int) -> dict:
    user = admin_service.get_user(current_app.config["MHES_DB_PATH"], user_id)
    if user is None:
        abort(404)
    return user


def _smtp_config() -> SmtpConfig:
    """Same SMTP settings ``routes/auth.py::forgot_password`` uses — the
    Admin-triggered reset link is sent through the identical mail path.
    """
    cfg = current_app.config
    return SmtpConfig(
        host=cfg.get("SMTP_HOST"),
        port=cfg.get("SMTP_PORT", 587),
        username=cfg.get("SMTP_USERNAME"),
        password=cfg.get("SMTP_PASSWORD"),
        use_tls=cfg.get("SMTP_USE_TLS", True),
        opportunistic_tls=cfg.get("SMTP_OPPORTUNISTIC_TLS", True),
        from_address=cfg.get("MAIL_FROM_ADDRESS", "no-reply@mhes.local"),
    )


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET"])
def edit_user_page(user_id: int) -> str:
    """Render the Edit User form.

    Username/Email/Team/Role/Status are editable; Created Date and
    Last Login are shown for reference only. No password field exists
    on this screen at all — the existing password is never displayed,
    and changing it is not something this form can do (see
    ``services.user_service.update_user``, which has no password
    parameter).
    """
    db_path = current_app.config["MHES_DB_PATH"]
    user = _get_user_or_404(user_id)
    return render_template(
        "admin_user_edit.html",
        user=user,
        teams=admin_service.list_teams(db_path),
        roles=VALID_ROLES,
        statuses=VALID_STATUSES,
        errors={},
        username=user["username"],
        email=user["email"] or "",
        team_id=str(user["team_id"]),
        role=user["role"],
        status=user["status"],
        current_user_id=session.get("user_id"),
    )


@admin_bp.route("/users/<int:user_id>/edit", methods=["POST"])
def edit_user_submit(user_id: int) -> str:
    """Validate and apply changes to an existing user's profile fields."""
    db_path = current_app.config["MHES_DB_PATH"]
    user = _get_user_or_404(user_id)

    username = request.form.get("username", "")
    email = request.form.get("email", "")
    team_id_raw = request.form.get("team_id", "")
    role = request.form.get("role") or user["role"]
    status = request.form.get("status") or "Active"

    try:
        team_id = int(team_id_raw)
    except ValueError:
        team_id = None

    try:
        updated = update_user(
            db_path, user_id, username=username, email=email, team_id=team_id, role=role, status=status,
            performed_by_user_id=session.get("user_id"),
            performed_by_username=session.get("username"),
        )
    except UserValidationError as e:
        return render_template(
            "admin_user_edit.html",
            user=user,
            teams=admin_service.list_teams(db_path),
            roles=VALID_ROLES,
            statuses=VALID_STATUSES,
            errors=e.errors,
            username=username,
            email=email,
            team_id=team_id_raw,
            role=role,
            status=status,
            current_user_id=session.get("user_id"),
        )

    flash(f"User '{updated['username']}' updated.", "success")
    return redirect(url_for("admin.list_users"))


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["GET"])
def admin_reset_password_from_email(user_id: int) -> str:
    """Landing route for the reset-link email's URL
    (``/admin/users/<user_id>/reset-password?token=...``).

    Validates the token (same ``AuthService.get_reset_token_status``
    check, same 15-minute expiry, same single-use rule the self-service
    flow uses) BEFORE rendering anything, then reuses
    ``routes/auth.py``'s exact same page-selection logic
    (``_render_for_token_status``) and templates — no new UI. The
    rendered form still posts to the existing, unmodified
    ``/auth/reset-password/<token>`` route, so password-update logic
    is untouched. ``user_id`` only shapes the URL as required; the
    token alone determines which account is affected.
    """
    token = request.args.get("token", "")
    status = AuthService(db_path=current_app.config["MHES_DB_PATH"]).get_reset_token_status(token)
    return _render_for_token_status(status, token)


@admin_bp.route("/users/<int:user_id>/send-reset-link", methods=["POST"])
def admin_send_reset_link(user_id: int) -> str:
    """Generate a password reset link for ``user_id`` and email it to the
    ACTING ADMIN's own registered email address — never the target
    user's.

    A single click-and-done action — no intermediate page is rendered,
    no password is ever set directly here. The token is generated for
    the *selected* user (so the link, once opened, resets that
    account's password) via ``AuthService.send_reset_link_for_user``,
    which stores only the token's hash and expires it after
    ``PASSWORD_RESET_TOKEN_TTL_MINUTES``. The target account's own
    email (if any) is never read or used for delivery.
    """
    user = _get_user_or_404(user_id)

    admin_user = UserRepository(current_app.config["MHES_DB_PATH"]).get_by_id(session.get("user_id"))
    if not admin_user or not admin_user["email"]:
        flash("Can't send a reset link: your own admin account has no email on file.", "warning")
        return redirect(url_for("admin.list_users"))

    AuthService(db_path=current_app.config["MHES_DB_PATH"]).send_reset_link_for_user(
        user,
        deliver_to_email=admin_user["email"],
        reset_url_base=request.url_root,
        smtp=_smtp_config(),
        token_ttl_minutes=current_app.config.get("PASSWORD_RESET_TOKEN_TTL_MINUTES", 15),
    )

    logger.info(
        "Password reset link for user_id=%s (username=%r) sent to admin_id=%s (username=%r)'s own email.",
        user_id, user["username"], session.get("user_id"), session.get("username"),
    )
    flash(f"Password reset link for '{user['username']}' has been sent to your registered email.", "success")
    return redirect(url_for("admin.list_users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user_submit(user_id: int) -> str:
    """Delete a user account, refusing (with a clear error) if a safety
    rule forbids it.

    See ``services.user_service.get_user_deletion_blockers`` for
    exactly what's checked (self-deletion, last active Admin).
    """
    user = _get_user_or_404(user_id)

    try:
        delete_user(
            current_app.config["MHES_DB_PATH"], user_id,
            current_user_id=session.get("user_id"),
            current_username=session.get("username"),
        )
    except UserDeletionBlockedError as e:
        flash(
            f"Can't delete user '{user['username']}': " + " ".join(e.reasons),
            "danger",
        )
        return redirect(url_for("admin.list_users"))

    flash(f"User '{user['username']}' deleted.", "success")
    return redirect(url_for("admin.list_users"))


@admin_bp.route("/teams", methods=["GET"])
def list_teams() -> str:
    """List teams, with search-by-name, search-by-code, status filter, and pagination.

    All filters are plain ``?query=string`` GET params, so the page
    stays link-able/bookmarkable and the filter form works with a
    normal (non-JS) submit — consistent with this being a
    server-rendered list page rather than a fetch-driven one.
    """
    name_query = (request.args.get("name") or "").strip()
    code_query = (request.args.get("code") or "").strip()
    status_query = (request.args.get("status") or "").strip()
    if status_query not in VALID_STATUSES:
        status_query = ""

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = max(page, 1)

    teams, total = admin_service.list_teams_page(
        current_app.config["MHES_DB_PATH"],
        name=name_query or None,
        code=code_query or None,
        status=status_query or None,
        page=page,
        per_page=_TEAMS_PER_PAGE,
    )
    total_pages = max(-(-total // _TEAMS_PER_PAGE), 1)  # ceil division
    page = min(page, total_pages)

    return render_template(
        "admin_teams.html",
        teams=teams,
        name_query=name_query,
        code_query=code_query,
        status_query=status_query,
        statuses=VALID_STATUSES,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=_TEAMS_PER_PAGE,
    )


def _team_form_defaults(form: dict) -> dict:
    """Build the field values to re-populate the Create Team form with.

    Shared by the GET (blank form) and the POST-with-errors (re-show
    what was submitted, don't make the admin retype everything) paths.
    """
    return {
        "name": form.get("name", ""),
        "slug": form.get("slug", ""),
        "description": form.get("description", ""),
        "status": form.get("status") or "Active",
    }


@admin_bp.route("/teams/create", methods=["GET"])
def create_team_page() -> str:
    """Render the Create Team form."""
    return render_template(
        "admin_team_create.html", statuses=VALID_STATUSES, errors={}, **_team_form_defaults({}),
    )


@admin_bp.route("/teams/create", methods=["POST"])
def create_team_submit() -> str:
    """Validate and create a new team.

    The Team Code field is auto-suggested client-side from the Team
    Name (see ``admin_team_create.html``) but always submitted as
    whatever the admin left it as — edited or not — so what's
    validated/saved here is the actual submitted value, never
    silently regenerated server-side.
    """
    name = request.form.get("name", "")
    slug = request.form.get("slug", "")
    description = request.form.get("description", "")
    status = request.form.get("status") or "Active"

    try:
        team = create_team(
            current_app.config["MHES_DB_PATH"],
            name=name, slug=slug, description=description, status=status,
        )
    except TeamValidationError as e:
        return render_template(
            "admin_team_create.html",
            statuses=VALID_STATUSES,
            errors=e.errors,
            **_team_form_defaults(request.form),
        )

    flash(f"Team '{team['name']}' created.", "success")
    return redirect(url_for("admin.list_teams"))


def _get_team_or_404(team_id: int) -> dict:
    team = admin_service.get_team(current_app.config["MHES_DB_PATH"], team_id)
    if team is None:
        abort(404)
    return team


@admin_bp.route("/teams/<int:team_id>/edit", methods=["GET"])
def edit_team_page(team_id: int) -> str:
    """Render the Edit Team form.

    Team Code is shown read-only whenever ``is_team_code_locked`` says
    changing it is unsafe (see ``services/team_service.py``) — the
    template disables that field entirely in that case, so no edited
    value for it is ever submitted in the first place.
    """
    team = _get_team_or_404(team_id)
    slug_locked = is_team_code_locked(
        current_app.config["MHES_DB_PATH"], team_id, teams_folder=current_app.config["TEAMS_FOLDER"],
    )
    return render_template(
        "admin_team_edit.html",
        team=team,
        statuses=VALID_STATUSES,
        errors={},
        slug_locked=slug_locked,
        name=team["name"],
        slug=team["slug"],
        description=team["description"] or "",
        status=team["status"],
    )


@admin_bp.route("/teams/<int:team_id>/edit", methods=["POST"])
def edit_team_submit(team_id: int) -> str:
    """Validate and apply changes to an existing team."""
    team = _get_team_or_404(team_id)
    slug_locked = is_team_code_locked(
        current_app.config["MHES_DB_PATH"], team_id, teams_folder=current_app.config["TEAMS_FOLDER"],
    )

    name = request.form.get("name", "")
    slug = request.form.get("slug", "")
    description = request.form.get("description", "")
    status = request.form.get("status") or "Active"

    try:
        updated = update_team(
            current_app.config["MHES_DB_PATH"],
            team_id,
            name=name,
            slug=slug,
            description=description,
            status=status,
            teams_folder=current_app.config["TEAMS_FOLDER"],
        )
    except TeamValidationError as e:
        return render_template(
            "admin_team_edit.html",
            team=team,
            statuses=VALID_STATUSES,
            errors=e.errors,
            slug_locked=slug_locked,
            name=name,
            slug=team["slug"] if slug_locked else slug,
            description=description,
            status=status,
        )

    flash(f"Team '{updated['name']}' updated.", "success")
    return redirect(url_for("admin.list_teams"))


@admin_bp.route("/teams/<int:team_id>/delete", methods=["POST"])
def delete_team_submit(team_id: int) -> str:
    """Delete a team, refusing (with a clear error) if anything still depends on it.

    See ``services.team_service.get_team_deletion_blockers`` for
    exactly what's checked (Users, Knowledge Base, Export History).
    """
    team = _get_team_or_404(team_id)

    try:
        delete_team(
            current_app.config["MHES_DB_PATH"], team_id, teams_folder=current_app.config["TEAMS_FOLDER"],
        )
    except TeamDeletionBlockedError as e:
        flash(
            f"Can't delete team '{team['name']}': " + " ".join(e.reasons),
            "danger",
        )
        return redirect(url_for("admin.list_teams"))

    flash(f"Team '{team['name']}' deleted.", "success")
    return redirect(url_for("admin.list_teams"))
