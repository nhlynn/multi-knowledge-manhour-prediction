"""Raw SQL data access for the ``temp_stashes`` table.

No business logic lives here — that belongs in
``scheduler.temp_data_service.TempDataService``. This module only knows
how to turn rows into dicts and back.
"""

import logging
import sqlite3
from typing import Any

from database.db import ensure_schema, get_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS temp_stashes (
    id TEXT PRIMARY KEY,
    stash_type TEXT NOT NULL DEFAULT 'preview',
    project_name TEXT,
    created_by TEXT,
    project_remark TEXT,
    json_data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_temp_stashes_expires_at ON temp_stashes(expires_at);
CREATE INDEX IF NOT EXISTS idx_temp_stashes_stash_type ON temp_stashes(stash_type);
"""


class TempRepository:
    """Repository for CRUD access to the ``temp_stashes`` table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        ensure_schema(self._conn(), _SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    def insert(self, record: dict[str, Any]) -> None:
        """Insert a new stash row.

        Args:
            record: Must contain id, stash_type, project_name, created_by,
                project_remark, json_data, created_at, expires_at.
        """
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO temp_stashes
                    (id, stash_type, project_name, created_by, project_remark,
                     json_data, created_at, expires_at)
                VALUES (:id, :stash_type, :project_name, :created_by, :project_remark,
                        :json_data, :created_at, :expires_at)
                """,
                record,
            )
        logger.debug("Inserted temp stash id=%s", record.get("id"))

    def get_by_id(self, stash_id: str) -> sqlite3.Row | None:
        """Return a single stash row by id, or None if not found."""
        return self._conn().execute(
            "SELECT * FROM temp_stashes WHERE id = ?", (stash_id,)
        ).fetchone()

    def exists(self, stash_id: str) -> bool:
        """Return whether a stash with this id exists."""
        return self.get_by_id(stash_id) is not None

    def list_all(self, stash_type: str | None = None) -> list[sqlite3.Row]:
        """Return all stashes, newest first, optionally filtered by type."""
        if stash_type is None:
            return self._conn().execute(
                "SELECT * FROM temp_stashes ORDER BY created_at DESC"
            ).fetchall()
        return self._conn().execute(
            "SELECT * FROM temp_stashes WHERE stash_type = ? ORDER BY created_at DESC",
            (stash_type,),
        ).fetchall()

    def list_page(
        self,
        *,
        stash_type: str | None = None,
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

        conn = self._conn()
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM temp_stashes {where_clause}", params
        ).fetchone()["c"]

        offset = max(page - 1, 0) * per_page
        rows = conn.execute(
            f"""
            SELECT * FROM temp_stashes {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()
        return rows, total

    def delete(self, stash_id: str) -> bool:
        """Delete a stash by id. Returns True if a row was removed."""
        conn = self._conn()
        with conn:
            cursor = conn.execute("DELETE FROM temp_stashes WHERE id = ?", (stash_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("Deleted temp stash id=%s", stash_id)
        return deleted

    def delete_older_than(self, cutoff_iso: str) -> list[sqlite3.Row]:
        """Delete stashes with created_at before cutoff_iso, returning the deleted rows."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM temp_stashes WHERE created_at < ? ORDER BY created_at ASC",
            (cutoff_iso,),
        ).fetchall()
        if rows:
            with conn:
                conn.execute("DELETE FROM temp_stashes WHERE created_at < ?", (cutoff_iso,))
        return rows

    def clear_expired(self, now_iso: str) -> list[sqlite3.Row]:
        """Delete stashes whose expires_at has passed, returning the deleted rows."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM temp_stashes WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now_iso,),
        ).fetchall()
        if rows:
            with conn:
                conn.execute(
                    "DELETE FROM temp_stashes WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now_iso,),
                )
        return rows

    def count(self) -> int:
        """Return the total number of stash rows."""
        row = self._conn().execute("SELECT COUNT(*) AS c FROM temp_stashes").fetchone()
        return row["c"]
