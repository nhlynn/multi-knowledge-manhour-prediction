"""Manual/ad-hoc trigger for the temp data cleanup job.

Automatic cleanup runs on its own via APScheduler (see scheduler.py),
started in-process whenever the Flask app is running — no external
scheduler or script is required for that. This script is only for
forcing an out-of-band run (e.g. to verify behavior, or clean up
immediately without waiting for the next 10:00/14:00 trigger). Run with:

    venv\\Scripts\\python.exe scheduler\\cleanup_temp_data.py [--days 7]
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from flask import Flask

from config import Config
from scheduler.temp_data_cleanup import delete_expired_temp_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=None,
        help="Delete stashes older than this many days "
             "(default: TEMP_DATA_RETENTION_DAYS config value).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # A minimal app is enough here — just need config access, not the
    # full create_app() (blueprints, scheduler, etc.) for a one-off run.
    app = Flask(__name__)
    app.config.from_object(Config)
    if args.days is not None:
        app.config["TEMP_DATA_RETENTION_DAYS"] = args.days

    delete_expired_temp_data(app)


if __name__ == "__main__":
    main()
