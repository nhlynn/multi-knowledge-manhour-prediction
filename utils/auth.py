"""Session helpers for MHES.

The Flask session (signed cookie, ``SECRET_KEY`` from ``config.py``)
stores only ``user_id``/``username``/``team_id``/``role`` — the full user
record is re-read from the ``users`` table on each request, the same
"reconstruct from storage per request" style already used by every other
service in this app (e.g. ``ExcelService``/``EmbeddingService`` are
re-instantiated per route call rather than cached).
"""

from typing import Any

from flask import current_app, session

from repositories.user_repository import UserRepository


def get_current_user() -> dict[str, Any] | None:
    """Return the logged-in user's record, or None if no one is logged in."""
    user_id = session.get("user_id")
    if user_id is None:
        return None
    repo = UserRepository(current_app.config["MHES_DB_PATH"])
    return repo.get_by_id(user_id)
