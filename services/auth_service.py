"""Authentication service for MHES.

Handles password hashing/verification and credential checking against the
``users`` table. Session/cookie handling is not this module's job — that
lives in ``routes/auth.py`` (via Flask's built-in, ``SECRET_KEY``-signed
session, the same mechanism already used by flash messages elsewhere in
the app).

Also handles the Forgot Password flow's server side
(``request_password_reset``) — login itself
(``authenticate``/``hash_password``) is completely unchanged by this;
Forgot Password is purely additive.
"""

import hashlib
import logging
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from repositories.password_reset_repository import PasswordResetTokenRepository
from repositories.user_repository import UserRepository
from services.email_service import SmtpConfig, build_password_reset_email, send_email

logger = logging.getLogger(__name__)


def _hash_reset_token(raw_token: str) -> str:
    """One-way hash of a raw password-reset token for storage/lookup.

    Never store or log the raw token itself — only this hash (same
    principle as password storage). SHA-256 is sufficient here (unlike
    password hashing, this input is already a high-entropy random value,
    not a low-entropy human-chosen secret, so a slow KDF isn't needed).
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class AuthService:
    """Service for verifying login credentials and handling password resets."""

    # A fixed hash with no corresponding real account, used only to
    # verify against when the submitted username doesn't exist (see
    # authenticate() below). Without this, a lookup miss returns
    # immediately while a lookup hit always pays for a full
    # check_password_hash() call — a measurable response-time
    # difference an attacker could use to enumerate valid usernames
    # even without ever seeing a login error message. Verifying against
    # this dummy hash either way keeps the two cases' timing equivalent.
    _DUMMY_HASH = generate_password_hash("mhes-dummy-password-for-timing-safety")

    def __init__(self, db_path: str) -> None:
        """Initialize AuthService.

        Args:
            db_path: Path to the shared MHES SQLite database.
        """
        self.users = UserRepository(db_path)
        self.reset_tokens = PasswordResetTokenRepository(db_path)

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a plaintext password for storage."""
        return generate_password_hash(password)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """Verify a username/password pair against the ``users`` table.

        Returns the user's record whenever the credentials are
        correct — even for an ``Inactive`` account. The caller
        (``routes/auth.py::login``) is responsible for checking
        ``status`` before starting a session, so it can show a
        specific "account deactivated" message rather than a generic
        credentials error. Revealing that distinction only after the
        password has already been verified correct means this can't
        be used to enumerate deactivated accounts — an attacker who
        doesn't already know a valid password never sees it, since a
        wrong password or unknown username both return None exactly
        as before.

        ``last_login`` is only recorded for an ``Active`` account —
        never on a failed attempt (wrong password/unknown username,
        both returning before this point), and never for a
        correct-but-Inactive login, since that's not a successful
        login.

        Args:
            username: Login name as submitted.
            password: Plaintext password as submitted.

        Returns:
            The user's record if the credentials are valid (whatever
            its ``status``), else None.
        """
        user = self.users.get_by_username(username)
        if user is None:
            check_password_hash(self._DUMMY_HASH, password)  # timing-safety decoy; result unused
            return None
        if not check_password_hash(user["password_hash"], password):
            return None
        if user["status"] == "Active":
            self.users.update_last_login(user["id"], datetime.now().isoformat())
        return user

    def request_password_reset(
        self,
        email: str,
        *,
        reset_url_base: str,
        smtp: SmtpConfig,
        token_ttl_minutes: int = 30,
    ) -> None:
        """Best-effort: if an account exists for ``email``, generate a
        single-use reset token and email a reset link.

        Always returns ``None`` and never raises — the caller
        (``routes/auth.py::forgot_password``) must show the identical
        message to the user regardless of whether anything was actually
        found/sent, to avoid revealing which emails have accounts
        (account enumeration). To make that safe in practice, not just
        in the response body:

        - The "no such account" path performs equivalent-cost dummy work
          (``_dummy_reset_token_work``) — including a throwaway DB
          insert+delete of the same shape as the real path's insert —
          instead of returning immediately, so it isn't measurably
          faster than the real path.
        - The actual email send happens on a background thread, so the
          real path's HTTP response doesn't wait on a potentially slow
          SMTP round trip either — without that, a real account would
          still take measurably longer to respond than a nonexistent one.

        Args:
            email: Email address as submitted on the Forgot Password form.
            reset_url_base: Scheme+host to build an absolute reset link
                from (e.g. ``request.url_root`` from the calling route).
            smtp: SMTP connection settings (see ``services.email_service.SmtpConfig``).
            token_ttl_minutes: How long the generated token stays valid.
        """
        user = self.users.get_by_email(email.strip().lower())
        if user is None:
            _dummy_reset_token_work(self.reset_tokens)
            return

        raw_token = secrets.token_urlsafe(32)
        now = datetime.now()
        expires_at = now + timedelta(minutes=token_ttl_minutes)
        self.reset_tokens.create_token(
            user_id=user["id"],
            token_hash=_hash_reset_token(raw_token),
            expires_at=expires_at.isoformat(),
            created_at=now.isoformat(),
        )

        reset_link = f"{reset_url_base.rstrip('/')}/auth/reset-password/{raw_token}"
        threading.Thread(
            target=_send_reset_email_safe,
            args=(smtp, user["email"], user["username"], reset_link, token_ttl_minutes),
            daemon=True,
        ).start()

    def get_reset_token_status(self, raw_token: str) -> str:
        """Classify a reset token so the caller can show the right page.

        Returns one of:
            "valid"   — exists, unused, not expired (show the new-password form).
            "expired" — exists, unused, but past its expiry.
            "invalid" — doesn't exist, or has already been used.

        "Already used" is folded into "invalid" rather than given its
        own status — from the visitor's perspective both mean "this
        link doesn't work anymore, request a new one", and a used token
        looking identical to a never-issued one avoids confirming
        whether a reset was actually completed via this exact link.

        Used by ``routes/auth.py::reset_password_page`` (GET) and
        re-checked independently by ``reset_password`` (POST) rather
        than trusting this alone, since time can pass between the two
        requests.
        """
        token_hash = _hash_reset_token(raw_token)
        now = datetime.now().isoformat()
        if self.reset_tokens.get_valid_token(token_hash, now) is not None:
            return "valid"

        record = self.reset_tokens.get_by_token_hash(token_hash)
        if record is not None and record["used_at"] is None and record["expires_at"] <= now:
            return "expired"
        return "invalid"

    def reset_password(self, raw_token: str, new_password: str) -> dict[str, Any] | None:
        """Verify a reset token and, if valid, set the account's new password.

        Rejects the token identically whether it never existed, has
        already been used, or has expired — ``PasswordResetTokenRepository
        .get_valid_token`` collapses all three into "not found" by design.

        Args:
            raw_token: The token from the reset link (not yet hashed).
            new_password: The new plaintext password (already checked
                for strength/confirmation-match by the caller).

        Returns:
            The updated user's record on success, or None if the token
            was invalid, expired, or already used (nothing is changed
            in that case).
        """
        now = datetime.now().isoformat()
        record = self.reset_tokens.get_valid_token(_hash_reset_token(raw_token), now)
        if record is None:
            return None

        # Claim the token BEFORE touching the password, not after.
        # mark_used()'s UPDATE ... WHERE used_at IS NULL is atomic at the
        # SQLite engine level, so if two concurrent requests both passed
        # the get_valid_token() check above for the same token, only one
        # of them can win this call — the other gets False back and
        # aborts here, never reaching set_password. Marking used
        # first (rather than after, as this used to do) is what makes
        # "single-use" actually hold under a race, not just in the
        # common single-request case.
        if not self.reset_tokens.mark_used(record["id"], now):
            return None

        user = self.users.get_by_id(record["user_id"])
        if user is None:
            # The account was somehow removed after the token was issued.
            # The token is already marked used above either way.
            return None

        self.users.set_password(user["id"], password_hash=self.hash_password(new_password), changed_at=now)
        logger.info("Password reset completed for user_id=%s via token id=%s", user["id"], record["id"])
        return user


