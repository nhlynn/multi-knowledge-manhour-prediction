"""Reusable cleanup logic for expired Preview stashes.

Shared by the APScheduler job (scheduler.py) and any manual/CLI use, so
there is exactly one place that decides how expiry is determined and
logged.
"""

import logging

from flask import Flask

from scheduler.temp_data_service import TempDataService

logger = logging.getLogger(__name__)


def delete_expired_temp_data(app: Flask) -> int:
    """Delete Preview stashes older than the configured retention period.

    Args:
        app: Flask application instance (used for config and logging).

    Returns:
        Number of stashes deleted. Returns 0 if the run failed.
    """
    retention_days = app.config["TEMP_DATA_RETENTION_DAYS"]
    logger.info("Temp data cleanup started (retention_days=%d).", retention_days)

    try:
        service = TempDataService(db_path=app.config["MHES_DB_PATH"])
        removed = service.remove_older_than(days=retention_days)
    except Exception:
        logger.exception("Temp data cleanup failed.")
        return 0

    if not removed:
        logger.info("Temp data cleanup finished: nothing to delete.")
        return 0

    logger.info("Temp data cleanup finished: deleted %d stash(es).", len(removed))
    for stash in removed:
        logger.info(
            "  deleted stash id=%s stashedAt=%s projectName=%r",
            stash.get("id"), stash.get("stashedAt"), stash.get("projectName"),
        )
    return len(removed)
