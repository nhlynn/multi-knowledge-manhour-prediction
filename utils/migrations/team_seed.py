"""Phase 1 of multi-team support: the ``teams`` table and its default row."""

import logging
from datetime import datetime
from typing import Any

from database.db import get_connection, mark_migration_applied, migration_applied

logger = logging.getLogger(__name__)

_DEFAULT_TEAM_MIGRATION_NAME = "create_default_team_v1"

DEFAULT_TEAM_NAME = "Infrastructure Team"
DEFAULT_TEAM_SLUG = "infrastructure-team"


def create_default_team(mhes_db_path: str) -> dict[str, Any] | None:
    """Create the ``teams`` table and seed the default "Infrastructure Team".

    This is Phase 1 of multi-team support: it only introduces the
    ``teams`` table and a single seed row. It does not add a ``team_id``
    column to any existing table, and does not change how the Knowledge
    Base, embeddings, upload, search, or export code paths behave — all
    existing data implicitly continues to belong to this one team until a
    later phase wires up per-team scoping.

    Args:
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        The default team's record, or None if the migration had already
        been applied (its state is not re-read in that case).
    """
    from repositories.team_repository import TeamRepository

    conn = get_connection(mhes_db_path)

    if migration_applied(conn, _DEFAULT_TEAM_MIGRATION_NAME):
        logger.debug("Default team migration already applied; skipping.")
        return None

    repo = TeamRepository(mhes_db_path)

    existing = repo.get_by_slug(DEFAULT_TEAM_SLUG)
    if existing is not None:
        logger.info(
            "Default team %r already present (id=%s); marking migration applied.",
            DEFAULT_TEAM_NAME, existing["id"],
        )
        mark_migration_applied(conn, _DEFAULT_TEAM_MIGRATION_NAME)
        return existing

    team = repo.insert(
        name=DEFAULT_TEAM_NAME,
        slug=DEFAULT_TEAM_SLUG,
        created_at=datetime.now().isoformat(),
    )
    mark_migration_applied(conn, _DEFAULT_TEAM_MIGRATION_NAME)
    logger.info("Created default team %r (id=%s).", DEFAULT_TEAM_NAME, team["id"])
    return team
