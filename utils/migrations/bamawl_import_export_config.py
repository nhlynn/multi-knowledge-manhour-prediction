"""Bamawl Team's own Import Template / Knowledge Parser / Export Template.

Seeds (or corrects) the ``team_import_configs``/``team_export_templates``
rows for "Bamawl Team" so its Excel Knowledge Base upload (matching
``simple_resource/bamawl_import_export_format_filled.xlsx``'s
``ALL_Detail`` sheet) parses via "phases mode"
(``services/excel_parser.py``, see ``docs/ARCHITECTURE.md`` §5i) instead
of the generic flat category/task/detail/estimate mapping, and its
exports use the standard column layout.

Looked up by team **name** ("Bamawl Team"), not slug -- the slug value
itself is Team Management's concern (``utils/migrations/team_seed.py``),
not this migration's; matching by name keeps this working regardless of
whatever slug that team currently has.

Before this migration, ``team_id=2`` (currently "Bamawl Team") carried a
stale column mapping/export template seeded for a differently-named team
earlier in this database's history -- wrong for Bamawl's actual file
structure. This migration replaces that row's contents outright (an
explicit ``upsert``, not "seed if missing"), specifically and only for
the team named "Bamawl Team". No other team's configuration is read or
written.
"""

import logging
from typing import Any

from database.db import get_connection, mark_migration_applied, migration_applied

logger = logging.getLogger(__name__)

_BAMAWL_CONFIG_MIGRATION_NAME = "seed_bamawl_import_export_config_v1"

BAMAWL_TEAM_NAME = "Bamawl Team"

# Phases-mode column mapping for simple_resource/bamawl_import_export_format*.xlsx's
# "ALL_Detail" sheet (real header row is row 4, not row 1 -- see
# docs/ARCHITECTURE.md §5i "Phase-Breakdown Excel Import"). Every phase
# column below was verified directly against that file: summing all 26
# phase values for a sample row ("Login / Logout") reproduces its
# "Total(h)" column exactly (97.45548h), confirming the file's two
# ambiguous duplicate headers -- "Review(h)" (appears twice: once as the
# Screen/Form/Function review, once as the Unit Test review) and
# "実施(h)\nImplementation" (appears twice: once as the Combined Test
# implementation, once -- unlabeled in the source sheet -- assumed here
# to be the Comprehensive Test implementation) -- are both correctly and
# exclusively accounted for, matching pandas' own disambiguation
# ("Review(h)"/"Review(h).1", "...Implementation"/"...Implementation.1").
# See docs/ARCHITECTURE.md's "Known limitation" note: the second,
# unlabeled "実施(h) Implementation" column's business meaning was
# inferred from its position (immediately after the labeled Combined
# Test group, with no group label of its own) rather than being
# explicitly confirmed -- flag to a human familiar with this workbook if
# "Comprehensive Test Implementation" turns out not to be the right name.
BAMAWL_IMPORT_COLUMN_MAPPING: dict[str, Any] = {
    "sheet": "ALL_Detail",
    "header_row": 4,
    "task_column": "Function",
    # ALL_Detail has a trailing per-role subtotal block (rows with a
    # blank ID but real numeric phase values -- e.g. "Leader 1人+
    # Developer 2人 + UIUX 1人 (Hr)/(Days)/(Months)" -- below the real
    # per-function task rows). Without id_column, task_column's
    # forward-fill would attribute that block's numbers to the last
    # real function above it; this excludes it (see
    # services/excel_parser.py::_process_phases_row).
    "id_column": "ID",
    "category": "Bamawl HR & Attendance System",
    "phase_columns": [
        {"label": "Development", "column": "Development man-hours (h)"},
        {"label": "Code Review", "column": "Code review (h)"},
        {"label": "Prototype", "column": "Prototype(h)"},
        {"label": "Prototype Review", "column": "PrototypeReview(h)"},
        {"label": "Business Flow", "column": "Business flow(h)"},
        {"label": "Business Flow Review", "column": "Business flow Review (h)"},
        {"label": "ERD", "column": "ERD(h)"},
        {"label": "ERD Review", "column": "ERD Review(h)"},
        {"label": "DFD", "column": "DFD(h)"},
        {"label": "DFD Review", "column": "DFD Review(h)"},
        {"label": "DB Design", "column": "DB Design(h)"},
        {"label": "DB Design Review", "column": "DB Design Review(h)"},
        {"label": "Screen/Form/Function", "column": "Screen/Form/Function (h)"},
        {"label": "Screen/Form/Function Review", "column": "Review(h)"},
        {"label": "Unit Test Specification", "column": "Test Specification(h)"},
        {"label": "Unit Test Review", "column": "Review(h).1"},
        {"label": "Unit Test Implementation", "column": "Implementation (h)"},
        {"label": "Combined Test Specification", "column": "テスト仕様書(h) Test Specification"},
        {"label": "Combined Test Review", "column": "レビュー(h)"},
        {"label": "Combined Test Implementation", "column": "実施(h) Implementation"},
        {"label": "Comprehensive Test Implementation", "column": "実施(h) Implementation.1"},
        {"label": "Test Data Creation", "column": "テストデータ作成(h) Test Data Creation"},
        {"label": "User Manual", "column": "マニュアル作成(h) User Manual"},
        {"label": "Accidental Work", "column": "付帯作業(h) Accidental Work"},
        {"label": "Risk", "column": "リスク(h)"},
        {"label": "Management Manhours", "column": "管理工数(h) Management Manhours"},
    ],
    "total_column": "Total(h)",
}

