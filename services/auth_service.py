"""Authentication service for MHES.

Handles password hashing/verification and credential checking against the
``users`` table. Session/cookie handling is not this module's job — that
lives in ``routes/auth.py`` (via Flask's built-in, ``SECRET_KEY``-signed
session, the same mechanism already used by flash messages elsewhere in
the app).
"""

from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from repositories.user_repository import UserRepository


class AuthService:
    """Service for verifying login credentials."""

    def __init__(self, db_path: str) -> None:
        """Initialize AuthService.

        Args:
            db_path: Path to the shared MHES SQLite database.
        """
        self.users = UserRepository(db_path)

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a plaintext password for storage."""
        return generate_password_hash(password)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """Verify a username/password pair against the ``users`` table.

        Args:
            username: Login name as submitted.
            password: Plaintext password as submitted.

        Returns:
            The user's record if the credentials are valid, else None.
        """
        user = self.users.get_by_username(username)
        if user is None:
            return None
        if not check_password_hash(user["password_hash"], password):
            return None
        return user
