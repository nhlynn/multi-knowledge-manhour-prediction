"""Session helpers for MHES.

The Flask session (signed cookie, ``SECRET_KEY`` from ``config.py``)
stores only ``user_id``/``username``/``team_id``/``role`` — the full user
record is re-read from the ``users`` table on each request, the same
"reconstruct from storage per request" style already used by every other
service in this app (e.g. ``ExcelService``/``EmbeddingService`` are
re-instantiated per route call rather than cached).

``start_session``/``end_session`` are the single place that knows which
keys make up a login session — ``routes/auth.py`` calls these instead of
setting/clearing session keys itself, so that set of keys has one
source of truth instead of being implicitly duplicated at every call site.

``check_account_still_active`` is a global ``before_request`` hook (see
``app.py``) enforcing that a session stays valid only as long as its
account (and that account's team) remain ``Active`` — checked fresh
against the database on every request, so an Admin deactivating a user
or their team takes effect on that user's very next request, with no
server restart and no stale cache.
"""

from typing import Any

from flask import current_app, flash, jsonify, redirect, session, url_for

from repositories.team_repository import TeamRepository
from repositories.user_repository import UserRepository
from utils.http import wants_json as _wants_json
from utils.permissions import GateResponse

_ACCOUNT_DEACTIVATED_MESSAGE = "Your account has been deactivated. Please contact your administrator."
_TEAM_DEACTIVATED_MESSAGE = "Your team has been deactivated. Please contact your administrator."


def get_current_user() -> dict[str, Any] | None:
    """Return the logged-in user's record, or None if no one is logged in."""
    user_id = session.get("user_id")
    if user_id is None:
        return None
    repo = UserRepository(current_app.config["MHES_DB_PATH"])
    return repo.get_by_id(user_id)


def start_session(user: dict[str, Any]) -> None:
    """Populate the Flask session for a newly authenticated user.

    Clears any prior session data first (e.g. from a previous login in
    the same browser session).

    Marks the session permanent so the user stays signed in across
    browser restarts (persistent login) — the cookie gets a real
    expiration (``config.py``'s ``PERMANENT_SESSION_LIFETIME``) instead
    of vanishing the moment the browser closes, and Flask re-issues
    that expiration on every request by default, so an active user
    never hits it. The only way out is an explicit logout
    (``end_session``) or the account being deactivated/removed
    (``check_account_still_active``) — nothing here changes either of
    those.

    Args:
        user: A user record as returned by ``AuthService.authenticate``
            (must have ``id``, ``username``, ``team_id``, ``role``).
    """
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["team_id"] = user["team_id"]
    session["role"] = user["role"]


def end_session() -> str | None:
    """Clear the current session.

    Returns:
        The username that was logged in, or None if no one was.
    """
    username = session.get("username")
    session.clear()
    return username


def _invalidate_session(message: str) -> GateResponse:
    """Clear the current session and return the "block this request"
    response — a redirect-to-login with a flash message for a normal
    browser request, or a 403 JSON body for an AJAX/API caller.

    Shared by every check in ``check_account_still_active`` below, so
    each one only needs to decide *whether* to invalidate, not *how*.
    """
    session.clear()
    if _wants_json():
        return jsonify({"error": message}), 403
    flash(message, "danger")
    return redirect(url_for("auth.login_page"))


def check_account_still_active() -> GateResponse | None:
    """``before_request`` hook: invalidate a session whose account (or
    that account's team) is no longer Active.

    Registered directly on the Flask app (not any one blueprint), so it
    runs for every request, app-wide — a status change made through
    Edit User or Edit Team takes effect on that user's very next
    request, anywhere in the app, without a restart. A request with no
    session (nobody logged in) is a no-op and returns immediately —
    most requests take this fast path.

    Checks, in order:

    1. The account still exists (a request with no session is already
       handled above; this covers an account deleted after the session
       started, rather than leaving a session pointing at nothing).
    2. The account's ``status`` is still ``Active``.
    3. The account's team still exists and its ``status`` is still
       ``Active`` — a user's own status can be Active while their team
       has since been deactivated, and that should lock them out too.

    Each failure shows its own specific message, so a deactivated user
    isn't told their *team* was deactivated (or vice versa).

    Returns:
        None to let the request through unchanged. Otherwise, the
        session is already cleared and a redirect-to-login (HTML) or
        403 JSON body (AJAX/API caller) is returned — Flask's
        before_request contract for "block this request".
    """
    user_id = session.get("user_id")
    if user_id is None:
        return None

    db_path = current_app.config["MHES_DB_PATH"]

    user = UserRepository(db_path).get_by_id(user_id)
    if user is None or user["status"] != "Active":
        return _invalidate_session(_ACCOUNT_DEACTIVATED_MESSAGE)

    team = TeamRepository(db_path).get_by_id(user["team_id"])
    if team is None or team["status"] != "Active":
        return _invalidate_session(_TEAM_DEACTIVATED_MESSAGE)

    return None
