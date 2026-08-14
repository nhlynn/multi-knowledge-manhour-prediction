"""Export History service for MHES.

Stores metadata about generated Excel exports (project name, created by,
file location/size, task/hour totals) in a dedicated SQLite database, so
the Export History page can be rendered from a fast metadata lookup
instead of re-scanning and re-reading every Excel file in the exports
folder on every page load.

The actual Excel files are untouched by this module — it only records
*where* a file is and *what* it contains, never moves/copies/deletes it.
No other module should open the export history database directly; go
through this service.

Team-aware Export History (Phase 6 of multi-team support): every export
record belongs to exactly one team (``team_id``) and, where known, the
actual authenticated user who triggered it (``created_by_user_id`` —
distinct from ``created_by``, a free-text name typed into the Preview
form that may not match any real account). Every read method accepts an
optional ``team_id`` filter so callers can enforce "see only your own
team's exports, unless you're Admin" (see ``routes/export.py``).
"""

import logging
import sqlite3
from datetime import datetime
from typing import Any

from database.db import ensure_schema, get_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS export_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT,
    created_by TEXT,
    created_by_user_id INTEGER,
    team_id INTEGER,
    export_date TEXT,
    file_name TEXT NOT NULL,
    file_url TEXT,
    file_path TEXT,
    file_size INTEGER,
    total_tasks INTEGER,
    total_hours REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_export_history_created_at ON export_history(created_at);
