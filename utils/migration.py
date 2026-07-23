"""One-shot database migrations for MHES.

Five migrations run at application startup (see app.py), all safe to
call on every startup — each no-ops once recorded as applied, and
no-ops if there is nothing to migrate (e.g. a fresh install):

1. ``migrate_stashes_json_to_sqlite`` — imports the legacy
   ``temp_data/stashes.json`` file (if still present) directly into the
   shared ``mhes.db``.
2. ``merge_legacy_databases_into_mhes`` — merges rows from the
   now-superseded per-feature databases (``temp_data/temp_storage.db``,
   ``exports/export_history.db``) into ``mhes.db``. The old database
   files are left on disk untouched; only their rows are copied.
3. ``create_default_team`` — creates the ``teams`` table (Phase 1 of
   multi-team support) and seeds a single "Infrastructure Team" row so
   existing (pre-multi-team) data has a team to be attributed to in a
   later phase. Does not touch Knowledge Base, embeddings, or any
   existing table's rows/columns.
4. ``migrate_kb_to_team_storage`` — copies the old shared
   ``kb_knowledge/`` and ``embeddings/`` folders into the default team's
   isolated ``storage/teams/<slug>/{knowledge,embeddings}`` tree (Phase 4
   of multi-team support), then retires the old folders (renamed to
   ``.bak``, never deleted). Must run after ``create_default_team``.
5. ``create_default_admin_user`` — creates the ``users`` table (Phase 2
   of multi-team support: authentication) and seeds a single Admin user
   attached to the default team, so there is a way to log in on a fresh
   install. Must run after ``create_default_team``.
6. ``seed_development_team_import_config`` — best-effort demo seed of a
   Development Team Excel column mapping (Phase 7 of multi-team
   support). Unlike the migrations above, this is environment-specific,
   not a guaranteed-to-apply product migration: it only does anything if
   a team with slug "development-team" already exists (which it does in
   this environment, created manually while testing earlier phases). On
   a fresh install without that team, it harmlessly no-ops and is not
   marked applied, so it keeps checking on every startup.
7. ``seed_development_team_export_template`` — same best-effort,
   environment-specific pattern as #6, but for Development Team's Excel
   *export* column template (Phase 8 of multi-team support).
"""

import json
import logging
import os
import secrets
import shutil
import sqlite3
from datetime import datetime
from typing import Any

from database.db import get_connection, mark_migration_applied, migration_applied

logger = logging.getLogger(__name__)

_JSON_MIGRATION_NAME = "stashes_json_to_sqlite_v1"
_MERGE_MIGRATION_NAME = "merge_legacy_dbs_into_mhes_v1"
_DEFAULT_TEAM_MIGRATION_NAME = "create_default_team_v1"
_DEFAULT_ADMIN_MIGRATION_NAME = "create_default_admin_user_v1"
_KB_TEAM_STORAGE_MIGRATION_NAME = "migrate_kb_to_team_storage_v1"
_DEV_TEAM_IMPORT_CONFIG_MIGRATION_NAME = "seed_development_team_import_config_v1"
_DEV_TEAM_EXPORT_TEMPLATE_MIGRATION_NAME = "seed_development_team_export_template_v1"

DEFAULT_TEAM_NAME = "Infrastructure Team"
DEFAULT_TEAM_SLUG = "infrastructure-team"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_ROLE = "Admin"
DEVELOPMENT_TEAM_SLUG = "development-team"


