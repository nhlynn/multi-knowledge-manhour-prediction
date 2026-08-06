"""KiKan Team's own Import Template / Knowledge Parser.

Seeds (or corrects) the ``team_import_configs`` row for "KiKan Team" so
its Excel Knowledge Base upload (matching
``import/kikan/kikan_import_template.xlsx``'s ``工数詳細`` worksheet)
parses via "phases mode" (``services/excel_parser.py``, see
``docs/ARCHITECTURE.md`` §5i) instead of the generic flat
category/task/detail/estimate mapping -- the same pattern
``utils/migrations/bamawl_import_export_config.py`` established for
Bamawl Team.

``import/kikan/kikan_import_template.xlsx`` is KiKan Team's **one**
official template -- Template Download serves it, import validation
accepts it, and ``services/kikan_export_builder.py::KikanExportBuilder``
builds every export directly on top of it (copy, then populate, then
save). There is deliberately no separate internal-only export
template; this module does not seed a ``team_export_templates`` row.

Looked up by team **name** ("KiKan Team"), not slug, matching every
other team-specific migration in this codebase.
"""

import logging
from typing import Any

from database.db import get_connection, mark_migration_applied, migration_applied

logger = logging.getLogger(__name__)

_KIKAN_CONFIG_MIGRATION_NAME = "seed_kikan_import_export_config_v1"

KIKAN_TEAM_NAME = "KiKan Team"

# The official template's full worksheet list, in order -- used (via
# _build_kikan_template_spec below and services/team_template_registry.py)
# by services/team_template_validator.py to validate that an uploaded
# workbook has every required sheet before import is attempted at all.
KIKAN_REQUIRED_SHEET_NAMES: list[str] = [
    "機能一覧",
    "Milestone",
    "工数詳細",
    "工数・費用",
]

# The official template's 工数詳細 header row (row 4), verbatim and in
# order -- read directly from
# import/kikan/kikan_import_template.xlsx. Used (via
# _build_kikan_template_spec below) by
# services/team_template_validator.py for an EXACT, position-by-position
# match (not the tolerant whitespace/case matching
# services/excel_parser.py's _find_column does when actually reading
# data) -- an uploaded file whose columns are named or ordered
# differently than this fails validation before any data is read.
KIKAN_DETAIL_HEADERS: list[str] = [
    "業務分類", "番号", "機能ID", "機能名称", "Status",
    "実装工数\n(h)", "コードレビュー\n(h)", "仕様理解\n(h)", "QA\n(h)",
    "テスト仕様書\n(h)", "レビュー\n(h)", "実施\n(h)",
    "テストデータ作成\n(h)", "付帯作業\n(h)", "リスク\n(h)", "管理工数\n(h)",
    "合計\n(h)",
]

# Optional template-version marker: not configured today, same as every
# other team -- "template version matches (if available)" in
# services/team_template_validator.py::validate_team_template is a
# documented no-op while this is None.
KIKAN_TEMPLATE_VERSION: str | None = None

# Phases-mode column mapping for import/kikan/kikan_import_template.xlsx's
# "工数詳細" sheet (real header row is row 4, not row 1 -- driver rows 1-3
# above it hold the per-phase hour ratios/group labels the sheet's own
# formulas use, not part of the header itself).
#
# 機能名称 (Function Name, column D) is itself a formula
# (=VLOOKUP(機能ID, 機能一覧, ...)); this reads its cached, Excel-computed
# display value, exactly like reading any other cell -- fine for files
# actually saved by Excel (the normal case for an uploaded workbook), but
# would break if the template were ever re-saved via openpyxl without
# recalculation first (see docs/ARCHITECTURE.md's openpyxl cached-value
# limitation note).
#
# 業務分類 (column A) is read dynamically per row (not a fixed literal
# like Bamawl's) since it holds a real, meaningful business
# classification ("画面" / Screen, etc.) merged down the function block.
#
# 工数詳細 has a trailing rollup block below the real per-function rows
# (rows 12-17: person-hour/day/month summaries; rows 19-21: team
# headcount) with numeric phase-column values but a non-numeric or blank
# 番号 (Number) cell -- id_column excludes that block from import the
# same way Bamawl's ID column excludes its own trailing subtotal rows
# (see services/excel_parser.py::_process_phases_row). 機能ID itself
# can't be used for this check since it's an alphanumeric string
# (e.g. "SPPR00101AC"), not a number.
KIKAN_IMPORT_COLUMN_MAPPING: dict[str, Any] = {
    "sheet": "工数詳細",
    "header_row": 4,
    "task_column": "機能名称",
    "id_column": "番号",
    "category_column": "業務分類",
    "phase_columns": [
        {"label": "Development", "column": "実装工数 (h)"},
        {"label": "Code Review", "column": "コードレビュー (h)"},
        {"label": "Spec Understanding", "column": "仕様理解 (h)"},
        {"label": "QA", "column": "QA (h)"},
        {"label": "Test Specification", "column": "テスト仕様書 (h)"},
        {"label": "Review", "column": "レビュー (h)"},
        {"label": "Implementation", "column": "実施 (h)"},
        {"label": "Test Data Creation", "column": "テストデータ作成 (h)"},
        {"label": "Accidental Work", "column": "付帯作業 (h)"},
        {"label": "Risk", "column": "リスク (h)"},
        {"label": "Management Manhours", "column": "管理工数 (h)"},
    ],
    "total_column": "合計 (h)",
}

