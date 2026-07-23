"""Raw SQL data access for the ``team_import_configs`` table.

Stores each team's Excel column-role mapping (Phase 7 of multi-team
support) — see ``services/excel_parser.py::_map_columns`` for how a
mapping is consumed at parse time. No parsing logic lives here, only
CRUD, mirroring the style of ``repositories/team_repository.py`` and
``repositories/user_repository.py``.
"""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from database.db import ensure_schema, get_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_import_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL UNIQUE REFERENCES teams(id),
    column_mapping TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_import_configs_team_id ON team_import_configs(team_id);
"""


class TeamImportConfigRepository:
    """Repository for CRUD access to the ``team_import_configs`` table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        ensure_schema(self._conn(), _SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    def upsert(self, *, team_id: int, column_mapping: dict[str, str]) -> dict[str, Any]:
        """Create or replace a team's column mapping.

        Args:
            team_id: Id of the team this mapping belongs to.
            column_mapping: Dict of MHES role -> source Excel column
                name, e.g. ``{"category": "Technology", "task": "Feature",
                "detail": "Feature", "estimate": "Hours"}``. Roles
                omitted here fall back to generic keyword detection at
                parse time (see ``excel_parser._map_columns``).

        Returns:
            The saved record, with ``column_mapping`` decoded back into
            a dict.
        """
        conn = self._conn()
        mapping_json = json.dumps(column_mapping, ensure_ascii=False)
        existing = self.get_by_team_id(team_id)
        with conn:
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO team_import_configs (team_id, column_mapping, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (team_id, mapping_json, datetime.now().isoformat()),
                )
            else:
                conn.execute(
                    "UPDATE team_import_configs SET column_mapping = ? WHERE team_id = ?",
                    (mapping_json, team_id),
                )
        record = self.get_by_team_id(team_id)
        assert record is not None
        logger.info("Saved import column mapping for team_id=%s: %r", team_id, column_mapping)
        return record

    def get_by_team_id(self, team_id: int) -> dict[str, Any] | None:
        """Return a team's column mapping record, or None if unconfigured."""
        row = self._conn().execute(
            "SELECT * FROM team_import_configs WHERE team_id = ?", (team_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["column_mapping"] = json.loads(record["column_mapping"])
        return record
