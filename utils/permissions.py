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

Roles (least to most privileged): "Member" < "Team Manager" < "Admin".
See ``repositories.user_repository.VALID_ROLES`` for the canonical list.
"""

from functools import wraps
from typing import Callable

from flask import flash, jsonify, redirect, request, session, url_for

from repositories.user_repository import VALID_ROLES


def _wants_json() -> bool:
    """Best-effort guess at whether the caller wants a JSON error, not an HTML redirect.

    Covers both explicit JSON POST bodies (``request.is_json``) and
    ``fetch()``-style AJAX calls, which typically send ``Accept: */*`` —
    ``best_match`` resolves that tie in favor of the first candidate,
    ``application/json``, which is what we want for the JS-driven pages
    (Preview stash APIs, Chatbot search, etc.).
    """
    if request.is_json:
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json"


def _unauthenticated_response():
    if _wants_json():
        return jsonify({"error": "Login required."}), 401
    flash("Please log in to continue.", "warning")
    return redirect(url_for("auth.login_page", next=request.path))


def _forbidden_response():
    if _wants_json():
        return jsonify({"error": "You do not have permission to perform this action."}), 403
    flash("You do not have permission to access that page.", "danger")
    return redirect(url_for("index"))


def _validate_roles(roles: tuple[str, ...]) -> set[str]:
    allowed = set(roles)
    unknown = allowed - set(VALID_ROLES)
    if unknown:
        raise ValueError(f"Unknown role(s) passed to permission check: {sorted(unknown)}")
    return allowed


# ------------------------------------------------------------------
# Per-view decorators (for routes defined directly on the Flask app)
# ------------------------------------------------------------------

def login_required(view_func: Callable) -> Callable:
    """Require any logged-in user (any of the three roles)."""

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if session.get("user_id") is None:
            return _unauthenticated_response()
        return view_func(*args, **kwargs)

    return wrapped


def roles_required(*roles: str) -> Callable:
    """Require a logged-in user whose role is one of ``roles``."""
    allowed = _validate_roles(roles)

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if session.get("user_id") is None:
                return _unauthenticated_response()
            if session.get("role") not in allowed:
                return _forbidden_response()
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


# ------------------------------------------------------------------
# Blueprint-wide before_request hooks
# ------------------------------------------------------------------

def require_login() -> None:
    """``before_request`` hook: require any logged-in user for a whole blueprint."""
    if session.get("user_id") is None:
        return _unauthenticated_response()


def require_roles(*roles: str) -> Callable:
    """Return a ``before_request`` hook requiring one of ``roles`` for a whole blueprint."""
    allowed = _validate_roles(roles)

    def hook():
        if session.get("user_id") is None:
            return _unauthenticated_response()
        if session.get("role") not in allowed:
            return _forbidden_response()

    return hook


__all__ = [
    "login_required",
    "roles_required",
    "require_login",
    "require_roles",
]
