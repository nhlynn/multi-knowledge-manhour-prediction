"""Phase 1 of multi-team support: the ``teams`` table and its default row."""

import logging
from datetime import datetime
from typing import Any

from database.db import get_connection, mark_migration_applied, migration_applied

logger = logging.getLogger(__name__)

_DEFAULT_TEAM_MIGRATION_NAME = "create_default_team_v1"

DEFAULT_TEAM_NAME = "Infrastructure Team"
DEFAULT_TEAM_SLUG = "infrastructure"

_DEFAULT_TEAMS_SEED_MIGRATION_NAME = "seed_default_teams_v1"

# (name, slug) pairs seeded on every fresh install alongside the
# Infrastructure Team above — the vendor teams this multi-vendor MHES
# instance is actually used by. status='Active' and description=NULL
# for all of them (matches the teams table's own column defaults).
DEFAULT_TEAMS: list[tuple[str, str]] = [
    ("Bamawl Team", "bamawl"),
    ("SGL Team", "sgl"),
    ("KiKan Team", "kikan"),
    ("SSD Team", "ssd"),
]


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


def seed_default_teams(mhes_db_path: str) -> list[dict[str, Any]]:
    """Seed the vendor default teams (``DEFAULT_TEAMS``) for a fresh install.

    Each team is created with ``status='Active'`` and ``description=NULL``.
    Existence is checked per-team by name (case-insensitive, the same
    check ``services.team_service`` uses for "does this team already
    exist") rather than relying only on this migration's own applied-flag
    — so a team created manually (or by some other means) before this
    migration ever runs is detected and left alone rather than
    duplicated, exactly like ``create_default_team`` above does for the
    Infrastructure Team. Only the teams still missing are inserted.

    Safe to call on every startup — no-ops once every team in
    ``DEFAULT_TEAMS`` is confirmed present and the migration is marked
    applied.

    Args:
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        The full current records (existing or newly created) for every
        team in ``DEFAULT_TEAMS``, in the same order.
    """
    from repositories.team_repository import TeamRepository

    conn = get_connection(mhes_db_path)

    if migration_applied(conn, _DEFAULT_TEAMS_SEED_MIGRATION_NAME):
        logger.debug("Default teams seed already applied; skipping.")
        return []

    repo = TeamRepository(mhes_db_path)
    records: list[dict[str, Any]] = []

    for name, slug in DEFAULT_TEAMS:
        existing = repo.get_by_slug(slug)
        if existing is None and repo.name_exists(name):
            # Same team, different slug than ours (e.g. created manually)
            # -- find it by name instead of inserting a duplicate.
            existing = next(
                (t for t in repo.list_all() if t["name"].lower() == name.lower()), None
            )
        if existing is not None:
            logger.info(
                "Default team %r already present (id=%s); leaving as-is.",
                name, existing["id"],
            )
            records.append(existing)
            continue

        team = repo.insert(
            name=name,
            slug=slug,
            created_at=datetime.now().isoformat(),
            description=None,
            status="Active",
        )
        logger.info("Seeded default team %r (id=%s).", name, team["id"])
        records.append(team)

    mark_migration_applied(conn, _DEFAULT_TEAMS_SEED_MIGRATION_NAME)
    return records
