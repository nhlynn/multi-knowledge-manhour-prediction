"""APScheduler integration for MHES.

Runs the temp data cleanup job (scheduler/temp_data_cleanup.py) on a cron
schedule, replacing the old Windows Task Scheduler + .bat file approach.
The scheduler lives in the same process as the Flask app, so it only
runs while the app is running (no separate OS-level task needed).

Job organization: ``init_scheduler`` only builds/starts the
``BackgroundScheduler`` itself; each job family gets its own
``_register_*_jobs`` function (currently just temp data cleanup) so
adding a second kind of scheduled job later means adding one more
function here, not growing ``init_scheduler`` into a monolith.
"""

import atexit
import logging
import os

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask
from pytz.exceptions import UnknownTimeZoneError

from scheduler.temp_data_cleanup import delete_expired_temp_data

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

_FALLBACK_TIMEZONE = "UTC"


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

    timezone = _resolve_timezone(app.config["TEMP_DATA_TIMEZONE"])
    scheduler = BackgroundScheduler(timezone=timezone)

    _register_temp_data_cleanup_jobs(scheduler, app, timezone)

    scheduler.start()
    logger.info("APScheduler started (timezone=%s).", timezone)

    atexit.register(lambda: scheduler.shutdown(wait=False))
    _scheduler = scheduler
    return scheduler


def _resolve_timezone(configured_timezone: str) -> str:
    """Validate the configured timezone, falling back to UTC (with a
    clear log message) rather than crashing the entire app at startup
    if ``TEMP_DATA_TIMEZONE`` is misconfigured.
    """
    try:
        # BackgroundScheduler/CronTrigger accept a tz name string directly,
        # but resolve it eagerly here (via pytz, the same library APScheduler
        # uses internally) so a bad value is caught with a clear message
        # instead of surfacing from deep inside APScheduler's own startup.
        pytz.timezone(configured_timezone)
        return configured_timezone
    except UnknownTimeZoneError:
        logger.error(
            "Configured TEMP_DATA_TIMEZONE=%r is not a recognized timezone; "
            "falling back to %r. Fix TEMP_DATA_TIMEZONE in your environment/.env.",
            configured_timezone, _FALLBACK_TIMEZONE,
        )
        return _FALLBACK_TIMEZONE


def _register_temp_data_cleanup_jobs(
    scheduler: BackgroundScheduler, app: Flask, timezone: str,
) -> None:
    """Register one cron job per configured cleanup time.

    A malformed entry in ``TEMP_DATA_CLEANUP_TIMES`` is logged and
    skipped rather than crashing app startup — every other, well-formed
    time in the list is still scheduled normally.
    """
    registered = 0
    for time_str in app.config["TEMP_DATA_CLEANUP_TIMES"]:
        try:
            hour, minute = _parse_hh_mm(time_str)
        except ValueError:
            logger.error(
                "Skipping invalid entry in TEMP_DATA_CLEANUP_TIMES: %r "
                "(expected \"HH:MM\").", time_str,
            )
            continue

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
        registered += 1

    logger.info("Registered %d temp data cleanup job(s).", registered)


def _parse_hh_mm(time_str: str) -> tuple[int, int]:
    """Parse an "HH:MM" string into (hour, minute) ints.

    Raises:
        ValueError: If ``time_str`` isn't a valid "HH:MM" 24-hour time.
    """
    hour_str, minute_str = time_str.strip().split(":")
    hour, minute = int(hour_str), int(minute_str)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{time_str!r} is out of range for a 24-hour HH:MM time.")
    return hour, minute
