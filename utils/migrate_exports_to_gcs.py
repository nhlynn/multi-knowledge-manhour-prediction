"""One-off migration: backfill pre-GCS export files into Google Cloud Storage.

Before the export storage migration (see services/gcs_service.py),
generated Excel exports were saved directly on local disk under
exports/<file_name>, with export_history.file_path storing that local
absolute path. This script uploads every such file still present on disk
to the configured GCS bucket, and repoints its export_history.file_path
at the new GCS object path — the same convention new exports already use
(mhes/bcmm/1001/<file_name>).

Unlike the automatic migrations in utils/migration.py (which run on every
app startup and are safe no-ops), this is deliberately NOT run
automatically: it needs real GCP credentials configured (GCP_BUCKET_NAME,
GOOGLE_APPLICATION_CREDENTIALS — see .env.example), and moving/uploading
existing files is significant enough to be a one-time, user-triggered
operation instead.

Usage:
    venv\\Scripts\\python.exe -m utils.migrate_exports_to_gcs [--dry-run] [--delete-local]

Options:
    --dry-run       Preview what would be migrated — no upload, no
                     database changes.
    --delete-local  Delete the local file after a successful upload.
                     Default is to leave it in place: exports/ is
                     git-ignored scratch space, so keeping the original
                     around costs nothing and is the safer choice until
                     you've verified the migrated files download
                     correctly from GCS.

Records whose file_path already looks like a GCS object path are skipped
(idempotent — safe to re-run). Records whose local file is missing are
reported and skipped (nothing to upload). Records that fail to upload are
reported and left untouched, so a partial run can simply be re-run later.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from flask import Flask

from config import Config
from services.export_history_service import ExportHistoryService
from services.gcs_service import GCSError, is_local_path, upload_excel_to_gcs

logger = logging.getLogger(__name__)


def migrate(dry_run: bool = False, delete_local: bool = False) -> dict[str, int]:
    """Upload every pre-migration local export file to GCS and repoint its
    export_history.file_path at the new GCS object path.

    Must be called inside a Flask app context (see main()) — the
    underlying GCS calls read bucket/project config via ``current_app``.

    Args:
        dry_run: If True, only log what would happen; no upload or
            database write occurs.
        delete_local: If True, delete the local file after a successful
            upload. Default False — the local copy is left in place.

    Returns:
        Counts: ``{"migrated": n, "already_gcs": n, "missing": n, "failed": n}``.
    """
    service = ExportHistoryService(db_path=Config.MHES_DB_PATH)
    history = service.get_history()

    counts = {"migrated": 0, "already_gcs": 0, "missing": 0, "failed": 0}

    for record in history:
        file_name = record["file_name"]
        file_path = record.get("file_path")

        if file_path and not is_local_path(file_path):
            logger.info("Already on GCS, skipping: %s (file_path=%s)", file_name, file_path)
            counts["already_gcs"] += 1
            continue

        # file_path is either a local absolute path, or None (oldest rows,
        # from before the file_path column existed at all) — either way,
        # reconstruct/use the local path the file would be at.
        local_path = file_path or os.path.join(Config.EXPORT_FOLDER, file_name)
        if not os.path.isfile(local_path):
            logger.warning("Local file missing, cannot migrate: %s (expected at %s)", file_name, local_path)
            counts["missing"] += 1
            continue

        if dry_run:
            logger.info("[DRY RUN] Would upload: %s -> mhes/bcmm/1001/%s", local_path, file_name)
            counts["migrated"] += 1
            continue

        try:
            object_path = upload_excel_to_gcs(local_path, file_name)
            service.update_file_path(record["id"], object_path)
            logger.info("Migrated id=%s: %s -> %s", record["id"], local_path, object_path)
            counts["migrated"] += 1
        except GCSError:
            logger.exception("Failed to migrate id=%s (%s) to GCS; left untouched.", record["id"], file_name)
            counts["failed"] += 1
            continue

        if delete_local:
            try:
                os.remove(local_path)
                logger.info("Deleted local file after successful migration: %s", local_path)
            except OSError:
                logger.warning("Migrated but could not delete local file: %s", local_path)

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only; no upload or database changes.",
    )
    parser.add_argument(
        "--delete-local", action="store_true",
        help="Delete the local file after a successful upload (default: keep it).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # A minimal app is enough here — just need config access (for GCS
    # bucket/project settings and the DB path), not the full create_app()
    # (blueprints, scheduler, etc.) for a one-off run.
    app = Flask(__name__)
    app.config.from_object(Config)

    if not app.config.get("GCP_BUCKET_NAME"):
        logger.error("GCP_BUCKET_NAME is not configured. Set it in your .env file first (see .env.example).")
        sys.exit(1)

    with app.app_context():
        counts = migrate(dry_run=args.dry_run, delete_local=args.delete_local)

    logger.info(
        "Done%s. Migrated=%d, already on GCS=%d, missing locally=%d, failed=%d.",
        " (dry run)" if args.dry_run else "",
        counts["migrated"], counts["already_gcs"], counts["missing"], counts["failed"],
    )
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
