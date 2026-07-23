"""Shared SQLite connection helper for MHES.

All application data (Preview temp stashes, export history, and any
future tables) lives in a single SQLite database file: ``database/mhes.db``.
This module is the only place that opens a raw ``sqlite3`` connection —
repositories and services call ``get_connection()`` and then ensure
their own tables exist via ``ensure_schema()``.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

DB_FILENAME = "mhes.db"

# One SQLite connection per (thread, db_path), reused across requests and
# the APScheduler background thread — avoids sharing a single connection
# object across threads while still avoiding a reopen per call.
_local = threading.local()


def get_db_path(database_folder: str) -> str:
    """Return the path to the shared MHES SQLite database file."""
    return os.path.join(database_folder, DB_FILENAME)


def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a thread-local SQLite connection to ``db_path``, opened once per thread.

    Configures WAL journal mode and a busy timeout so the Flask
    request-handling threads, the APScheduler background thread, and any
    standalone CLI script can all safely access the same database file
    concurrently.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        An open sqlite3.Connection with row_factory set to sqlite3.Row.
    """
    cache = getattr(_local, "connections", None)
    if cache is None:
        cache = {}
        _local.connections = cache

    conn = cache.get(db_path)
    if conn is not None:
        return conn

    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS db_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
    except sqlite3.Error:
        logger.exception("Failed to open SQLite connection at %s", db_path)
        raise

    cache[db_path] = conn
    logger.debug("Opened shared SQLite connection at %s", db_path)
    return conn


def ensure_schema(conn: sqlite3.Connection, schema_sql: str) -> None:
    """Run a ``CREATE TABLE/INDEX IF NOT EXISTS`` script against a connection."""
    conn.executescript(schema_sql)


def migration_applied(conn: sqlite3.Connection, name: str) -> bool:
    """Return whether a one-shot migration with this name has already run."""
    row = conn.execute("SELECT 1 FROM db_migrations WHERE name = ?", (name,)).fetchone()
    return row is not None


def mark_migration_applied(conn: sqlite3.Connection, name: str) -> None:
    """Record that a one-shot migration has run, so it is never re-applied."""
    conn.execute(
        "INSERT OR REPLACE INTO db_migrations (name, applied_at) VALUES (?, ?)",
        (name, datetime.now().isoformat()),
    )
