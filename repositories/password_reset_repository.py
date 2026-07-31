"""Raw SQL data access for the ``password_reset_tokens`` table.

Supports the "Forgot Password" feature. No business logic lives here
(token generation, expiry-duration policy, email sending) — that
belongs to a service layer built on top of this repository, mirroring
the split used by every other repository in this package.

Only a token's *hash* is ever stored here — never the raw token itself
(same principle as ``users.password_hash``) — so a leak of this table
(backup, SQL injection, etc.) never hands out a directly usable reset
token. Callers must hash the raw token (e.g. SHA-256) before calling
``create_token``/``get_valid_token`` — this module has no way to
enforce that itself, since it never sees the raw token to compare
against.
"""

import logging
from typing import Any

from repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user_id ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_expires_at ON password_reset_tokens(expires_at);
"""
# token_hash is UNIQUE, which SQLite already backs with an implicit
# index -- deliberately no separate CREATE INDEX for it (that would be
# a redundant duplicate index over the same column).


class PasswordResetTokenRepository(BaseRepository):
    """Repository for CRUD access to the ``password_reset_tokens`` table."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)

    def create_token(
        self, *, user_id: int, token_hash: str, expires_at: str, created_at: str,
    ) -> dict[str, Any]:
        """Insert a new password reset token row.

        Args:
            user_id: Id of the user this token is for.
            token_hash: A one-way hash of the raw token (e.g. SHA-256
                hex digest) — the raw token itself must never be passed
                here or stored anywhere.
            expires_at: ISO datetime string after which the token is no
                longer valid.
            created_at: ISO datetime string.

        Returns:
            The newly created token record.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO password_reset_tokens
                    (user_id, token_hash, created_at, expires_at, used_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (user_id, token_hash, created_at, expires_at),
            )
        record = self.get_by_id(cursor.lastrowid)
        assert record is not None
        logger.info("Created password reset token id=%s for user_id=%s", cursor.lastrowid, user_id)
        return record

    def get_by_id(self, token_id: int) -> dict[str, Any] | None:
        """Return a single token record by id, or None if not found."""
        return self._fetch_one_dict(
            "SELECT * FROM password_reset_tokens WHERE id = ?", (token_id,)
        )

    def get_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        """Return the token record for ``token_hash`` regardless of
        whether it's still valid, or None if it never existed.

        Unlike ``get_valid_token``, this doesn't filter by expiry/used
        state — used by ``AuthService.get_reset_token_status`` to tell
        "expired" apart from "invalid/never existed" for the Reset
        Password page's dedicated Expired/Invalid views. Distinguishing
        those two isn't an account-enumeration risk (unlike the email
        lookup in ``request_password_reset``): a visitor with a token
        already has proof they received the original email, so a more
        specific message here doesn't reveal anything they don't already
        know.
        """
        return self._fetch_one_dict(
            "SELECT * FROM password_reset_tokens WHERE token_hash = ?", (token_hash,)
        )

    def get_valid_token(self, token_hash: str, now_iso: str) -> dict[str, Any] | None:
        """Return the token record for ``token_hash`` if it is still valid.

        "Valid" means: exists, has not already been used, and has not
        expired as of ``now_iso``. Returns None otherwise — this
        collapses "no such token", "already used", and "expired" into
        one outcome, since callers should present the same generic
        error for all three (never reveal which case applied).

        Args:
            token_hash: The one-way hash of the raw token being verified
                (never the raw token itself).
            now_iso: The current time (ISO datetime string), passed in
                by the caller rather than computed here so this method
                stays pure data access with no wall-clock dependency.
        """
        return self._fetch_one_dict(
            """
            SELECT * FROM password_reset_tokens
            WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
            """,
            (token_hash, now_iso),
        )

    def mark_used(self, token_id: int, used_at: str) -> bool:
        """Mark a token as used (consumed), so it can never be replayed.

        Args:
            token_id: Id of the token record to mark used.
            used_at: ISO datetime string recording when it was used.

        Returns:
            True if a row was updated, False if no matching, still-unused
            token was found (e.g. it was already marked used elsewhere).
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "UPDATE password_reset_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (used_at, token_id),
            )
        marked = cursor.rowcount > 0
        if marked:
            logger.info("Marked password reset token id=%s as used", token_id)
        return marked

    def delete_expired(self, now_iso: str) -> int:
        """Delete every token whose ``expires_at`` has passed.

        Args:
            now_iso: The current time (ISO datetime string) — tokens
                with ``expires_at <= now_iso`` are deleted regardless of
                whether they were ever used.

        Returns:
            The number of rows deleted.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM password_reset_tokens WHERE expires_at <= ?", (now_iso,)
            )
        deleted = cursor.rowcount
        if deleted:
            logger.info("Deleted %d expired password reset token(s)", deleted)
        return deleted

    def delete_by_id(self, token_id: int) -> bool:
        """Delete a single token record by id.

        Used by ``AuthService``'s enumeration-safety decoy path (a
        throwaway row inserted and immediately removed, to keep the
        "no such account" request roughly as DB-write-expensive as the
        real path's single insert — see
        ``AuthService._dummy_reset_token_work``) — not part of the
        normal token lifecycle otherwise.

        Returns:
            True if a row was removed, False if no matching id was found.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM password_reset_tokens WHERE id = ?", (token_id,)
            )
        return cursor.rowcount > 0
