"""Authentication route blueprint for MHES.

Handles login/logout via Flask's built-in session. Routes are kept thin —
credential verification lives in ``services/auth_service.py``.
"""

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from services.auth_service import AuthService

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


def _auth_service() -> AuthService:
    return AuthService(db_path=current_app.config["MHES_DB_PATH"])


def _safe_next(next_url: str) -> str | None:
    """Return ``next_url`` if it is a safe same-site relative path, else None.

    Guards against open-redirect (``next_url`` must start with a single
    ``/``, not ``//`` — the latter is browser-parsed as protocol-relative
    and would redirect off-site).
    """
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return None


@auth_bp.route("/login", methods=["GET"])
def login_page() -> str:
    """Render the login page (redirects to the landing page if already logged in)."""
    if session.get("user_id") is not None:
        return redirect(url_for("index"))
    return render_template("login.html", next_url=request.args.get("next", ""))


@auth_bp.route("/login", methods=["POST"])
def login() -> str:
    """Validate submitted credentials and start a session on success."""
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = request.form.get("next", "")

    if not username or not password:
        flash("Please enter both username and password.", "warning")
        return redirect(url_for("auth.login_page", next=next_url))

    user = _auth_service().authenticate(username, password)
    if user is None:
        logger.warning("Failed login attempt for username=%r", username)
        flash("Invalid username or password.", "danger")
        return redirect(url_for("auth.login_page", next=next_url))

    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["team_id"] = user["team_id"]
    session["role"] = user["role"]

    logger.info("User %r (id=%s) logged in.", user["username"], user["id"])
    flash(f"Welcome back, {user['username']}.", "success")
    return redirect(_safe_next(next_url) or url_for("index"))


@auth_bp.route("/logout", methods=["POST"])
def logout() -> str:
    """Clear the current session."""
    username = session.get("username")
    session.clear()
    if username:
        logger.info("User %r logged out.", username)
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login_page"))
