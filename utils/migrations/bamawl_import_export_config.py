"""Bamawl Team's own Import Template / Knowledge Parser / Export Template.

Seeds (or corrects) the ``team_import_configs``/``team_export_templates``
rows for "Bamawl Team" so its Excel Knowledge Base upload (matching
``simple_resource/bamawl_import_export_format_filled.xlsx``'s
``ALL_Detail`` sheet -- Bamawl Team's single official workbook, used
for both import and export, see ``services/bamawl_export_builder.py``)
parses via "phases mode" (``services/excel_parser.py``, see
``docs/ARCHITECTURE.md`` §5i) instead of the generic flat
category/task/detail/estimate mapping. The same ``column_mapping``
seeded here is reused directly by
``services/bamawl_export_builder.py`` to know where each phase column
lives when building an export back onto that same template.

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

v2: ``BAMAWL_IMPORT_COLUMN_MAPPING`` gained an ``extra_columns`` entry
for ``ALL_Detail``'s ``Status`` column (same mechanism KiKan's own
``Status`` column already used) -- v1 never declared it, so Status was
silently dropped on import and therefore always came out blank on
export regardless of what ``services/bamawl_export_builder.py`` did.
The migration name was bumped (``_v1`` -> ``_v2``) so this correction
re-seeds even for databases where v1 already ran and marked itself
applied.
"""

import logging
from typing import Any

from database.db import get_connection, mark_migration_applied, migration_applied

logger = logging.getLogger(__name__)

_BAMAWL_CONFIG_MIGRATION_NAME = "seed_bamawl_import_export_config_v3"

BAMAWL_TEAM_NAME = "Bamawl Team"

# The official template's full worksheet list, in order -- used (via
# _build_bamawl_template_spec below and services/team_template_registry.py)
# by services/team_template_validator.py to validate that an uploaded
# workbook has every required sheet (not just ALL_Detail) before
# import is attempted at all.
BAMAWL_REQUIRED_SHEET_NAMES: list[str] = [
    "ReqDefinition",
    "FunctionList",
    "TotalManhour",
    "ALL_Detail",
    "Infra Manhour",
    "Business Flow(system admin)",
    "Milestone",
]

# The official template's ALL_Detail header row (row 4), verbatim and
# in order -- read directly from
# simple_resource/bamawl_import_export_format_filled.xlsx. Used (via
# _build_bamawl_template_spec below) by
# services/team_template_validator.py for an EXACT, position-by-position
# match (not the tolerant whitespace/case matching
# services/excel_parser.py's _find_column does when actually reading
# data) -- an uploaded file whose columns are named or ordered
# differently than this, even if every individually-required column
# can still be *found* somewhere in the row, fails validation.
BAMAWL_ALL_DETAIL_HEADERS: list[str] = [
    "ID", "Requirements", "Function", "Status",
    "\nDevelopment man-hours (h)\n", "\nCode review (h)", "Prototype(h)", "PrototypeReview(h)",
    "\n\nBusiness flow(h)", "\nBusiness flow Review\n(h)",
    "ERD(h)", "ERD Review(h)", "DFD(h)", "DFD Review(h)",
    "\nDB Design(h)", "\nDB Design Review(h)",
    "Screen/Form/Function (h)", "Review(h)",
    "\nTest Specification(h)", "Review(h)",
    "\nImplementation\n(h)",
    "テスト仕様書(h)\nTest Specification", "レビュー(h)",
    "実施(h)\nImplementation", "実施(h)\nImplementation",
    "テストデータ作成(h)\nTest Data Creation", "マニュアル作成(h)\nUser Manual",
    "付帯作業(h)\nAccidental Work", "リスク(h)", "管理工数(h)\nManagement Manhours",
    "Total(h)",
]

# Optional template-version marker: not configured today because the
# official template has no version cell/convention yet -- "template
# version matches (if available)" in
# services/team_template_validator.py::validate_team_template is a
# documented no-op while this is None. Setting it to a real value (and
# giving validate_team_template a cell reference to check it against)
# is how a future template revision would turn this check on.
BAMAWL_TEMPLATE_VERSION: str | None = None

# Phases-mode column mapping for simple_resource/bamawl_import_export_format_filled.xlsx's
# (Bamawl Team's single official template, used for both import and export)
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
    # Requirements is a per-row grouping label sitting above the Function
    # (task) rows: consecutive rows sharing a Requirement belong to it,
    # and blank cells forward-fill from the Requirement above. Feeding it
    # as category_column (instead of the old fixed "category" literal)
    # makes each Requirement show up as its own Category in Chatbot/
    # Preview -- exactly the grouping Bamawl wants above its tasks. The
    # generic phases-mode parser already supports category_column
    # (services/excel_parser.py::_process_phases_sheet); nothing else
    # needs to change on the import side.
    "category_column": "Requirements",
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
    # Same mechanism as KiKan's own "Status" column (see
    # utils/migrations/kikan_import_export_config.py) -- captures the
    # ALL_Detail sheet's real "Status" column (present in
    # BAMAWL_ALL_DETAIL_HEADERS above) verbatim onto each task's
    # "status" field, passed through generically all the way to
    # Preview/export by services/excel_parser.py's own extra_columns
    # handling. Previously missing here, which is why Status was never
    # captured on import and therefore never available to write back
    # out on export (see services/bamawl_export_builder.py).
    "extra_columns": [
        {"field": "status", "column": "Status"},
    ],
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

# Bamawl Team's entry in the team-agnostic template-validation registry
# (see services/team_template_validator.py::TeamTemplateSpec and
# services/team_template_registry.py) -- bundles everything above that
# a strict per-team upload validation needs, plus where its public
# sample download lives. This is the one place a future SGL/KiKan/SSD
# spec would be defined analogously (their own required sheet list,
# header sheet/row, expected headers, column mapping, and sample path),
# then registered in services/team_template_registry.py.
def _build_bamawl_template_spec() -> Any:
    from services.team_template_validator import TeamTemplateSpec

    return TeamTemplateSpec(
        team_name=BAMAWL_TEAM_NAME,
        required_sheet_names=BAMAWL_REQUIRED_SHEET_NAMES,
        header_sheet=BAMAWL_IMPORT_COLUMN_MAPPING["sheet"],
        header_row=BAMAWL_IMPORT_COLUMN_MAPPING["header_row"],
        expected_headers=BAMAWL_ALL_DETAIL_HEADERS,
        column_mapping=BAMAWL_IMPORT_COLUMN_MAPPING,
        template_version=BAMAWL_TEMPLATE_VERSION,
        sample_template_path=("import", "bamawl", "bamawl_import_template.xlsx"),
    )


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