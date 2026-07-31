"""Phase 4 of multi-team support: moving the Knowledge Base onto per-team storage."""

import logging
import os
import shutil

from database.db import get_connection, mark_migration_applied, migration_applied
from utils.migrations.team_seed import DEFAULT_TEAM_SLUG

logger = logging.getLogger(__name__)

_KB_TEAM_STORAGE_MIGRATION_NAME = "migrate_kb_to_team_storage_v1"


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
