"""Authentication route blueprint for MHES.

Handles login/logout via Flask's built-in session. Routes are kept thin —
credential verification lives in ``services/auth_service.py``.
"""

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from services.auth_service import AuthService
from services.email_service import SmtpConfig
from utils.auth import end_session, start_session
from utils.login_rate_limiter import is_locked_out, record_failed_attempt, record_successful_attempt
from utils.password_policy import validate_password_strength
from utils.rate_limiter import is_rate_limited, record_request

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


def _auth_service() -> AuthService:
    return AuthService(db_path=current_app.config["MHES_DB_PATH"])


def _smtp_config() -> SmtpConfig:
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

    if is_locked_out(username):
        logger.warning("Login blocked for username=%r: too many recent failed attempts.", username)
        flash("Too many failed login attempts. Please try again in 15 minutes.", "danger")
        return redirect(url_for("auth.login_page", next=next_url))

    user = _auth_service().authenticate(username, password)
    if user is None:
        record_failed_attempt(username)
        logger.warning("Failed login attempt for username=%r", username)
        flash("Invalid username or password.", "danger")
        return redirect(url_for("auth.login_page", next=next_url))

    if user["status"] != "Active":
        record_failed_attempt(username)
        logger.warning("Login blocked for username=%r: account is Inactive.", username)
        flash("Your account has been deactivated. Please contact your administrator.", "danger")
        return redirect(url_for("auth.login_page", next=next_url))

    record_successful_attempt(username)
    start_session(user)

    logger.info("User %r (id=%s) logged in.", user["username"], user["id"])
    flash(f"Welcome back, {user['username']}.", "success")
    return redirect(_safe_next(next_url) or url_for("index"))


@auth_bp.route("/logout", methods=["POST"])
def logout() -> str:
    """Clear the current session."""
    username = end_session()
    if username:
        logger.info("User %r logged out.", username)
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login_page"))


_FORGOT_PASSWORD_FLASH_MESSAGE = (
    "If an account exists for that email, we've sent a password reset link. "
    "Please check your inbox."
)

# Deliberately generous (not a login-attempt lockout): this only throttles
# how often a *reset request* (and the email it triggers) can be
# generated — it must never be tight enough to routinely block a real
# user who just mistyped their email once or twice.
_FORGOT_PASSWORD_MAX_PER_EMAIL = 3
_FORGOT_PASSWORD_MAX_PER_IP = 10
_FORGOT_PASSWORD_WINDOW_SECONDS = 15 * 60


@auth_bp.route("/forgot-password", methods=["GET"])
def forgot_password_page() -> str:
    """Render the Forgot Password request form."""
    if session.get("user_id") is not None:
        return redirect(url_for("index"))
    return render_template("forgot_password.html")


@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password() -> str:
    """Request a password reset email.

    Always shows the identical confirmation message and redirects to
    the login page, whether or not the submitted email matches an
    account, and whether or not this specific request got rate-limited
    — see ``AuthService.request_password_reset`` for how the "does this
    email exist" case is also made safe against timing-based account
    enumeration, not just the response text. Rate limiting is applied
    per submitted email *and* per source IP, so neither spamming one
    target's inbox nor spraying many different emails from one source
    can run unbounded.
    """
    email = (request.form.get("email") or "").strip()

    if not email:
        flash("Please enter your email address.", "warning")
        return redirect(url_for("auth.forgot_password_page"))

    email_key = f"forgot_password:email:{email.lower()}"
    ip_key = f"forgot_password:ip:{request.remote_addr}"
    rate_limited = (
        is_rate_limited(
            email_key, max_requests=_FORGOT_PASSWORD_MAX_PER_EMAIL,
            window_seconds=_FORGOT_PASSWORD_WINDOW_SECONDS,
        )
        or is_rate_limited(
            ip_key, max_requests=_FORGOT_PASSWORD_MAX_PER_IP,
            window_seconds=_FORGOT_PASSWORD_WINDOW_SECONDS,
        )
    )

    if rate_limited:
        logger.warning(
            "Password reset request rate-limited (email=%r, ip=%s).", email, request.remote_addr,
        )
    else:
        record_request(email_key)
        record_request(ip_key)
        try:
            _auth_service().request_password_reset(
                email,
                reset_url_base=request.url_root,
                smtp=_smtp_config(),
                token_ttl_minutes=current_app.config.get("PASSWORD_RESET_TOKEN_TTL_MINUTES", 30),
            )
        except Exception:
            # Never let an unexpected failure here change the response
            # shape (that would itself be an enumeration signal) or
            # surface internal details to the client — log and fall
            # through to the same generic confirmation as success.
            logger.exception("Unexpected error while processing a password reset request.")
        logger.info("Password reset requested for email=%r", email)

    # Identical outcome either way: rate-limited, matched, or unmatched
    # all look the same to the client.
    flash(_FORGOT_PASSWORD_FLASH_MESSAGE, "info")
    return redirect(url_for("auth.login_page"))


def _render_for_token_status(status: str, token: str) -> str:
    """Render the page matching a reset token's current status.

    Shared by the GET and POST handlers below, so both show the exact
    same Invalid/Expired pages for the exact same conditions.
    """
    if status == "expired":
        return render_template("reset_token_expired.html")
    if status != "valid":
        return render_template("reset_token_invalid.html")
    return render_template("reset_password.html", token=token)


@auth_bp.route("/reset-password/<token>", methods=["GET"])
def reset_password_page(token: str) -> str:
    """Render the new-password form, or a dedicated Invalid/Expired page.

    Rejects an expired or already-used token here too (not just on
    submit) — no point showing a form the POST would just reject anyway.
    """
    if session.get("user_id") is not None:
        return redirect(url_for("index"))

    status = _auth_service().get_reset_token_status(token)
    return _render_for_token_status(status, token)


@auth_bp.route("/reset-password/<token>", methods=["POST"])
def reset_password(token: str) -> str:
    """Validate the new password and, if the token is still valid, apply it.

    The token's status is re-checked here independently of the GET
    handler's check (time may have passed, or it may have already been
    used by a concurrent request) before anything is written.
    """
    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    if new_password != confirm_password:
        flash("Passwords do not match.", "warning")
        return redirect(url_for("auth.reset_password_page", token=token))

    strength_error = validate_password_strength(new_password)
    if strength_error:
        flash(strength_error, "warning")
        return redirect(url_for("auth.reset_password_page", token=token))

    auth_service = _auth_service()
    status = auth_service.get_reset_token_status(token)
    if status != "valid":
        return _render_for_token_status(status, token)

    user = auth_service.reset_password(token, new_password)
    if user is None:
        # Consumed by a concurrent request between the status check
        # above and this call — vanishingly rare, but handled the same
        # as any other no-longer-valid token rather than erroring.
        return render_template("reset_token_invalid.html")

    logger.info("User %r (id=%s) reset their password.", user["username"], user["id"])
    return render_template("reset_password_success.html")