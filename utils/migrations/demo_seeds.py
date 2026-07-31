"""Best-effort, environment-specific demo seeds (Phases 7 and 8).

Unlike every other migration in this package, these two are not
guaranteed-to-apply product migrations: "Development Team" is not one
of MHES's default teams (only "Infrastructure Team" is created
automatically), so both functions simply no-op — and are **not** marked
applied — on any install where that team doesn't exist, re-checking on
every startup rather than permanently giving up.
"""

import logging
from typing import Any

from database.db import get_connection, mark_migration_applied, migration_applied

logger = logging.getLogger(__name__)

_DEV_TEAM_IMPORT_CONFIG_MIGRATION_NAME = "seed_development_team_import_config_v1"
_DEV_TEAM_EXPORT_TEMPLATE_MIGRATION_NAME = "seed_development_team_export_template_v1"

DEVELOPMENT_TEAM_SLUG = "development-team"


def seed_development_team_import_config(mhes_db_path: str) -> dict[str, Any] | None:
    """Best-effort demo seed of Development Team's Excel column mapping.

    Seeds ``{"category": "Technology", "task": "Feature", "detail":
    "Feature", "estimate": "Hours"}`` — ``task`` and ``detail`` both map
    to "Feature" because this illustrative format has no separate
    per-activity breakdown (see ``docs/ARCHITECTURE.md`` §5g).

    Args:
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        The saved config record, or None if already applied, or if
        Development Team doesn't exist (yet).
    """
    from repositories.team_import_config_repository import TeamImportConfigRepository
    from repositories.team_repository import TeamRepository

    conn = get_connection(mhes_db_path)
    if migration_applied(conn, _DEV_TEAM_IMPORT_CONFIG_MIGRATION_NAME):
        logger.debug("Development Team import config seed already applied; skipping.")
        return None

    team = TeamRepository(mhes_db_path).get_by_slug(DEVELOPMENT_TEAM_SLUG)
    if team is None:
        logger.debug(
            "No team with slug %r yet; skipping the Development Team import "
            "config demo seed (will check again next startup).",
            DEVELOPMENT_TEAM_SLUG,
        )
        return None

    config = TeamImportConfigRepository(mhes_db_path).upsert(
        team_id=team["id"],
        column_mapping={
            "category": "Technology",
            "task": "Feature",
            "detail": "Feature",
            "estimate": "Hours",
        },
    )
    mark_migration_applied(conn, _DEV_TEAM_IMPORT_CONFIG_MIGRATION_NAME)
    logger.info(
        "Seeded Development Team (id=%s) import column mapping: %r",
        team["id"], config["column_mapping"],
    )
    return config


def seed_development_team_export_template(mhes_db_path: str) -> dict[str, Any] | None:
    """Best-effort demo seed of Development Team's Excel export template.

    Seeds a compact 4-column template — ``Technology`` (category),
    ``Task``, ``Hours`` (estimate), ``Notes`` (remarks) — deliberately
    without a "working_day" column, to demonstrate that a team's export
    can both relabel and drop columns relative to
    ``services.export_workbook_service.DEFAULT_EXPORT_TEMPLATE`` (see
    ``docs/ARCHITECTURE.md`` §5h).

    Args:
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        The saved template record, or None if already applied, or if
        Development Team doesn't exist (yet).
    """
    from repositories.team_export_template_repository import TeamExportTemplateRepository
    from repositories.team_repository import TeamRepository

    conn = get_connection(mhes_db_path)
    if migration_applied(conn, _DEV_TEAM_EXPORT_TEMPLATE_MIGRATION_NAME):
        logger.debug("Development Team export template seed already applied; skipping.")
        return None

    team = TeamRepository(mhes_db_path).get_by_slug(DEVELOPMENT_TEAM_SLUG)
    if team is None:
        logger.debug(
            "No team with slug %r yet; skipping the Development Team export "
            "template demo seed (will check again next startup).",
            DEVELOPMENT_TEAM_SLUG,
        )
        return None

    template = TeamExportTemplateRepository(mhes_db_path).upsert(
        team_id=team["id"],
        template_config={
            "sheet_title": "Dev Manhour",
            "columns": [
                {"key": "category", "label": "Technology", "width": 20},
                {"key": "task", "label": "Task", "width": 40},
                {"key": "estimate_hours", "label": "Hours", "width": 15},
                {"key": "remarks", "label": "Notes", "width": 35},
            ],
        },
    )
    mark_migration_applied(conn, _DEV_TEAM_EXPORT_TEMPLATE_MIGRATION_NAME)
    logger.info(
        "Seeded Development Team (id=%s) export template: %r",
        team["id"], template["template_config"],
    )
    return template
