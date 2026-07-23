"""APScheduler integration for MHES.

Runs the temp data cleanup job (scheduler/temp_data_cleanup.py) on a cron
schedule, replacing the old Windows Task Scheduler + .bat file approach.
The scheduler lives in the same process as the Flask app, so it only
runs while the app is running (no separate OS-level task needed).
"""

import atexit
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask

from scheduler.temp_data_cleanup import delete_expired_temp_data

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def init_scheduler(app: Flask) -> BackgroundScheduler | None:
    """Create and start the background scheduler for the given app.

    Safe to call multiple times: if a scheduler is already running for
    this process, it is returned as-is instead of creating a second one.
    Also guards against Flask's debug-mode reloader, which spawns a
    parent "monitor" process and a child "reloaded" process — only the
    child (where WERKZEUG_RUN_MAIN=='true') starts the scheduler, so
    debug mode doesn't end up running the job twice.

    Args:
        app: Flask application instance.

    Returns:
        The running BackgroundScheduler, or None if skipped (e.g. the
        debug-mode parent monitor process).
    """
    global _scheduler

    if _scheduler is not None:
        logger.info("Scheduler already running; skipping re-initialization.")
        return _scheduler

    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        logger.info("Skipping scheduler startup in Werkzeug reloader monitor process.")
        return None

    timezone = app.config["TEMP_DATA_TIMEZONE"]
    scheduler = BackgroundScheduler(timezone=timezone)

    for time_str in app.config["TEMP_DATA_CLEANUP_TIMES"]:
        hour, minute = _parse_hh_mm(time_str)
        job_id = f"temp_data_cleanup_{hour:02d}{minute:02d}"
        scheduler.add_job(
            func=delete_expired_temp_data,
            args=(app,),
            trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
            id=job_id,
            replace_existing=True,  # re-registering the same id updates it, never duplicates
            misfire_grace_time=3600,  # still run if the app was down at the trigger time, within 1h
        )
        logger.info(
            "Scheduled temp data cleanup job '%s' for %02d:%02d %s daily.",
            job_id, hour, minute, timezone,
        )

    scheduler.start()
    logger.info("APScheduler started (timezone=%s).", timezone)

    atexit.register(lambda: scheduler.shutdown(wait=False))
    _scheduler = scheduler
    return scheduler


def _parse_hh_mm(time_str: str) -> tuple[int, int]:
    """Parse an "HH:MM" string into (hour, minute) ints."""
    hour_str, minute_str = time_str.strip().split(":")
    return int(hour_str), int(minute_str)
