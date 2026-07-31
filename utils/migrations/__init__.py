"""One-shot database migrations for MHES.

This package replaces the former single ``utils/migration.py`` module —
split by concern for readability, with every previously public name
re-exported here so existing callers (``app.py``,
``services/export_history_service.py``) are unaffected beyond their
import path.

Seven migrations run at application startup (see ``app.py``), all safe
to call on every startup — each no-ops once recorded as applied, and
no-ops if there is nothing to migrate (e.g. a fresh install):

1. ``migrate_stashes_json_to_sqlite`` — imports the legacy
   ``temp_data/stashes.json`` file (if still present) directly into the
   shared ``mhes.db``. (:mod:`utils.migrations.legacy_import`)
2. ``merge_legacy_databases_into_mhes`` — merges rows from the
   now-superseded per-feature databases (``temp_data/temp_storage.db``,
   ``exports/export_history.db``) into ``mhes.db``. The old database
   files are left on disk untouched; only their rows are copied.
   (:mod:`utils.migrations.legacy_import`)
3. ``create_default_team`` — creates the ``teams`` table (Phase 1 of
   multi-team support) and seeds a single "Infrastructure Team" row so
   existing (pre-multi-team) data has a team to be attributed to in a
   later phase. Does not touch Knowledge Base, embeddings, or any
   existing table's rows/columns. (:mod:`utils.migrations.team_seed`)
4. ``migrate_kb_to_team_storage`` — copies the old shared
   ``kb_knowledge/`` and ``embeddings/`` folders into the default team's
   isolated ``storage/teams/<slug>/{knowledge,embeddings}`` tree (Phase 4
   of multi-team support), then retires the old folders (renamed to
   ``.bak``, never deleted). Must run after ``create_default_team``.
   (:mod:`utils.migrations.kb_storage`)
5. ``create_default_admin_user`` — creates the ``users`` table (Phase 2
   of multi-team support: authentication) and seeds a single Admin user
   attached to the default team, so there is a way to log in on a fresh
   install. Must run after ``create_default_team``.
   (:mod:`utils.migrations.user_seed`)
6. ``seed_development_team_import_config`` — best-effort demo seed of a
   Development Team Excel column mapping (Phase 7 of multi-team
   support). Unlike the migrations above, this is environment-specific,
   not a guaranteed-to-apply product migration: it only does anything if
   a team with slug "development-team" already exists (which it does in
   this environment, created manually while testing earlier phases). On
   a fresh install without that team, it harmlessly no-ops and is not
   marked applied, so it keeps checking on every startup.
   (:mod:`utils.migrations.demo_seeds`)
7. ``seed_development_team_export_template`` — same best-effort,
   environment-specific pattern as #6, but for Development Team's Excel
   *export* column template (Phase 8 of multi-team support).
   (:mod:`utils.migrations.demo_seeds`)
"""

from utils.migrations.demo_seeds import (
    seed_development_team_export_template,
    seed_development_team_import_config,
)
from utils.migrations.kb_storage import migrate_kb_to_team_storage
from utils.migrations.legacy_import import (
    merge_legacy_databases_into_mhes,
    migrate_stashes_json_to_sqlite,
)
from utils.migrations.team_seed import (
    DEFAULT_TEAM_NAME,
    DEFAULT_TEAM_SLUG,
    create_default_team,
)
from utils.migrations.user_seed import (
    DEFAULT_ADMIN_ROLE,
    DEFAULT_ADMIN_USERNAME,
    create_default_admin_user,
)

# Kept importable from the package root for the same reason
# DEFAULT_TEAM_SLUG is: seed_development_team_import_config/_export_template
# reference it directly by name in their own module.
from utils.migrations.demo_seeds import DEVELOPMENT_TEAM_SLUG

__all__ = [
    "migrate_stashes_json_to_sqlite",
    "merge_legacy_databases_into_mhes",
    "create_default_team",
    "migrate_kb_to_team_storage",
    "create_default_admin_user",
    "seed_development_team_import_config",
    "seed_development_team_export_template",
    "DEFAULT_TEAM_NAME",
    "DEFAULT_TEAM_SLUG",
    "DEFAULT_ADMIN_USERNAME",
    "DEFAULT_ADMIN_ROLE",
    "DEVELOPMENT_TEAM_SLUG",
]
