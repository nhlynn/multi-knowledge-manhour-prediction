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
account remains ``Active`` — checked fresh against the database on
every request, so an Admin deactivating a user takes effect on that
user's very next request, with no server restart and no stale cache.
"""

from typing import Any

from flask import current_app, flash, jsonify, redirect, session, url_for

from repositories.user_repository import UserRepository
from utils.http import wants_json as _wants_json
from utils.permissions import GateResponse

_DEACTIVATED_MESSAGE = "Your account has been deactivated. Please contact your administrator."


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

    Args:
        user: A user record as returned by ``AuthService.authenticate``
            (must have ``id``, ``username``, ``team_id``, ``role``).
    """
    session.clear()
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


def check_account_still_active() -> GateResponse | None:
    """``before_request`` hook: invalidate a session whose account is no
    longer Active.

    Registered directly on the Flask app (not any one blueprint), so it
    runs for every request, app-wide — a status change made through
    Edit User takes effect on this user's very next request, anywhere
    in the app, without a restart. A request with no session (nobody
    logged in) is a no-op and returns immediately — most requests take
    this fast path.

    Also treats an account that no longer exists at all (e.g. deleted
    after the session started) the same as a deactivated one, rather
    than leaving a session pointing at nothing.

    Returns:
        None to let the request through unchanged. Otherwise, the
        session is already cleared and a redirect-to-login (HTML) or
        403 JSON body (AJAX/API caller) is returned — Flask's
        before_request contract for "block this request".
    """
    user_id = session.get("user_id")
    if user_id is None:
        return None

    user = UserRepository(current_app.config["MHES_DB_PATH"]).get_by_id(user_id)
    if user is not None and user["status"] == "Active":
        return None

    session.clear()
    if _wants_json():
        return jsonify({"error": _DEACTIVATED_MESSAGE}), 403
    flash(_DEACTIVATED_MESSAGE, "danger")
    return redirect(url_for("auth.login_page"))