# KiKan Team's entry in the team-agnostic template-validation registry
# (see services/team_template_validator.py::TeamTemplateSpec and
# services/team_template_registry.py) -- bundles everything above that
# a strict per-team upload validation needs, plus where its downloadable
# template lives.
#
# Unlike Bamawl Team (which has two files: a real internal workbook
# used for import/export, plus a separate, sanitized public sample
# used only for download), KiKan Team has deliberately just ONE
# official template: import/kikan/kikan_import_template.xlsx (built
# from the real internal workbook by
# import/kikan/build_sample_template.py, then sanitized). Template
# Download serves this exact file, import validation accepts it, and
# services/kikan_export_builder.py::KikanExportBuilder.template_path
# points at this same path as its export base -- no other KiKan
# template file is read anywhere at runtime.
def _build_kikan_template_spec() -> Any:
    from services.team_template_validator import TeamTemplateSpec

    return TeamTemplateSpec(
        team_name=KIKAN_TEAM_NAME,
        required_sheet_names=KIKAN_REQUIRED_SHEET_NAMES,
        header_sheet=KIKAN_IMPORT_COLUMN_MAPPING["sheet"],
        header_row=KIKAN_IMPORT_COLUMN_MAPPING["header_row"],
        expected_headers=KIKAN_DETAIL_HEADERS,
        column_mapping=KIKAN_IMPORT_COLUMN_MAPPING,
        template_version=KIKAN_TEMPLATE_VERSION,
        sample_template_path=("import", "kikan", "kikan_import_template.xlsx"),
    )


def seed_kikan_import_export_config(mhes_db_path: str) -> dict[str, Any] | None:
    """Seed/correct KiKan Team's import column mapping.

    Safe to call on every startup -- no-ops once applied. If no team
    named "KiKan Team" exists yet, logs a warning and retries on the
    next startup (same pattern as ``seed_bamawl_import_export_config``).

    Args:
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        KiKan Team's record, or None if already applied or if no team
        named "KiKan Team" exists yet.
    """
    from repositories.team_import_config_repository import TeamImportConfigRepository
    from repositories.team_repository import TeamRepository

    conn = get_connection(mhes_db_path)

    if migration_applied(conn, _KIKAN_CONFIG_MIGRATION_NAME):
        logger.debug("KiKan import config already seeded; skipping.")
        return None

    team_repo = TeamRepository(mhes_db_path)
    team = next(
        (t for t in team_repo.list_all() if t["name"].lower() == KIKAN_TEAM_NAME.lower()),
        None,
    )
    if team is None:
        logger.warning(
            "Cannot seed KiKan import config: no team named %r exists yet. "
            "Will retry on next startup once it does.", KIKAN_TEAM_NAME,
        )
        return None

    TeamImportConfigRepository(mhes_db_path).upsert(
        team_id=team["id"], column_mapping=KIKAN_IMPORT_COLUMN_MAPPING
    )

    mark_migration_applied(conn, _KIKAN_CONFIG_MIGRATION_NAME)
    logger.info("Seeded KiKan Team's (id=%s) import column mapping.", team["id"])
    return team
