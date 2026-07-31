"""Reusable cleanup logic for expired Preview stashes.

Shared by the APScheduler job (scheduler.py) and any manual/CLI use, so
there is exactly one place that decides how expiry is determined and
logged.
"""

import logging
import sqlite3
import time

from flask import Flask

from scheduler.temp_data_service import TempDataService

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2


def delete_expired_temp_data(app: Flask) -> int:
    """Delete Preview stashes older than the configured retention period.

    Retries a bounded number of times on a transient SQLite lock error
    (``database is locked`` — realistic here since the same file is
    shared with Flask's request-handling threads under WAL mode) before
    giving up; any other failure is not retried.

    Args:
        app: Flask application instance (used for config and logging).

    Returns:
        Number of stashes deleted. Returns 0 if the run failed.
    """
    retention_days = app.config["TEMP_DATA_RETENTION_DAYS"]
    logger.info("Temp data cleanup started (retention_days=%d).", retention_days)
    started_at = time.monotonic()

    try:
        removed = _remove_expired_with_retry(app.config["MHES_DB_PATH"], retention_days)
    except Exception:
        logger.exception(
            "Temp data cleanup failed after %.1fs.", time.monotonic() - started_at,
        )
        return 0

    elapsed = time.monotonic() - started_at
    if not removed:
        logger.info("Temp data cleanup finished in %.1fs: nothing to delete.", elapsed)
        return 0

    logger.info(
        "Temp data cleanup finished in %.1fs: deleted %d stash(es).", elapsed, len(removed),
    )
    for stash in removed:
        logger.info(
            "Temp data cleanup: deleted stash id=%s stashedAt=%s projectName=%r",
            stash.get("id"), stash.get("stashedAt"), stash.get("projectName"),
        )
    return len(removed)


def _remove_expired_with_retry(db_path: str, retention_days: int) -> list[dict]:
    """Call ``TempDataService.remove_older_than``, retrying a bounded
    number of times if SQLite reports the database as transiently locked.

    Raises whatever the final attempt raised if every attempt fails.
    """
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            service = TempDataService(db_path=db_path)
            return service.remove_older_than(days=retention_days)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == _MAX_ATTEMPTS:
                raise
            last_error = e
            logger.warning(
                "Temp data cleanup: database locked (attempt %d/%d); retrying in %ds.",
                attempt, _MAX_ATTEMPTS, _RETRY_BACKOFF_SECONDS,
            )
            time.sleep(_RETRY_BACKOFF_SECONDS)
    raise last_error  # pragma: no cover - unreachable (loop always returns or raises)
