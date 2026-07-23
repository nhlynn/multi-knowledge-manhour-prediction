"""Raw SQL data access for the ``team_export_templates`` table.

Stores each team's Excel export column layout (Phase 8 of multi-team
support) — see ``routes/export.py::_build_workbook`` for how a template
config is consumed at export time. No rendering logic lives here, only
CRUD, mirroring the style of ``repositories/team_import_config_repository.py``.
"""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from database.db import ensure_schema, get_connection

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_export_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL UNIQUE REFERENCES teams(id),
    template_config TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_team_export_templates_team_id ON team_export_templates(team_id);
"""


class TeamExportTemplateRepository:
    """Repository for CRUD access to the ``team_export_templates`` table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        ensure_schema(self._conn(), _SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    def upsert(self, *, team_id: int, template_config: dict[str, Any]) -> dict[str, Any]:
        """Create or replace a team's export template.

        Args:
            team_id: Id of the team this template belongs to.
            template_config: Dict with ``sheet_title`` and an ordered
                ``columns`` list, each ``{"key": ..., "label": ...,
                "width": ...}``. See ``routes/export.py::DEFAULT_EXPORT_TEMPLATE``
                for the exact shape and the recognized ``key`` vocabulary.

        Returns:
            The saved record, with ``template_config`` decoded back into
            a dict.
        """
        conn = self._conn()
        config_json = json.dumps(template_config, ensure_ascii=False)
        existing = self.get_by_team_id(team_id)
        with conn:
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO team_export_templates (team_id, template_config, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (team_id, config_json, datetime.now().isoformat()),
                )
            else:
                conn.execute(
                    "UPDATE team_export_templates SET template_config = ? WHERE team_id = ?",
                    (config_json, team_id),
                )
        record = self.get_by_team_id(team_id)
        assert record is not None
        logger.info("Saved export template for team_id=%s: %r", team_id, template_config)
        return record

    def get_by_team_id(self, team_id: int) -> dict[str, Any] | None:
        """Return a team's export template record, or None if unconfigured."""
        row = self._conn().execute(
            "SELECT * FROM team_export_templates WHERE team_id = ?", (team_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["template_config"] = json.loads(record["template_config"])
        return record