def migrate_stashes_json_to_sqlite(temp_data_folder: str, mhes_db_path: str) -> int:
    """Import legacy ``stashes.json`` records directly into ``mhes.db``.

    Args:
        temp_data_folder: Folder containing the legacy ``stashes.json``.
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        Number of records migrated (0 if there was nothing to migrate,
        or the migration had already run).
    """
    from repositories.temp_repository import TempRepository

    conn = get_connection(mhes_db_path)

    if migration_applied(conn, _JSON_MIGRATION_NAME):
        logger.debug("Stash JSON migration already applied; skipping.")
        return 0

    json_path = os.path.join(temp_data_folder, "stashes.json")
    if not os.path.isfile(json_path):
        logger.info("No legacy stashes.json found; nothing to migrate.")
        mark_migration_applied(conn, _JSON_MIGRATION_NAME)
        return 0

    logger.info("Migrating legacy stashes.json into %s...", mhes_db_path)
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            legacy_stashes = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.exception(
            "Failed to read stashes.json; leaving it in place and skipping migration."
        )
        return 0

    repo = TempRepository(mhes_db_path)
    migrated = 0
    for stash in legacy_stashes:
        stash_id = stash.get("id")
        if not stash_id or repo.exists(stash_id):
            continue
        created_at = stash.get("stashedAt")
        if not created_at:
            created_at = datetime.now().isoformat()
            logger.warning(
                "Legacy stash id=%s had no stashedAt; using current time instead.", stash_id
            )
        record = {
            "id": stash_id,
            "stash_type": "preview",
            "project_name": stash.get("projectName") or "",
            "created_by": stash.get("createdBy") or "",
            "project_remark": stash.get("projectRemark") or "",
            "json_data": json.dumps(
                {
                    "categories": stash.get("categories") or [],
                    "totals": stash.get("totals") or {},
                },
                ensure_ascii=False,
            ),
            "created_at": created_at,
            "expires_at": None,
        }
        repo.insert(record)
        migrated += 1

    if migrated != len(legacy_stashes):
        logger.warning(
            "Migrated %d of %d legacy stashes (skipped duplicates/malformed records).",
            migrated, len(legacy_stashes),
        )
    else:
        logger.info("Migrated %d legacy stash(es) into %s.", migrated, mhes_db_path)

    backup_path = json_path + ".bak"
    try:
        os.replace(json_path, backup_path)
        logger.info("Renamed stashes.json to %s after successful migration.", backup_path)
    except OSError:
        logger.exception(
            "Migration succeeded but failed to rename stashes.json to .bak; "
            "leaving original file in place."
        )

    mark_migration_applied(conn, _JSON_MIGRATION_NAME)
    return migrated


def merge_legacy_databases_into_mhes(
    legacy_temp_db_path: str, legacy_export_db_path: str, mhes_db_path: str,
) -> dict[str, int]:
    """Merge the old per-feature SQLite databases into the shared ``mhes.db``.

    Old database files are never modified, moved, or deleted — only
    their rows are copied over, deduplicated so re-running is safe.

    Args:
        legacy_temp_db_path: Path to the old ``temp_storage.db`` (holds
            the ``temp_stashes`` table), if it exists.
        legacy_export_db_path: Path to the old ``export_history.db``
            (holds the ``export_history`` table), if it exists.
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        Dict with the number of rows merged per table.
    """
    conn = get_connection(mhes_db_path)

    if migration_applied(conn, _MERGE_MIGRATION_NAME):
        logger.debug("Legacy database merge already applied; skipping.")
        return {"temp_stashes": 0, "export_history": 0}

    merged = {
        "temp_stashes": _merge_temp_stashes(legacy_temp_db_path, mhes_db_path),
        "export_history": _merge_export_history(legacy_export_db_path, mhes_db_path),
    }

    mark_migration_applied(conn, _MERGE_MIGRATION_NAME)
    logger.info(
        "Legacy database merge complete: %d temp stash(es), %d export history record(s) "
        "merged into %s.",
        merged["temp_stashes"], merged["export_history"], mhes_db_path,
    )
    return merged


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


