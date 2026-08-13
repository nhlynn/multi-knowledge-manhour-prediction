"""Raw SQL data access for the ``temp_stashes`` table.

No business logic lives here — that belongs in
``scheduler.temp_data_service.TempDataService``. This module only knows
how to turn rows into dicts and back.
"""

import logging
import sqlite3
from typing import Any

from repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS temp_stashes (
    id TEXT PRIMARY KEY,
    stash_type TEXT NOT NULL DEFAULT 'preview',
    project_name TEXT,
    created_by TEXT,
    project_remark TEXT,
    json_data TEXT NOT NULL,
    team_id INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_temp_stashes_expires_at ON temp_stashes(expires_at);
CREATE INDEX IF NOT EXISTS idx_temp_stashes_stash_type ON temp_stashes(stash_type);
CREATE INDEX IF NOT EXISTS idx_temp_stashes_created_at ON temp_stashes(created_at);
"""
# idx_temp_stashes_team_id is intentionally NOT in _SCHEMA: ensure_schema()
# runs this whole script unconditionally, and on a pre-team-scoping
# database the CREATE TABLE IF NOT EXISTS above is a no-op (table
# already exists without team_id), so a CREATE INDEX ON
# temp_stashes(team_id) here would fail with "no such column: team_id".
# _ensure_team_id_column() below creates this index only after
# confirming/adding the column via ALTER -- same pattern as
# services/export_history_service.py's own _ensure_team_columns.


class TempRepository(BaseRepository):
    """Repository for CRUD access to the ``temp_stashes`` table."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)
        self._ensure_team_id_column(self._conn())

    def _ensure_team_id_column(self, conn: sqlite3.Connection) -> None:
        """Add ``team_id`` for databases created before team-scoping.

        Mirrors ``services/export_history_service.py``'s own
        ``_ensure_team_columns`` -- ``CREATE TABLE IF NOT EXISTS`` only
        applies to brand new databases, so an existing ``temp_stashes``
        table (from before Temporary Data was team-scoped) needs an
        explicit ALTER. Existing rows (which predate any team concept,
        and were visible to every team -- the bug this fixes) are then
        backfilled onto the default team
        (``utils.migrations.DEFAULT_TEAM_SLUG``), same as
        ``export_history``'s own legacy rows. Safe to run on every
        repository construction: the ALTER is a no-op once the column
        exists, and the backfill only ever touches rows still missing
        a ``team_id``.
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(temp_stashes)")}
        if "team_id" not in columns:
            conn.execute("ALTER TABLE temp_stashes ADD COLUMN team_id INTEGER")
            logger.info("Added team_id column to temp_stashes table.")

        # Created here (not in _SCHEMA), and unconditionally (not only
        # in the branch above), so it's created exactly once whether
        # the column just got ALTERed in on a legacy database or
        # already existed from a fresh install's CREATE TABLE.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_temp_stashes_team_id ON temp_stashes(team_id)"
        )

        orphaned = conn.execute(
            "SELECT COUNT(*) AS c FROM temp_stashes WHERE team_id IS NULL"
        ).fetchone()["c"]
        if not orphaned:
            return

        from repositories.team_repository import TeamRepository
        from utils.migrations import DEFAULT_TEAM_SLUG

        team = TeamRepository(self.db_path).get_by_slug(DEFAULT_TEAM_SLUG)
        if team is None:
            logger.warning(
                "%d temp_stashes row(s) have no team_id and the default team "
                "does not exist yet; will retry the backfill on next use.",
                orphaned,
            )
            return

        with conn:
            conn.execute(
                "UPDATE temp_stashes SET team_id = ? WHERE team_id IS NULL",
                (team["id"],),
            )
        logger.info(
            "Backfilled %d pre-team-scoping temp_stashes row(s) onto team_id=%s (%s).",
            orphaned, team["id"], team["name"],
        )

    def insert(self, record: dict[str, Any]) -> None:
        """Insert a new stash row.

        Args:
            record: Must contain id, stash_type, project_name, created_by,
                project_remark, json_data, team_id, created_at, expires_at.
        """
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO temp_stashes
                    (id, stash_type, project_name, created_by, project_remark,
                     json_data, team_id, created_at, expires_at)
                VALUES (:id, :stash_type, :project_name, :created_by, :project_remark,
                        :json_data, :team_id, :created_at, :expires_at)
                """,
                record,
            )
        logger.debug("Inserted temp stash id=%s team_id=%s", record.get("id"), record.get("team_id"))

    def get_by_id(self, stash_id: str, *, team_id: int | None = None) -> sqlite3.Row | None:
        """Return a single stash row by id, or None if not found.

        Args:
            stash_id: The stash's id.
            team_id: If given, only return the row if it belongs to
                this team -- a stash id from another team is treated
                identically to a nonexistent one (None means "no
                filter", i.e. Admin; see ``routes/preview.py``'s own
                ``_team_id_filter``).
        """
        if team_id is None:
            return self._fetch_one("SELECT * FROM temp_stashes WHERE id = ?", (stash_id,))
        return self._fetch_one(
            "SELECT * FROM temp_stashes WHERE id = ? AND team_id = ?", (stash_id, team_id),
        )

    def exists(self, stash_id: str, *, team_id: int | None = None) -> bool:
        """Return whether a stash with this id exists (and, if
        ``team_id`` is given, belongs to that team)."""
        return self.get_by_id(stash_id, team_id=team_id) is not None

    def list_all(
        self, stash_type: str | None = None, *, team_id: int | None = None,
    ) -> list[sqlite3.Row]:
        """Return all stashes, newest first, optionally filtered by type and team."""
        conditions = []
        params: list[Any] = []
        if stash_type is not None:
            conditions.append("stash_type = ?")
            params.append(stash_type)
        if team_id is not None:
            conditions.append("team_id = ?")
            params.append(team_id)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return self._fetch_all(
            f"SELECT * FROM temp_stashes {where_clause} ORDER BY created_at DESC", params,
        )

    def list_page(
        self,
        *,
        stash_type: str | None = None,
        team_id: int | None = None,
        page: int = 1,
        per_page: int = 20,
        from_date: str | None = None,
        to_date: str | None = None,
        project_name: str | None = None,
    ) -> tuple[list[sqlite3.Row], int]:
        """Return one page of stashes, newest first, plus the total matching count.

        Filters and pagination are applied in SQL (WHERE + LIMIT/OFFSET) so
        only the rows needed for the requested page are ever read out of
        the database.

        Args:
            stash_type: Only include stashes of this type, if given.
            team_id: Only include stashes belonging to this team, if
                given (None means "no filter", i.e. Admin -- see
                ``routes/preview.py``'s own ``_team_id_filter``).
            page: 1-based page number.
            per_page: Number of rows per page.
            from_date: Only include rows with a created_at date (``yyyy-mm-dd``,
                taken from the leading 10 characters of ``created_at``) on
                or after this date.
            to_date: Only include rows with a created_at date on or before
                this date.
            project_name: Case-insensitive substring match against
                ``project_name``.

        Returns:
            A tuple of ``(rows, total_count)`` where ``total_count`` is the
            number of matching rows across all pages.
        """
        conditions = []
        params: list[Any] = []
        if stash_type is not None:
            conditions.append("stash_type = ?")
            params.append(stash_type)
        if team_id is not None:
            conditions.append("team_id = ?")
            params.append(team_id)
        if from_date:
            conditions.append("substr(created_at, 1, 10) >= ?")
            params.append(from_date)
        if to_date:
            conditions.append("substr(created_at, 1, 10) <= ?")
            params.append(to_date)
        if project_name:
            conditions.append("LOWER(project_name) LIKE ?")
            params.append(f"%{project_name.lower()}%")
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        total = self._fetch_one(
            f"SELECT COUNT(*) AS c FROM temp_stashes {where_clause}", params
        )["c"]

        offset = max(page - 1, 0) * per_page
        rows = self._fetch_all(
            f"""
            SELECT * FROM temp_stashes {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        )
        return rows, total

    def delete(self, stash_id: str, *, team_id: int | None = None) -> bool:
        """Delete a stash by id. Returns True if a row was removed.

        Args:
            stash_id: The stash's id.
            team_id: If given, only delete the row if it belongs to
                this team (None means "no filter", i.e. Admin) --
                otherwise a team could delete another team's stash by
                guessing/reusing its id.
        """
        conn = self._conn()
        with conn:
            if team_id is None:
                cursor = conn.execute("DELETE FROM temp_stashes WHERE id = ?", (stash_id,))
            else:
                cursor = conn.execute(
                    "DELETE FROM temp_stashes WHERE id = ? AND team_id = ?", (stash_id, team_id),
                )
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("Deleted temp stash id=%s", stash_id)
        return deleted

    def delete_older_than(self, cutoff_iso: str) -> list[sqlite3.Row]:
        """Delete stashes with created_at before cutoff_iso, returning the deleted rows."""
        rows = self._fetch_all(
            "SELECT * FROM temp_stashes WHERE created_at < ? ORDER BY created_at ASC",
            (cutoff_iso,),
        )
        if rows:
            conn = self._conn()
            with conn:
                conn.execute("DELETE FROM temp_stashes WHERE created_at < ?", (cutoff_iso,))
        return rows

    def clear_expired(self, now_iso: str) -> list[sqlite3.Row]:
        """Delete stashes whose expires_at has passed, returning the deleted rows."""
        rows = self._fetch_all(
            "SELECT * FROM temp_stashes WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now_iso,),
        )
        if rows:
            conn = self._conn()
            with conn:
                conn.execute(
                    "DELETE FROM temp_stashes WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now_iso,),
                )
        return rows

    def count(self) -> int:
        """Return the total number of stash rows."""
        return self._fetch_one("SELECT COUNT(*) AS c FROM temp_stashes")["c"]