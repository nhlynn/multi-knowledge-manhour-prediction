"""Raw SQL data access for the ``users`` table.

No business logic (password hashing/verification, session handling)
lives here — that belongs to ``services.auth_service.AuthService`` and
``routes/auth.py`` respectively. Mirrors the style of
``repositories/team_repository.py`` and ``repositories/temp_repository.py``.
"""

import logging
import sqlite3
from typing import Any

from database.db import ensure_schema, get_connection

logger = logging.getLogger(__name__)

VALID_ROLES = ("Admin", "Team Manager", "Member")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    role TEXT NOT NULL CHECK(role IN ('Admin', 'Team Manager', 'Member')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_team_id ON users(team_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
"""


class UserRepository:
    """Repository for CRUD access to the ``users`` table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        ensure_schema(self._conn(), _SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    def insert(
        self,
        *,
        username: str,
        password_hash: str,
        team_id: int,
        role: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Insert a new user row.

        Args:
            username: Unique login name.
            password_hash: Hashed password (never pass plaintext here).
            team_id: Id of the team this user belongs to.
            role: One of ``"Admin"``, ``"Team Manager"``, ``"Member"``.
            created_at: ISO datetime string.

        Returns:
            The newly created user record.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO users (username, password_hash, team_id, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, password_hash, team_id, role, created_at),
            )
        record = self.get_by_id(cursor.lastrowid)
        assert record is not None
        logger.info(
            "Created user id=%s username=%r role=%r team_id=%s",
            cursor.lastrowid, username, role, team_id,
        )
        return record

    def get_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Return a single user by id, or None if not found."""
        row = self._conn().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        """Return a single user by username, or None if not found."""
        row = self._conn().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_by_team(self, team_id: int) -> list[dict[str, Any]]:
        """Return all users belonging to a team, oldest first."""
        rows = self._conn().execute(
            "SELECT * FROM users WHERE team_id = ? ORDER BY created_at ASC",
            (team_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_all(self) -> list[dict[str, Any]]:
        """Return all users, oldest first."""
        rows = self._conn().execute(
            "SELECT * FROM users ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]