CREATE INDEX IF NOT EXISTS idx_export_history_file_name ON export_history(file_name);
"""
# idx_export_history_team_id is intentionally NOT in _SCHEMA: ensure_schema()
# runs this whole script unconditionally, and on a pre-Phase-6 database the
# CREATE TABLE IF NOT EXISTS above is a no-op (table already exists without
# team_id), so a CREATE INDEX ON export_history(team_id) here would fail
# with "no such column: team_id". _ensure_team_columns() below creates this
# index only after confirming/adding the column via ALTER.


class ExportHistoryService:
    """Service for reading/writing Export History metadata (SQLite-backed)."""

    def __init__(self, db_path: str) -> None:
        """Initialize the service.

        Args:
            db_path: Path to the export history SQLite database file.
        """
        self.db_path = db_path
        conn = self._conn()
        ensure_schema(conn, _SCHEMA)
        self._ensure_file_path_column(conn)
        self._ensure_team_columns(conn)

    def _conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    @staticmethod
    def _ensure_file_path_column(conn: sqlite3.Connection) -> None:
        """Add the ``file_path`` column for databases created before it existed.

        ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` only applies to brand
        new databases, so an existing ``export_history`` table (from
        before this column was introduced) needs an explicit ALTER.
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(export_history)")}
        if "file_path" not in columns:
            conn.execute("ALTER TABLE export_history ADD COLUMN file_path TEXT")
            logger.info("Added file_path column to export_history table.")

    def _ensure_team_columns(self, conn: sqlite3.Connection) -> None:
        """Add ``team_id``/``created_by_user_id`` for databases created before Phase 6.

        Mirrors ``_ensure_file_path_column`` — ``CREATE TABLE IF NOT EXISTS``
        only applies to brand new databases, so an existing table needs an
        explicit ALTER. Existing rows (which predate any team concept) are
        then backfilled onto the default team
        (``utils.migrations.DEFAULT_TEAM_SLUG``). Safe to run on every
        service construction: the ALTERs are no-ops once the columns
        exist, and the backfill only ever touches rows still missing a
        ``team_id``.
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(export_history)")}
        if "team_id" not in columns:
            conn.execute("ALTER TABLE export_history ADD COLUMN team_id INTEGER")
            logger.info("Added team_id column to export_history table.")
        if "created_by_user_id" not in columns:
            conn.execute("ALTER TABLE export_history ADD COLUMN created_by_user_id INTEGER")
            logger.info("Added created_by_user_id column to export_history table.")

        # Created here (not in _SCHEMA), and unconditionally (not only in
        # the branch above), so it's created exactly once whether the
        # column just got ALTERed in on a legacy database or already
        # existed from a fresh install's CREATE TABLE.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_export_history_team_id ON export_history(team_id)"
        )

        orphaned = conn.execute(
            "SELECT COUNT(*) AS c FROM export_history WHERE team_id IS NULL"
        ).fetchone()["c"]
        if not orphaned:
            return

        from repositories.team_repository import TeamRepository
        from utils.migrations import DEFAULT_TEAM_SLUG

        team = TeamRepository(self.db_path).get_by_slug(DEFAULT_TEAM_SLUG)
        if team is None:
            logger.warning(
                "%d export_history row(s) have no team_id and the default team "
                "does not exist yet; will retry the backfill on next use.",
                orphaned,
            )
            return

        with conn:
            conn.execute(
                "UPDATE export_history SET team_id = ? WHERE team_id IS NULL",
                (team["id"],),
            )
        logger.info(
            "Backfilled %d pre-Phase-6 export_history row(s) onto team_id=%s (%s).",
            orphaned, team["id"], team["name"],
        )

    def insert_history(
        self,
        *,
        project_name: str,
        created_by: str,
        team_id: int,
        export_date: str,
        file_name: str,
        file_url: str,
        file_size: int,
        total_tasks: int,
        total_hours: float,
        created_by_user_id: int | None = None,
        file_path: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Insert one export history record.

        Args:
            project_name: Project name entered on Preview.
            created_by: Created By entered on Preview (free-text; may not
                match any real account).
            team_id: Id of the team this export belongs to (Phase 6) —
                every export is scoped to the team of the user who
                created it.
            export_date: When the export was generated (ISO datetime string).
            file_name: Name of the generated Excel file (as saved on disk).
            file_url: URL for downloading the file (e.g. from ``url_for``).
            file_size: Size of the generated file, in bytes.
            total_tasks: Total number of tasks across all categories.
            total_hours: Total estimated hours across all tasks.
            created_by_user_id: Id of the actual logged-in user who
                triggered the export (Phase 6), distinct from the
                free-text ``created_by``. None for legacy/migrated rows
                where no authenticated user exists.
            file_path: Absolute path to the file in the local exports
                folder, recorded at export time for reference.
            created_at: Record creation timestamp (ISO datetime string).
                Defaults to now; only overridden when migrating existing
                records so their original ordering is preserved.

        Returns:
            The newly created history record.
        """
        conn = self._conn()
        created_at = created_at or datetime.now().isoformat()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO export_history
                    (project_name, created_by, created_by_user_id, team_id, export_date,
                     file_name, file_url, file_path, file_size, total_tasks, total_hours,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_name, created_by, created_by_user_id, team_id, export_date,
                    file_name, file_url, file_path, file_size, total_tasks, total_hours,
                    created_at,
                ),
            )
        record = self.get_history_by_id(cursor.lastrowid)
        logger.info(
            "Export history saved: id=%s file_name=%s project_name=%r team_id=%s",
            cursor.lastrowid, file_name, project_name, team_id,
        )
        return record

    def record_export_result(
        self,
        *,
        categories: list[dict[str, Any]],
        file_name: str,
        file_url: str,
        file_path: str,
        file_size: int,
        project_name: str,
        created_by: str,
        team_id: int,
        created_by_user_id: int | None,
    ) -> None:
        """Compute totals from the exported categories and save a history row.

        Best-effort: by the time this is called, the Excel file has
        already been uploaded successfully, so a failure here is only
        logged — it must never remove the uploaded file or fail the
        export response (see ``routes/export.py::export_excel``).

        Args:
            categories: The same Category → Task → Activity structure
                that was exported, used only to compute ``total_tasks``/
                ``total_hours`` for the history record.
            file_url: Download URL for the file (built by the caller via
                ``url_for``, since generating one requires an active
                Flask request/routing context this service doesn't have).
            Other args: see ``insert_history``.
        """
        try:
            total_tasks = sum(len(cat.get("tasks") or []) for cat in categories)
            total_hours = sum(
                (task.get("total_hours") or 0)
                for cat in categories
                for task in (cat.get("tasks") or [])
            )
            self.insert_history(
                project_name=project_name,
                created_by=created_by,
                created_by_user_id=created_by_user_id,
                team_id=team_id,
                export_date=datetime.now().isoformat(),
                file_name=file_name,
                file_url=file_url,
                file_path=file_path,
                file_size=file_size,
                total_tasks=total_tasks,
                total_hours=total_hours,
            )
        except Exception:
            logger.exception(
                "Failed to save export history for file=%s; the Excel file was still uploaded to GCS.",
                file_name,
            )

    def get_history(self, *, team_id: int | None = None) -> list[dict[str, Any]]:
        """Return all export history records, newest first.

        Args:
            team_id: If given, only return records belonging to this
                team. None (the default) returns records across every
                team — used by Admin views and global CLI/ops scripts
                (e.g. ``utils/migrate_exports_to_gcs.py``); callers that
                must respect per-team visibility pass this explicitly.
        """
        if team_id is None:
            rows = self._conn().execute(
                "SELECT * FROM export_history ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM export_history WHERE team_id = ? ORDER BY created_at DESC",
                (team_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_records_for_team(self, team_id: int) -> bool:
        """Return whether any export history record belongs to this team.

        Used by ``services.team_service.get_team_deletion_blockers`` to
        decide whether a team is safe to delete — an ``EXISTS`` query
        rather than ``len(get_history(...))`` so it doesn't need to
        materialize every row just to answer a yes/no question.
        """
        row = self._conn().execute(
            "SELECT EXISTS(SELECT 1 FROM export_history WHERE team_id = ?) AS found", (team_id,),
        ).fetchone()
        return bool(row["found"])

    def get_history_page(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        team_id: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        project_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one page of export history records, newest first, plus the total count.

        Filters and pagination are applied in SQL (WHERE + LIMIT/OFFSET) so
        only the records needed for the requested page are ever read out of
        the database.

        Args:
            page: 1-based page number.
            per_page: Number of records per page.
            team_id: If given, only include records belonging to this
                team (Phase 6). None returns records across every team —
                ``routes/export.py`` passes None for Admin with no Team
                filter chosen, or a specific team's id either for a
                non-Admin role or an Admin who narrowed the Team dropdown
                (see ``_team_id_list_filter``).
            from_date: Only include records with an export date (``yyyy-mm-dd``,
                taken from the leading 10 characters of ``export_date``) on
                or after this date.
            to_date: Only include records with an export date on or before
                this date.
            project_name: Case-insensitive substring match against
                ``project_name``.

        Returns:
            A tuple of ``(records, total_count)`` where ``total_count`` is
            the number of matching records across all pages (not just this
            page), needed to render pagination controls.
        """
        conditions = []
        params: list[Any] = []
        if team_id is not None:
            conditions.append("team_id = ?")
            params.append(team_id)
        if from_date:
            conditions.append("substr(export_date, 1, 10) >= ?")
            params.append(from_date)
        if to_date:
            conditions.append("substr(export_date, 1, 10) <= ?")
            params.append(to_date)
        if project_name:
            conditions.append("LOWER(project_name) LIKE ?")
            params.append(f"%{project_name.lower()}%")
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        conn = self._conn()
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM export_history {where_clause}", params
        ).fetchone()["c"]

        offset = max(page - 1, 0) * per_page
        rows = conn.execute(
            f"""
            SELECT * FROM export_history {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()
        return [dict(row) for row in rows], total

    def get_history_by_id(self, history_id: int) -> dict[str, Any] | None:
        """Return a single export history record by id, or None if not found."""
        row = self._conn().execute(
            "SELECT * FROM export_history WHERE id = ?", (history_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_history_by_file_name(
        self, file_name: str, *, team_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Return the most recent export history record for a file name, or None.

        Used by the download/view routes to look up where a file actually
        lives (``file_path`` — a GCS object path for records created after
        the storage migration, or a local absolute path for older ones)
        given only the filename from the URL.

        Args:
            file_name: filename from the URL.
            team_id: If given, only return the record if it belongs to
                this team (Phase 6) — this is the actual authorization
                boundary for download/view: a non-Admin request for
                another team's export simply finds nothing here, and the
                calling route treats that identically to "file not
                found" (see ``routes/export.py``). None (Admin) returns
                the record regardless of which team it belongs to.
        """
        if team_id is None:
            row = self._conn().execute(
                "SELECT * FROM export_history WHERE file_name = ? ORDER BY created_at DESC LIMIT 1",
                (file_name,),
            ).fetchone()
        else:
            row = self._conn().execute(
                """
                SELECT * FROM export_history
                WHERE file_name = ? AND team_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (file_name, team_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_file_path(self, history_id: int, file_path: str) -> bool:
        """Update a single record's ``file_path`` (and nothing else).

        Used by ``utils/migrate_exports_to_gcs.py`` to repoint a
        pre-migration record at its new GCS object path once the
        underlying file has been uploaded to the bucket.

        Args:
            history_id: Id of the history record to update.
            file_path: New value for ``file_path`` (a GCS object path).

        Returns:
            True if a record was updated, False if no match was found.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "UPDATE export_history SET file_path = ? WHERE id = ?",
                (file_path, history_id),
            )
        return cursor.rowcount > 0

    def delete_history(self, history_id: int) -> bool:
        """Delete a single export history record by id.

        Only removes the metadata row — never touches the Excel file itself.

        Args:
            history_id: Id of the history record to remove.

        Returns:
            True if a record was removed, False if no match was found.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute("DELETE FROM export_history WHERE id = ?", (history_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted export history id=%s", history_id)
        return deleted