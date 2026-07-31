"""Outbound email sending for MHES.

Currently used only by the Forgot Password flow
(``AuthService.request_password_reset``). Uses plain SMTP (stdlib
``smtplib``) rather than a provider SDK — no new dependency, consistent
with this project's existing preference for dependency-free
implementations where a stdlib option covers the need (see
``utils/csrf.py``, ``utils/login_rate_limiter.py``).

Takes an explicit ``SmtpConfig`` rather than reading ``current_app.config``
directly, so this module has no Flask dependency and can be unit tested
(or reused from a non-request context, e.g. a future CLI script)
without an app context — the caller (a route) is responsible for
building ``SmtpConfig`` from ``current_app.config``.
"""

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

logger = logging.getLogger(__name__)


class EmailError(Exception):
    """Raised when sending an email fails (SMTP connection/auth/send error)."""


@dataclass
class SmtpConfig:
    """SMTP connection settings, resolved by the caller from app config.

    ``use_tls`` + ``opportunistic_tls`` together select one of three
    connection modes:
        use_tls=False                        -> plaintext (no encryption)
        use_tls=True, opportunistic_tls=True  -> plaintext connect, then
                                                  STARTTLS (port 587/25)
        use_tls=True, opportunistic_tls=False -> encrypted from the first
                                                  byte, via SMTP_SSL (port 465)
    """

    host: str | None
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    from_address: str
    opportunistic_tls: bool = True


def send_email(
    smtp: SmtpConfig, *, to_address: str, subject: str, text_body: str, html_body: str | None = None,
) -> None:
    """Send an email via SMTP, as plain text or as a text+HTML alternative.

    If ``smtp.host`` is not configured, this is a deliberate no-op
    (logged at WARNING) rather than an error — so environments without
    email configured don't crash a caller that assumes best-effort
    delivery (see ``AuthService.request_password_reset``, which must
    behave identically whether or not email is actually configured).

    Never logs ``text_body``/``html_body`` (only the recipient address
    and subject) — callers that embed a secret (e.g. a password reset
    link) in the body must never have that secret reach the logs
    through this function.

    Args:
        smtp: Connection settings — see ``SmtpConfig``.
        to_address: Recipient email address.
        subject: Email subject line.
        text_body: Plain-text email body (always sent — the fallback an
            email client shows if it can't render ``html_body``).
        html_body: Optional HTML email body. When given, the message is
            sent as ``multipart/alternative`` (both parts included; the
            client picks whichever it can render).

    Raises:
        EmailError: If ``smtp.host`` *is* configured but sending still
            fails (connection refused, authentication failure, etc).
    """
    if not smtp.host:
        logger.warning(
            "SMTP_HOST not configured; skipping email send to %s (subject=%r).",
            to_address, subject,
        )
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp.from_address
    message["To"] = to_address
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    implicit_tls = smtp.use_tls and not smtp.opportunistic_tls
    smtp_cls = smtplib.SMTP_SSL if implicit_tls else smtplib.SMTP

    try:
        with smtp_cls(smtp.host, smtp.port, timeout=10) as client:
            if smtp.use_tls and smtp.opportunistic_tls:
                client.starttls()
            if smtp.username and smtp.password:
                client.login(smtp.username, smtp.password)
            client.send_message(message)
    except Exception as e:
        logger.exception("Failed to send email to %s (subject=%r).", to_address, subject)
        raise EmailError(f"Failed to send email to {to_address}: {e}") from e

    logger.info("Sent email to %s (subject=%r).", to_address, subject)


def build_password_reset_email(
    *, user_name: str, reset_link: str, expires_in_minutes: int,
) -> tuple[str, str, str]:
    """Build the subject, plain-text body, and HTML body for a password
    reset email.

    Kept as pure string construction (no Jinja/``render_template``) so
    this has no Flask app-context dependency — the email is normally
    sent from a background thread (see
    ``AuthService.request_password_reset``), which doesn't have one.

    Args:
        user_name: The account's username, shown as a greeting.
        reset_link: The absolute, single-use reset URL (already built
            by the caller — this function only embeds it, it doesn't
            generate or validate it).
        expires_in_minutes: How long the link stays valid, for display only.

    Returns:
        ``(subject, text_body, html_body)``.
    """
    safe_name = escape(user_name)
    safe_link = escape(reset_link)

    subject = "Reset your MHES password"

    text_body = (
        f"Hi {user_name},\n\n"
        "We received a request to reset the password for your MHES account.\n\n"
        f"Reset your password using this link (expires in {expires_in_minutes} minutes):\n"
        f"{reset_link}\n\n"
        "Security notice: if you didn't request this, you can safely ignore this email — "
        "your password will not change unless you open the link above and choose a new one. "
        "Never share this link with anyone; MHES staff will never ask you for it.\n"
    )

    html_body = f"""\
<!DOCTYPE html>
<html>
  <body style="margin:0; padding:0; background-color:#f1f5f9; font-family:Arial, Helvetica, sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f1f5f9; padding:32px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.1);">
            <tr>
              <td style="background-color:#111827; padding:20px 32px;">
                <span style="color:#ffffff; font-size:18px; font-weight:bold;">MHES</span>
              </td>
            </tr>
            <tr>
              <td style="padding:32px;">
                <p style="margin:0 0 16px 0; font-size:15px; color:#0f172a;">Hi {safe_name},</p>
                <p style="margin:0 0 24px 0; font-size:15px; color:#334155; line-height:1.5;">
                  We received a request to reset the password for your MHES account.
                  Click the button below to choose a new password.
                </p>
                <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 24px 0;">
                  <tr>
                    <td style="border-radius:6px; background-color:#4f46e5;">
                      <a href="{safe_link}"
                         style="display:inline-block; padding:12px 28px; font-size:15px; font-weight:bold;
                                color:#ffffff; text-decoration:none; border-radius:6px;">
                        Reset Password
                      </a>
                    </td>
                  </tr>
                </table>
                <p style="margin:0 0 16px 0; font-size:13px; color:#64748b;">
                  This link will expire in <strong>{expires_in_minutes} minutes</strong>.
                </p>
                <p style="margin:0 0 8px 0; font-size:13px; color:#64748b; word-break:break-all;">
                  If the button doesn't work, copy and paste this link into your browser:<br>
                  <a href="{safe_link}" style="color:#4f46e5;">{safe_link}</a>
                </p>
                <hr style="border:none; border-top:1px solid #e2e8f0; margin:24px 0;">
                <p style="margin:0; font-size:12px; color:#94a3b8; line-height:1.5;">
                  <strong>Security notice:</strong> if you didn't request this, you can safely ignore
                  this email — your password will not change unless you open the link above and choose
                  a new one. Never share this link with anyone; MHES staff will never ask you for it.
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""
    return subject, text_body, html_body
