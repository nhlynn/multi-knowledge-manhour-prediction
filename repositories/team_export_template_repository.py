"""Raw SQL data access for the ``team_export_templates`` table.

Stores each team's Excel export column layout (Phase 8 of multi-team
support) — see ``services/export_workbook_service.py::build_workbook``
for how a template config is consumed at export time. No rendering
logic lives here, only CRUD, mirroring the style of
``repositories/team_import_config_repository.py``.
"""

import json
import logging
from datetime import datetime
from typing import Any

from repositories.base_repository import BaseRepository

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


class TeamExportTemplateRepository(BaseRepository):
    """Repository for CRUD access to the ``team_export_templates`` table."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)

    def upsert(self, *, team_id: int, template_config: dict[str, Any]) -> dict[str, Any]:
        """Create or replace a team's export template.

        Args:
            team_id: Id of the team this template belongs to.
            template_config: Dict with ``sheet_title`` and an ordered
                ``columns`` list, each ``{"key": ..., "label": ...,
                "width": ...}``. See
                ``services/export_workbook_service.py::DEFAULT_EXPORT_TEMPLATE``
                for the exact shape and the recognized ``key`` vocabulary.

        Returns:
            The saved record, with ``template_config`` decoded back into
            a dict.
        """
        config_json = json.dumps(template_config, ensure_ascii=False)
        existing = self.get_by_team_id(team_id)
        self._upsert_by_unique_column(
            table="team_export_templates",
            unique_column="team_id",
            unique_value=team_id,
            data_column="template_config",
            data_value=config_json,
            created_at=datetime.now().isoformat(),
            existing=existing,
        )
        record = self.get_by_team_id(team_id)
        assert record is not None
        logger.info("Saved export template for team_id=%s: %r", team_id, template_config)
        return record

    def get_by_team_id(self, team_id: int) -> dict[str, Any] | None:
        """Return a team's export template record, or None if unconfigured."""
        record = self._fetch_one_dict(
            "SELECT * FROM team_export_templates WHERE team_id = ?", (team_id,)
        )
        if record is None:
            return None
        record["template_config"] = json.loads(record["template_config"])
        return record
