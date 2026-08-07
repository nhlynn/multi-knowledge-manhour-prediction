"""Role-based authorization for MHES (Phase 3).

Two ways to gate a route, both funneling through the same checks so
behavior is identical either way (HTML pages get a flash + redirect to
login/home; JSON/AJAX callers get a 401/403 JSON body instead):

1. Decorate an individual view function — ``@login_required`` or
   ``@roles_required("Admin", "Team Manager")`` — for routes defined
   directly on the ``Flask`` app (e.g. ``app.py``'s ``index``).
2. Register ``require_login`` or ``require_roles(...)`` as a
   blueprint-wide ``before_request`` hook — used for the ``routes/*.py``
   blueprints, so every route in a blueprint is covered by one line
   instead of one decorator per view function.

This module only decides *who* is allowed to reach a view. It never
touches what a view does once let through — no business logic here.

Roles (least to most privileged): "Team Manager" < "Admin".
See ``repositories.user_repository.VALID_ROLES`` for the canonical list.
"""

from functools import wraps
from typing import Callable

from flask import Response, flash, jsonify, redirect, request, session, url_for

from repositories.user_repository import VALID_ROLES
from utils.http import wants_json as _wants_json

# What every "block this request" helper below returns: either a plain
# redirect Response, or a (jsonify(...), status_code) tuple for AJAX/JSON
# callers — this is also exactly Flask's before_request contract, where
# None means "let the request through".
GateResponse = Response | tuple[Response, int]


def _unauthenticated_response() -> GateResponse:
    if _wants_json():
        return jsonify({"error": "Login required."}), 401
    flash("Please log in to continue.", "warning")
    return redirect(url_for("auth.login_page", next=request.path))


def _forbidden_response() -> GateResponse:
    if _wants_json():
        return jsonify({"error": "You do not have permission to perform this action."}), 403
    flash("You do not have permission to access that page.", "danger")
    return redirect(url_for("index"))


def _forbidden_response_strict() -> GateResponse:
    """Like ``_forbidden_response``, but a hard HTTP 403 in every case —
    including a normal browser (HTML) request, which the friendlier
    variant above instead turns into a flash message + redirect home.

    Used where "you don't have permission" must be an unambiguous
    403 rather than a softer redirect (e.g. Team Management — see
    ``require_roles_strict``).
    """
    if _wants_json():
        return jsonify({"error": "You do not have permission to perform this action."}), 403
    return Response("Forbidden: you do not have permission to access this page.", status=403)


def _validate_roles(roles: tuple[str, ...]) -> set[str]:
    allowed = set(roles)
    unknown = allowed - set(VALID_ROLES)
    if unknown:
        raise ValueError(f"Unknown role(s) passed to permission check: {sorted(unknown)}")
    return allowed


# ------------------------------------------------------------------
# Shared checks
# ------------------------------------------------------------------
#
# Both the decorator and before_request-hook styles below funnel
# through these two functions, so the actual "who is allowed through"
# logic exists exactly once. Each returns None to mean "let the request
# through" or an error response to mean "block it" — that's also
# exactly Flask's before_request return-value contract, so a hook can
# just `return _check_login()`/`return _check_roles(allowed)` directly.

def _check_login() -> GateResponse | None:
    """Return an error response if no one is logged in, else None."""
    if session.get("user_id") is None:
        return _unauthenticated_response()
    return None


def _check_roles(allowed: set[str]) -> GateResponse | None:
    """Return an error response if not logged in or not in ``allowed``, else None."""
    response = _check_login()
    if response is not None:
        return response
    if session.get("role") not in allowed:
        return _forbidden_response()
    return None


def _check_roles_strict(allowed: set[str]) -> GateResponse | None:
    """Same role check as ``_check_roles``, but blocks with a hard 403
    (``_forbidden_response_strict``) rather than a flash+redirect.

    Not being logged in at all is unchanged — still routed through
    ``_check_login`` (401 JSON / redirect-to-login for HTML), since
    that's a different failure mode (unauthenticated, not
    unauthorized) than a logged-in user with the wrong role.
    """
    response = _check_login()
    if response is not None:
        return response
    if session.get("role") not in allowed:
        return _forbidden_response_strict()
    return None


# ------------------------------------------------------------------
# Per-view decorators (for routes defined directly on the Flask app)
# ------------------------------------------------------------------

def login_required(view_func: Callable) -> Callable:
    """Require any logged-in user (any of the three roles)."""

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        return _check_login() or view_func(*args, **kwargs)

    return wrapped


def roles_required(*roles: str) -> Callable:
    """Require a logged-in user whose role is one of ``roles``."""
    allowed = _validate_roles(roles)

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            return _check_roles(allowed) or view_func(*args, **kwargs)

        return wrapped

    return decorator


# ------------------------------------------------------------------
# Blueprint-wide before_request hooks
# ------------------------------------------------------------------

def require_login() -> GateResponse | None:
    """``before_request`` hook: require any logged-in user for a whole blueprint."""
    return _check_login()


def require_roles(*roles: str) -> Callable:
    """Return a ``before_request`` hook requiring one of ``roles`` for a whole blueprint."""
    allowed = _validate_roles(roles)

    def hook() -> GateResponse | None:
        return _check_roles(allowed)

    return hook


def require_roles_strict(*roles: str) -> Callable:
    """Return a ``before_request`` hook requiring one of ``roles`` for a
    whole blueprint, like ``require_roles`` — but a logged-in user with
    the wrong role gets a hard HTTP 403 (``_forbidden_response_strict``)
    instead of a flash message + redirect home.

    Used for Team Management (``routes/admin.py``'s ``admin_bp``),
    where "not an Admin" must be an unambiguous 403 for every request
    (view/create/edit/delete alike), not a softer redirect.
    """
    allowed = _validate_roles(roles)

    def hook() -> GateResponse | None:
        return _check_roles_strict(allowed)

    return hook


__all__ = [
    "login_required",
    "roles_required",
    "require_login",
    "require_roles",
    "require_roles_strict",
]
