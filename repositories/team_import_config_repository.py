"""Raw SQL data access for the ``team_import_configs`` table.

Stores each team's Excel column-role mapping (Phase 7 of multi-team
support) — see ``services/excel_parser.py::_map_columns`` for how a
mapping is consumed at parse time. No parsing logic lives here, only
CRUD, mirroring the style of ``repositories/team_repository.py`` and
``repositories/user_repository.py``.
"""

import json
import logging
from datetime import datetime
from typing import Any

from repositories.base_repository import BaseRepository

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


class TeamImportConfigRepository(BaseRepository):
    """Repository for CRUD access to the ``team_import_configs`` table."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)

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
        mapping_json = json.dumps(column_mapping, ensure_ascii=False)
        existing = self.get_by_team_id(team_id)
        self._upsert_by_unique_column(
            table="team_import_configs",
            unique_column="team_id",
            unique_value=team_id,
            data_column="column_mapping",
            data_value=mapping_json,
            created_at=datetime.now().isoformat(),
            existing=existing,
        )
        record = self.get_by_team_id(team_id)
        assert record is not None
        logger.info("Saved import column mapping for team_id=%s: %r", team_id, column_mapping)
        return record

    def get_by_team_id(self, team_id: int) -> dict[str, Any] | None:
        """Return a team's column mapping record, or None if unconfigured."""
        record = self._fetch_one_dict(
            "SELECT * FROM team_import_configs WHERE team_id = ?", (team_id,)
        )
        if record is None:
            return None
        record["column_mapping"] = json.loads(record["column_mapping"])
        return record
