"""Import of pre-multi-team, pre-SQLite legacy data stores into ``mhes.db``.

Covers two independent legacy sources, both superseded by SQLite tables
in the shared database:

- ``temp_data/stashes.json`` -> ``temp_stashes`` table
  (``migrate_stashes_json_to_sqlite``).
- The old per-feature databases, ``temp_data/temp_storage.db`` and
  ``exports/export_history.db`` -> ``temp_stashes`` / ``export_history``
  tables in ``mhes.db`` (``merge_legacy_databases_into_mhes``).
"""

import json
import logging
import os
import sqlite3
from datetime import datetime

from database.db import get_connection, mark_migration_applied, migration_applied
from utils.migrations.team_seed import DEFAULT_TEAM_SLUG

logger = logging.getLogger(__name__)

_JSON_MIGRATION_NAME = "stashes_json_to_sqlite_v1"
_MERGE_MIGRATION_NAME = "merge_legacy_dbs_into_mhes_v1"


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
