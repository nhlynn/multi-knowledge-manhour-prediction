"""Temporary data service for MHES.

Stores Preview stashes (created when starting a new AI Chatbot session
with Preview data pending) in a SQLite database on the server, so they
survive closing the browser.

Team-aware Temporary Data: every stash belongs to exactly one team
(``team_id``) -- previously this store was shared across every team
regardless of who created a stash (a known gap; every team's temp
stashes were visible to every other team). Every read/write method
accepts an optional ``team_id`` so callers can enforce "see only your
own team's stashes, unless you're Admin" (see ``routes/preview.py``'s
own ``_team_id_filter``, mirroring ``routes/export.py``'s identical
pattern for Export History).

This class is the only supported way to read/write Preview stashes —
no other module should touch the repository or SQLite connection
directly.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from repositories.temp_repository import TempRepository

logger = logging.getLogger(__name__)


def _row_to_stash(row) -> dict[str, Any]:
    """Convert a temp_stashes SQLite row back into the legacy stash dict shape."""
    data = json.loads(row["json_data"])
    return {
        "id": row["id"],
        "stashedAt": row["created_at"],
        "projectName": row["project_name"] or "",
        "createdBy": row["created_by"] or "",
        "categories": data.get("categories", []),
        "totals": data.get("totals", {}),
    }


class TempDataService:
    """Service for reading/writing server-side Preview stashes (SQLite-backed)."""

    def __init__(self, db_path: str) -> None:
        """Initialize the service.

        Args:
            db_path: Path to the shared MHES SQLite database.
        """
        self.db_path = db_path
        self._repo = TempRepository(self.db_path)

    def list_stashes(self, *, team_id: int | None = None) -> list[dict[str, Any]]:
        """Return all stashed Preview snapshots, newest first.

        Args:
            team_id: Only return stashes belonging to this team, if
                given (None means "no filter", i.e. Admin).
        """
        return [
            _row_to_stash(row)
            for row in self._repo.list_all(stash_type="preview", team_id=team_id)
        ]

    def list_stashes_page(
        self,
        *,
        team_id: int | None = None,
        page: int = 1,
        per_page: int = 20,
        from_date: str | None = None,
        to_date: str | None = None,
        project_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one page of stashed Preview snapshots, newest first, plus the total count.

        Args:
            team_id: Only include stashes belonging to this team, if
                given (None means "no filter", i.e. Admin).

        See ``TempRepository.list_page`` for how filtering/pagination is
        applied in SQL.
        """
        rows, total = self._repo.list_page(
            stash_type="preview",
            team_id=team_id,
            page=page,
            per_page=per_page,
            from_date=from_date,
            to_date=to_date,
            project_name=project_name,
        )
        return [_row_to_stash(row) for row in rows], total

    def get_by_key(self, stash_id: str, *, team_id: int | None = None) -> dict[str, Any] | None:
        """Return a single stash by id, or None if not found (or not
        owned by ``team_id``, if given -- None means "no filter", i.e.
        Admin)."""
        row = self._repo.get_by_id(stash_id, team_id=team_id)
        return _row_to_stash(row) if row is not None else None

    def exists(self, stash_id: str, *, team_id: int | None = None) -> bool:
        """Return whether a stash with this id exists (and, if
        ``team_id`` is given, belongs to that team)."""
        return self._repo.exists(stash_id, team_id=team_id)

    def add_stash(
        self,
        categories: list[dict[str, Any]],
        totals: dict[str, Any],
        project_name: str,
        created_by: str = "",
        *,
        team_id: int,
    ) -> dict[str, Any]:
        """Create a new stash and persist it.

        Args:
            categories: Category → Task → Activity structure from Preview.
            totals: Preview totals at the time of stashing.
            project_name: Project name entered on Preview, if any.
            created_by: Created By entered on Preview, if any.
            team_id: The team this stash belongs to -- always the
                creating user's own team (never None/"every team",
                even for an Admin; that's only ever a read-side
                filter, see ``routes/preview.py``'s ``_team_id_filter``).

        Returns:
            The newly created stash record.
        """
        stash_id = uuid.uuid4().hex
        created_at = datetime.now().isoformat()
        record = {
            "id": stash_id,
            "stash_type": "preview",
            "project_name": project_name or "",
            "created_by": created_by or "",
            # project_remark: retained as an always-empty column on the
            # existing temp_stashes table rather than a schema migration
            # -- Project Remark itself was removed as a feature (no
            # longer collected from Preview at all), but the column
            # stays for now since dropping it isn't worth the migration
            # risk for a field that simply always writes "" going forward.
            "project_remark": "",
            "json_data": json.dumps(
                {"categories": categories, "totals": totals or {}}, ensure_ascii=False
            ),
            "team_id": team_id,
            "created_at": created_at,
            "expires_at": None,
        }
        self._repo.insert(record)
        logger.info(
            "Saved temp stash id=%s projectName=%r team_id=%s", stash_id, project_name, team_id,
        )
        return _row_to_stash(self._repo.get_by_id(stash_id))

    def remove_stash(self, stash_id: str, *, team_id: int | None = None) -> bool:
        """Remove a stash by id.

        Args:
            stash_id: Id of the stash to remove.
            team_id: If given, only remove the stash if it belongs to
                this team (None means "no filter", i.e. Admin) --
                otherwise a team could delete another team's stash by
                guessing/reusing its id.

        Returns:
            True if a stash was removed, False if no match was found.
        """
        removed = self._repo.delete(stash_id, team_id=team_id)
        if removed:
            logger.info("Deleted temp stash id=%s", stash_id)
        return removed

    def remove_older_than(self, days: int) -> list[dict[str, Any]]:
        """Remove stashes older than the given number of days.

        Compares against ``created_at``, which is recorded via
        ``datetime.now()`` (naive, server-local time), so this uses the
        same naive local-time basis for the cutoff.

        Args:
            days: Age threshold in days; stashes older than this are removed.

        Returns:
            The list of removed stash records (for logging purposes).
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self._repo.delete_older_than(cutoff)
        removed = [_row_to_stash(row) for row in rows]
        if removed:
            logger.info("Cleanup removed %d expired temp stash(es).", len(removed))
        return removed