# Bamawl's export uses the same standard layout every unconfigured team
# gets (services.export_workbook_service.DEFAULT_EXPORT_TEMPLATE) --
# stored explicitly as Bamawl's own row (rather than left unconfigured)
# so it's no longer accidentally sharing the stale mapping described
# above, and so it can be changed independently later without affecting
# the fallback any other unconfigured team relies on.
BAMAWL_EXPORT_TEMPLATE: dict[str, Any] = {
    "sheet_title": "Manhour",
    "columns": [
        {"key": "category", "label": "Category", "width": 25},
        {"key": "task", "label": "Task List", "width": 45},
        {"key": "estimate_hours", "label": "Estimate (Hours)", "width": 22},
        {"key": "working_day", "label": "Working Day", "width": 15},
        {"key": "remarks", "label": "Remarks", "width": 35},
    ],
}


def seed_bamawl_import_export_config(mhes_db_path: str) -> dict[str, Any] | None:
    """Seed/correct Bamawl Team's import column mapping and export template.

    Safe to call on every startup -- no-ops once applied. If no team
    named "Bamawl Team" exists yet, logs a warning and retries on the
    next startup (same pattern as ``create_default_admin_user`` waiting
    on ``create_default_team``).

    Args:
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        Bamawl Team's record, or None if already applied or if no team
        named "Bamawl Team" exists yet.
    """
    from repositories.team_export_template_repository import TeamExportTemplateRepository
    from repositories.team_import_config_repository import TeamImportConfigRepository
    from repositories.team_repository import TeamRepository

    conn = get_connection(mhes_db_path)

    if migration_applied(conn, _BAMAWL_CONFIG_MIGRATION_NAME):
        logger.debug("Bamawl import/export config already seeded; skipping.")
        return None

    team_repo = TeamRepository(mhes_db_path)
    team = next(
        (t for t in team_repo.list_all() if t["name"].lower() == BAMAWL_TEAM_NAME.lower()),
        None,
    )
    if team is None:
        logger.warning(
            "Cannot seed Bamawl import/export config: no team named %r exists yet. "
            "Will retry on next startup once it does.", BAMAWL_TEAM_NAME,
        )
        return None

    TeamImportConfigRepository(mhes_db_path).upsert(
        team_id=team["id"], column_mapping=BAMAWL_IMPORT_COLUMN_MAPPING
    )
    TeamExportTemplateRepository(mhes_db_path).upsert(
        team_id=team["id"], template_config=BAMAWL_EXPORT_TEMPLATE
    )

    mark_migration_applied(conn, _BAMAWL_CONFIG_MIGRATION_NAME)
    logger.info(
        "Seeded Bamawl Team's (id=%s) import column mapping and export template.", team["id"],
    )
    return team
