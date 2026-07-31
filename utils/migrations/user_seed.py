"""Phase 2 of multi-team support: the ``users`` table and the default Admin."""

import logging
import os
import secrets
from datetime import datetime
from typing import Any

from database.db import get_connection, mark_migration_applied, migration_applied
from utils.migrations.team_seed import DEFAULT_TEAM_SLUG

logger = logging.getLogger(__name__)

_DEFAULT_ADMIN_MIGRATION_NAME = "create_default_admin_user_v1"

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_ROLE = "Admin"


def create_default_admin_user(mhes_db_path: str) -> dict[str, Any] | None:
    """Create the ``users`` table and seed one Admin user for the default team.

    Requires a team with slug ``DEFAULT_TEAM_SLUG`` to already exist (i.e.
    ``create_default_team`` must run first, which ``app.py`` guarantees by
    calling order) — the seeded admin needs a ``team_id`` to belong to.
    Safe to call on every startup — no-ops once applied.

    The password comes from the ``MHES_DEFAULT_ADMIN_PASSWORD`` environment
    variable if set at the time this migration first runs; otherwise a
    random one is generated and logged once, at WARNING level, so an
    operator can capture it. It is hashed before storage and is never
    recoverable afterwards — if it's lost, a new user must be created (or
    this migration's row deleted from ``db_migrations`` and rerun) rather
    than "recovering" the original password.

    Args:
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        The seeded admin user's record, or None if the migration had
        already been applied, or if the default team does not exist yet
        (in which case this is retried on the next startup).
    """
    from repositories.team_repository import TeamRepository
    from repositories.user_repository import UserRepository
    from services.auth_service import AuthService

    conn = get_connection(mhes_db_path)

    if migration_applied(conn, _DEFAULT_ADMIN_MIGRATION_NAME):
        logger.debug("Default admin user migration already applied; skipping.")
        return None

    team_repo = TeamRepository(mhes_db_path)
    team = team_repo.get_by_slug(DEFAULT_TEAM_SLUG)
    if team is None:
        logger.warning(
            "Cannot seed default admin user: no team with slug %r exists yet. "
            "Will retry on next startup once create_default_team has run.",
            DEFAULT_TEAM_SLUG,
        )
        return None

    user_repo = UserRepository(mhes_db_path)

    existing = user_repo.get_by_username(DEFAULT_ADMIN_USERNAME)
    if existing is not None:
        logger.info(
            "Default admin user %r already present (id=%s); marking migration applied.",
            DEFAULT_ADMIN_USERNAME, existing["id"],
        )
        mark_migration_applied(conn, _DEFAULT_ADMIN_MIGRATION_NAME)
        return existing

    password = os.environ.get("MHES_DEFAULT_ADMIN_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)

    user = user_repo.insert(
        username=DEFAULT_ADMIN_USERNAME,
        password_hash=AuthService.hash_password(password),
        team_id=team["id"],
        role=DEFAULT_ADMIN_ROLE,
        created_at=datetime.now().isoformat(),
    )

    mark_migration_applied(conn, _DEFAULT_ADMIN_MIGRATION_NAME)

    if generated:
        logger.warning(
            "Seeded default admin user '%s' with a randomly generated password: "
            "%s -- this is logged only this once and cannot be recovered later. "
            "Log in and note it down now, or set MHES_DEFAULT_ADMIN_PASSWORD "
            "before first startup on future installs to control it directly.",
            DEFAULT_ADMIN_USERNAME, password,
        )
    else:
        logger.info(
            "Seeded default admin user '%s' using MHES_DEFAULT_ADMIN_PASSWORD "
            "from the environment.",
            DEFAULT_ADMIN_USERNAME,
        )

    return user