def _dummy_reset_token_work(reset_tokens: PasswordResetTokenRepository) -> None:
    """Timing-safety decoy for ``request_password_reset``: performs work
    of equivalent cost to the real path's token generation, hashing, and
    single DB write, so a nonexistent email doesn't return measurably
    faster than a real one. The decoy row is inserted and then
    immediately removed again — it never lingers in the table and never
    references a real user.
    """
    now = datetime.now().isoformat()
    decoy = reset_tokens.create_token(
        user_id=-1,
        token_hash=_hash_reset_token(secrets.token_urlsafe(32)),
        expires_at=now,
        created_at=now,
    )
    reset_tokens.delete_by_id(decoy["id"])


def _send_reset_email_safe(
    smtp: SmtpConfig, to_address: str, user_name: str, reset_link: str, token_ttl_minutes: int,
) -> None:
    """Runs on a background thread — must never raise, since nothing
    there would catch it; failures are only logged.

    Deliberately never logs ``reset_link`` (or anything derived from
    the raw token) — only ``to_address`` — even on failure, so the
    secret reset link never ends up in the application log.
    """
    subject, text_body, html_body = build_password_reset_email(
        user_name=user_name, reset_link=reset_link, expires_in_minutes=token_ttl_minutes,
    )
    try:
        send_email(
            smtp,
            to_address=to_address,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
    except Exception:
        logger.exception("Failed to send password reset email to %s.", to_address)
