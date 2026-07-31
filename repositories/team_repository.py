"""Raw SQL data access for the ``teams`` table.

No business logic lives here — that belongs to whatever service/migration
calls this repository (mirrors the split used by
``repositories.temp_repository.TempRepository``). This module only knows
how to create/read team rows.
"""

import logging
import sqlite3
from typing import Any

from repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active', 'Inactive'))
);
CREATE INDEX IF NOT EXISTS idx_teams_slug ON teams(slug);
"""


class TeamRepository(BaseRepository):
    """Repository for CRUD access to the ``teams`` table."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)
        self._ensure_description_and_status_columns()
        self._ensure_name_unique_index()

    def _ensure_description_and_status_columns(self) -> None:
        """Add ``description``/``status`` for databases created before
        they existed.

        Mirrors ``UserRepository._ensure_email_column`` — ``CREATE TABLE
        IF NOT EXISTS`` in ``_SCHEMA`` only applies to brand new
        databases, so an existing ``teams`` table needs an explicit
        ALTER. Existing rows get ``description = NULL`` (optional field,
        nothing to backfill) and ``status = 'Active'`` (via the column's
        own DEFAULT, so every pre-existing team starts Active rather
        than an ambiguous NULL). Safe to run on every service
        construction — both ALTERs are no-ops once applied.
        """
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(teams)")}
        if "description" not in columns:
            conn.execute("ALTER TABLE teams ADD COLUMN description TEXT")
            logger.info("Added description column to teams table.")
        if "status" not in columns:
            conn.execute(
                "ALTER TABLE teams ADD COLUMN status TEXT NOT NULL DEFAULT 'Active' "
                "CHECK(status IN ('Active', 'Inactive'))"
            )
            logger.info("Added status column to teams table (existing rows defaulted to 'Active').")

    def _ensure_name_unique_index(self) -> None:
        """Add a case-insensitive UNIQUE index on ``name``.

        Defense-in-depth against a create/rename race slipping two
        same-named teams past the application-level uniqueness check
        (``name_exists``, used by ``services.team_service``) — that
        check-then-insert is not atomic, so this is what actually
        prevents a duplicate under concurrent requests, the same way
        the ``slug`` column's own ``UNIQUE`` constraint already does
        for Team Code.

        Guarded in a try/except: if some environment's database
        already has duplicate names (shouldn't happen given the
        application-level check, but data can predate it), creating
        this index fails — logged as a warning rather than crashing
        the app on every startup.
        """
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_name_unique "
                    "ON teams(name COLLATE NOCASE)"
                )
        except sqlite3.IntegrityError:
            logger.warning(
                "Could not create unique index on teams.name — duplicate team names "
                "already exist in this database. Application-level uniqueness checks "
                "still apply, but the database itself won't enforce it until the "
                "existing duplicates are resolved."
            )

    def insert(
        self,
        *,
        name: str,
        slug: str,
        created_at: str,
        description: str | None = None,
        status: str = "Active",
    ) -> dict[str, Any]:
        """Insert a new team row.

        Args:
            name: Display name, e.g. "Infrastructure Team".
            slug: Unique, URL/path-safe identifier, e.g. "infrastructure-team".
            created_at: ISO datetime string.
            description: Optional free-text description.
            status: 'Active' or 'Inactive' (matches the table's CHECK
                constraint). Defaults to 'Active', same as the column's
                own DEFAULT, for callers (e.g. ``utils/migrations/team_seed.py``)
                that don't pass it.

        Returns:
            The newly created team record.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "INSERT INTO teams (name, slug, created_at, description, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, slug, created_at, description, status),
            )
        record = self.get_by_id(cursor.lastrowid)
        assert record is not None
        logger.info("Created team id=%s name=%r slug=%r", cursor.lastrowid, name, slug)
        return record

    def delete(self, team_id: int) -> bool:
        """Delete a team row by id. Returns True if a row was removed.

        No dependency/safety checks here — that's
        ``services.team_service.delete_team``'s job (checking Users,
        Knowledge Base, and Export History before ever calling this).
        This method only knows how to remove the row itself.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted team id=%s", team_id)
        return deleted

    def update(
        self, team_id: int, *, name: str, slug: str, description: str | None, status: str,
    ) -> dict[str, Any]:
        """Update an existing team's editable fields.

        Callers needing to keep ``slug`` unchanged must pass its
        current value back in — this method has no notion of "leave
        as-is"; that decision (whether the slug is still safe to
        change at all) belongs to ``services.team_service.update_team``.

        Args:
            team_id: The team to update.
            name: New display name.
            slug: New slug (or the existing one, if unchanged).
            description: New description.
            status: 'Active' or 'Inactive'.

        Returns:
            The updated team record.
        """
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE teams SET name = ?, slug = ?, description = ?, status = ? WHERE id = ?",
                (name, slug, description, status, team_id),
            )
        record = self.get_by_id(team_id)
        assert record is not None
        logger.info("Updated team id=%s name=%r slug=%r status=%r", team_id, name, slug, status)
        return record

    def get_by_id(self, team_id: int) -> dict[str, Any] | None:
        """Return a single team by id, or None if not found."""
        return self._fetch_one_dict("SELECT * FROM teams WHERE id = ?", (team_id,))

    def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Return a single team by slug, or None if not found."""
        return self._fetch_one_dict("SELECT * FROM teams WHERE slug = ?", (slug,))

    def list_all(self) -> list[dict[str, Any]]:
        """Return all teams, oldest first."""
        return self._fetch_all_dicts("SELECT * FROM teams ORDER BY created_at ASC")

    def list_page(
        self,
        *,
        name: str | None = None,
        code: str | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one page of teams, oldest first, plus the total matching count.

        Filters and pagination are applied in SQL (WHERE + LIMIT/OFFSET),
        mirroring ``repositories.temp_repository.TempRepository.list_page``.

        Args:
            name: Case-insensitive substring match against ``name``, if given.
            code: Case-insensitive substring match against ``slug`` (the
                "Team Code"), if given. Independent of ``name`` — both
                apply together (AND) when both are given.
            status: Only include teams with this exact ``status``
                ('Active'/'Inactive'), if given.
            page: 1-based page number.
            per_page: Number of rows per page.

        Returns:
            ``(rows, total_matching_count)``.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if name:
            conditions.append("LOWER(name) LIKE ?")
            params.append(f"%{name.lower()}%")
        if code:
            conditions.append("LOWER(slug) LIKE ?")
            params.append(f"%{code.lower()}%")
        if status:
            conditions.append("status = ?")
            params.append(status)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        total = self._fetch_one(
            f"SELECT COUNT(*) AS c FROM teams {where_clause}", tuple(params)
        )["c"]

        offset = max(page - 1, 0) * per_page
        rows = self._fetch_all_dicts(
            f"""
            SELECT * FROM teams {where_clause}
            ORDER BY created_at ASC
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        return rows, total

    def name_exists(self, name: str, *, exclude_id: int | None = None) -> bool:
        """Return whether a team with this name already exists (case-insensitive).

        Args:
            name: Display name to check.
            exclude_id: If given, ignore the row with this id — for
                validating an edit against every *other* team's name.
        """
        if exclude_id is None:
            return self._fetch_one(
                "SELECT 1 FROM teams WHERE LOWER(name) = ?", (name.lower(),)
            ) is not None
        return self._fetch_one(
            "SELECT 1 FROM teams WHERE LOWER(name) = ? AND id != ?",
            (name.lower(), exclude_id),
        ) is not None

    def slug_exists(self, slug: str, *, exclude_id: int | None = None) -> bool:
        """Return whether a team with this slug already exists (case-insensitive).

        Args:
            slug: Slug to check.
            exclude_id: If given, ignore the row with this id — for
                validating an edit against every *other* team's slug.
        """
        if exclude_id is None:
            return self._fetch_one(
                "SELECT 1 FROM teams WHERE LOWER(slug) = ?", (slug.lower(),)
            ) is not None
        return self._fetch_one(
            "SELECT 1 FROM teams WHERE LOWER(slug) = ? AND id != ?",
            (slug.lower(), exclude_id),
        ) is not None