def seed_development_team_import_config(mhes_db_path: str) -> dict[str, Any] | None:
    """Best-effort demo seed of Development Team's Excel column mapping.

    Unlike the other seed migrations in this module, "Development Team"
    is not one of MHES's guaranteed-to-exist default teams (only
    "Infrastructure Team" is created automatically) — it only exists in
    this environment because it was created manually while testing
    Phases 4-6. So this function is deliberately more forgiving than a
    real migration: if the team doesn't exist (e.g. on a fresh install
    elsewhere), it simply does nothing and is **not** marked applied,
    so it re-checks on every startup rather than permanently giving up.

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

    Same forgiving, environment-specific pattern as
    ``seed_development_team_import_config``: only does anything if a
    team with slug "development-team" already exists; no-ops (and is not
    marked applied) otherwise.

    Seeds a compact 4-column template — ``Technology`` (category),
    ``Task``, ``Hours`` (estimate), ``Notes`` (remarks) — deliberately
    without a "working_day" column, to demonstrate that a team's export
    can both relabel and drop columns relative to
    ``routes.export.DEFAULT_EXPORT_TEMPLATE`` (see
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


def migrate_kb_to_team_storage(
    legacy_kb_folder: str,
    legacy_embeddings_folder: str,
    teams_folder: str,
    mhes_db_path: str,
) -> dict[str, int] | None:
    """Move the shared ``kb_knowledge/``/``embeddings/`` folders into the
    default team's isolated storage tree (Phase 4 of multi-team support).

    All pre-Phase-4 Knowledge Base data implicitly belonged to the
    default team (``DEFAULT_TEAM_SLUG``), since there was no team concept
    when it was created — so that is the only team this migration ever
    writes into. Safe to call on every startup: no-ops once applied, and
    no-ops (until the default team exists) if ``create_default_team``
    hasn't run yet.

    Copies files into ``storage/teams/<slug>/{knowledge,embeddings}/``
    (does not delete anything from the legacy folders). Once every file
    has been copied, the legacy folders are renamed with a ``.bak``
    suffix — mirroring the existing ``stashes.json`` -> ``stashes.json.bak``
    convention — so they are clearly retired but trivially restorable.
    See the rollback plan in ``docs/ARCHITECTURE.md`` §5e.

    Args:
        legacy_kb_folder: The old, pre-Phase-4 ``kb_knowledge/`` folder.
        legacy_embeddings_folder: The old, pre-Phase-4 ``embeddings/`` folder.
        teams_folder: ``config["TEAMS_FOLDER"]`` — the ``storage/teams``
            root every team's isolated storage lives under.
        mhes_db_path: Path to the shared MHES SQLite database.

    Returns:
        ``{"knowledge_files": N, "embedding_files": M}`` counts of files
        copied, or None if the migration had already been applied, or if
        the default team does not exist yet (retried on next startup).
    """
    from repositories.team_repository import TeamRepository
    from utils.team_storage import team_embeddings_folder, team_kb_folder

    conn = get_connection(mhes_db_path)

    if migration_applied(conn, _KB_TEAM_STORAGE_MIGRATION_NAME):
        logger.debug("KB team-storage migration already applied; skipping.")
        return None

    team = TeamRepository(mhes_db_path).get_by_slug(DEFAULT_TEAM_SLUG)
    if team is None:
        logger.warning(
            "Cannot migrate Knowledge Base to team storage: no team with "
            "slug %r exists yet. Will retry on next startup once "
            "create_default_team has run.",
            DEFAULT_TEAM_SLUG,
        )
        return None

    dest_kb = team_kb_folder(teams_folder, team["slug"])
    dest_embeddings = team_embeddings_folder(teams_folder, team["slug"])

    copied = {
        "knowledge_files": _copy_folder_contents(legacy_kb_folder, dest_kb),
        "embedding_files": _copy_folder_contents(legacy_embeddings_folder, dest_embeddings),
    }

    _retire_legacy_folder(legacy_kb_folder)
    _retire_legacy_folder(legacy_embeddings_folder)

    mark_migration_applied(conn, _KB_TEAM_STORAGE_MIGRATION_NAME)
    logger.info(
        "Migrated Knowledge Base to team storage for team %r: "
        "%d knowledge file(s) -> %s, %d embedding file(s) -> %s.",
        team["name"], copied["knowledge_files"], dest_kb,
        copied["embedding_files"], dest_embeddings,
    )
    return copied


def _copy_folder_contents(src_folder: str, dest_folder: str) -> int:
    """Copy every regular file from ``src_folder`` into ``dest_folder`` (non-recursive).

    Skips ``.gitkeep`` placeholders. Returns the number of files copied.
    """
    if not os.path.isdir(src_folder):
        logger.info("No legacy folder at %s; nothing to migrate.", src_folder)
        return 0

    os.makedirs(dest_folder, exist_ok=True)
    count = 0
    for name in os.listdir(src_folder):
        if name == ".gitkeep":
            continue
        src_path = os.path.join(src_folder, name)
        if not os.path.isfile(src_path):
            continue
        shutil.copy2(src_path, os.path.join(dest_folder, name))
        count += 1
    return count


def _retire_legacy_folder(folder: str) -> None:
    """Rename a fully-migrated legacy folder to ``<folder>.bak``, if present."""
    if not os.path.isdir(folder):
        return
    backup_path = folder + ".bak"
    if os.path.isdir(backup_path):
        logger.warning(
            "Legacy folder %s already has a .bak counterpart; leaving %s in place untouched.",
            folder, folder,
        )
        return
    try:
        os.rename(folder, backup_path)
        logger.info("Renamed legacy folder %s to %s after migration.", folder, backup_path)
    except OSError:
        logger.exception(
            "Migration succeeded but failed to rename %s to %s; leaving original folder in place.",
            folder, backup_path,
        )


def _read_legacy_rows(db_path: str, table_name: str) -> list[sqlite3.Row]:
    """Best-effort read of all rows from a table in an old database file.

    Handles a missing database file and a missing/renamed table the same
    way: log and return no rows, rather than failing the whole startup.
    """
    if not os.path.isfile(db_path):
        logger.info("No legacy database found at %s; nothing to merge from it.", db_path)
        return []

    try:
        legacy_conn = sqlite3.connect(db_path)
        legacy_conn.row_factory = sqlite3.Row
        try:
            return legacy_conn.execute(f"SELECT * FROM {table_name}").fetchall()
        finally:
            legacy_conn.close()
    except sqlite3.Error:
        logger.exception(
            "Failed to read table '%s' from legacy database %s; skipping.", table_name, db_path
        )
        return []


def _merge_temp_stashes(legacy_db_path: str, mhes_db_path: str) -> int:
    from repositories.temp_repository import TempRepository

    rows = _read_legacy_rows(legacy_db_path, "temp_stashes")
    if not rows:
        return 0

    repo = TempRepository(mhes_db_path)
    migrated = 0
    for row in rows:
        record = dict(row)
        if repo.exists(record["id"]):
            continue
        try:
            repo.insert(record)
            migrated += 1
        except sqlite3.Error:
            logger.exception(
                "Failed to merge temp stash id=%s into %s.", record.get("id"), mhes_db_path
            )
    return migrated


def _merge_export_history(legacy_db_path: str, mhes_db_path: str) -> int:
    from repositories.team_repository import TeamRepository
    from services.export_history_service import ExportHistoryService

    rows = _read_legacy_rows(legacy_db_path, "export_history")
    if not rows:
        return 0

    # Legacy rows predate any team concept — attribute them all to the
    # default team (Phase 6 requires every export_history row to have a
    # team_id; ExportHistoryService.insert_history now makes it mandatory).
    # Requires create_default_team to have already run — app.py's startup
    # order guarantees this.
    team = TeamRepository(mhes_db_path).get_by_slug(DEFAULT_TEAM_SLUG)
    if team is None:
        logger.warning(
            "Cannot merge legacy export_history rows: no team with slug %r "
            "exists yet. Will retry on next startup once create_default_team has run.",
            DEFAULT_TEAM_SLUG,
        )
        return 0

    service = ExportHistoryService(mhes_db_path)
    existing_keys = {(h["file_name"], h["created_at"]) for h in service.get_history()}

    migrated = 0
    for row in rows:
        record = dict(row)
        key = (record.get("file_name"), record.get("created_at"))
        if key in existing_keys:
            continue
        try:
            service.insert_history(
                project_name=record.get("project_name") or "",
                created_by=record.get("created_by") or "",
                team_id=team["id"],
                export_date=record.get("export_date") or record.get("created_at") or "",
                file_name=record["file_name"],
                file_url=record.get("file_url") or "",
                file_size=record.get("file_size") or 0,
                total_tasks=record.get("total_tasks") or 0,
                total_hours=record.get("total_hours") or 0,
                created_at=record.get("created_at"),
            )
            migrated += 1
        except sqlite3.Error:
            logger.exception(
                "Failed to merge export history file_name=%s into %s.",
                record.get("file_name"), mhes_db_path,
            )
    return migrated
