"""Raw SQL data access for the ``teams`` table.

No business logic lives here — that belongs to whatever service/migration
calls this repository (mirrors the split used by
``repositories.temp_repository.TempRepository``). This module only knows
how to create/read team rows.
"""

import logging
import sqlite3
from typing import Any

from database.db import ensure_schema, get_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_teams_slug ON teams(slug);
"""


class TeamRepository:
    """Repository for CRUD access to the ``teams`` table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        ensure_schema(self._conn(), _SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    def insert(self, *, name: str, slug: str, created_at: str) -> dict[str, Any]:
        """Insert a new team row.

        Args:
            name: Display name, e.g. "Infrastructure Team".
            slug: Unique, URL/path-safe identifier, e.g. "infrastructure-team".
            created_at: ISO datetime string.

        Returns:
            The newly created team record.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "INSERT INTO teams (name, slug, created_at) VALUES (?, ?, ?)",
                (name, slug, created_at),
            )
        record = self.get_by_id(cursor.lastrowid)
        assert record is not None
        logger.info("Created team id=%s name=%r slug=%r", cursor.lastrowid, name, slug)
        return record

    def get_by_id(self, team_id: int) -> dict[str, Any] | None:
        """Return a single team by id, or None if not found."""
        row = self._conn().execute(
            "SELECT * FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Return a single team by slug, or None if not found."""
        row = self._conn().execute(
            "SELECT * FROM teams WHERE slug = ?", (slug,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_all(self) -> list[dict[str, Any]]:
        """Return all teams, oldest first."""
        rows = self._conn().execute(
            "SELECT * FROM teams ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]
