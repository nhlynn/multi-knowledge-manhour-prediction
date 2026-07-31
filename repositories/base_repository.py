"""Shared base class for MHES's raw-SQL repositories.

Every repository in this package independently duplicated the same
three lines of connection/schema boilerplate, and the two "one config
row per team" repositories (``team_import_config_repository.py``,
``team_export_template_repository.py``) duplicated an identical
insert-or-update pattern differing only in table/column names. This
module factors both out. No business logic lives here — only
mechanical row access, mirroring each repository's own stated scope.

Table/column names passed to the helpers below are always fixed,
code-controlled strings (never derived from request/user input) —
consistent with how every other dynamic SQL fragment in this codebase
is built (see ``repositories/temp_repository.py::list_page`` and
``services/export_history_service.py``'s filtered queries).
"""

import sqlite3
from typing import Any

from database.db import ensure_schema, get_connection


class BaseRepository:
    """Common connection/schema plumbing plus small query helpers.

    Subclasses call ``super().__init__(db_path, _SCHEMA)`` with their
    own module-level schema script, then implement their own
    table-specific methods (optionally using the ``_fetch_*``/
    ``_upsert_by_unique_column`` helpers below).
    """

    def __init__(self, db_path: str, schema_sql: str) -> None:
        self.db_path = db_path
        ensure_schema(self._conn(), schema_sql)

    def _conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    def _fetch_one(self, query: str, params: tuple = ()) -> sqlite3.Row | None:
        """Run a SELECT expected to return at most one row."""
        return self._conn().execute(query, params).fetchone()

    def _fetch_one_dict(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        """Same as ``_fetch_one``, decoded into a plain dict (or None)."""
        row = self._fetch_one(query, params)
        return dict(row) if row is not None else None

    def _fetch_all(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a SELECT and return every matching row."""
        return self._conn().execute(query, params).fetchall()

    def _fetch_all_dicts(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Same as ``_fetch_all``, decoded into plain dicts."""
        return [dict(row) for row in self._fetch_all(query, params)]

    def _upsert_by_unique_column(
        self,
        *,
        table: str,
        unique_column: str,
        unique_value: Any,
        data_column: str,
        data_value: str,
        created_at: str,
        existing: dict[str, Any] | None,
    ) -> None:
        """Insert a new row, or update ``data_column`` if one already exists.

        Shared shape behind both ``TeamImportConfigRepository.upsert()``
        and ``TeamExportTemplateRepository.upsert()``: check-exists (by
        the caller, passed in as ``existing``) -> INSERT or UPDATE ->
        the caller re-reads the row itself. ``table``/``unique_column``/
        ``data_column`` are always fixed, code-controlled strings, never
        user input.
        """
        conn = self._conn()
        with conn:
            if existing is None:
                conn.execute(
                    f"INSERT INTO {table} ({unique_column}, {data_column}, created_at) "
                    f"VALUES (?, ?, ?)",
                    (unique_value, data_value, created_at),
                )
            else:
                conn.execute(
                    f"UPDATE {table} SET {data_column} = ? WHERE {unique_column} = ?",
                    (data_value, unique_value),
                )
