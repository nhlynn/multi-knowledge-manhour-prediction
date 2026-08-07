"""Raw SQL data access for the ``users`` table.

No business logic (password hashing/verification, session handling)
lives here — that belongs to ``services.auth_service.AuthService`` and
``routes/auth.py`` respectively. Mirrors the style of
``repositories/team_repository.py`` and ``repositories/temp_repository.py``.
"""

import logging
from typing import Any

from repositories.base_repository import BaseRepository

logger = logging.getLogger(__name__)

VALID_ROLES = ("Admin", "Team Manager")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    role TEXT NOT NULL CHECK(role IN ('Admin', 'Team Manager')),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active', 'Inactive')),
    last_login TEXT,
    updated_at TEXT,
    password_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_team_id ON users(team_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
"""


class UserRepository(BaseRepository):
    """Repository for CRUD access to the ``users`` table."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path, _SCHEMA)
        self._ensure_email_column()
        self._ensure_status_and_audit_columns()

    def _ensure_status_and_audit_columns(self) -> None:
        """Add ``status``/``last_login``/``updated_at``/``password_changed_at``
        for databases created before they existed.

        Mirrors ``TeamRepository._ensure_description_and_status_columns``
        — ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` only applies to
        brand new databases, so an existing ``users`` table needs
        explicit ALTERs. Existing accounts are defaulted to
        ``status = 'Active'`` (via the column's own DEFAULT, so no
        pre-existing account is silently locked out); the three
        timestamp columns are nullable and left unbackfilled — there is
        no historical last-login/update/password-change data to
        recover, so "unknown" is represented as NULL rather than a
        fabricated value. Safe to run on every service construction —
        all four ALTERs are no-ops once applied. Existing columns
        (``id``, ``username``, ``password_hash``, ``email``,
        ``team_id``, ``role``, ``created_at``) are untouched.
        """
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "status" not in columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'Active' "
                "CHECK(status IN ('Active', 'Inactive'))"
            )
            logger.info("Added status column to users table (existing accounts defaulted to 'Active').")
        if "last_login" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
            logger.info("Added last_login column to users table.")
        if "updated_at" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")
            logger.info("Added updated_at column to users table.")
        if "password_changed_at" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
            logger.info("Added password_changed_at column to users table.")

    def _ensure_email_column(self) -> None:
        """Add ``email`` for databases created before it existed (Forgot
        Password support).

        Mirrors ``ExportHistoryService._ensure_file_path_column`` —
        ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` only applies to
        brand new databases, so an existing ``users`` table needs an
        explicit ALTER. Nullable and not backfilled: every existing
        account simply has no email until one is set through some future
        admin/profile flow, and Forgot Password treats "no email on
        file" the same as "no account" (see
        ``AuthService.request_password_reset``) — safe to run on every
        service construction, since the ALTER and index creation are
        both no-ops once applied.
        """
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "email" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
            logger.info("Added email column to users table.")
        # A separate (not inline) unique index, since SQLite's ALTER TABLE
        # ADD COLUMN can't attach a UNIQUE constraint directly. SQLite
        # treats NULLs as distinct in a unique index, so any number of
        # accounts with no email yet can coexist — only a real duplicate
        # email is rejected.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")

    def insert(
        self,
        *,
        username: str,
        password_hash: str,
        team_id: int,
        role: str,
        created_at: str,
        email: str | None = None,
        status: str = "Active",
        password_changed_at: str | None = None,
    ) -> dict[str, Any]:
        """Insert a new user row.

        Args:
            username: Unique login name.
            password_hash: Hashed password (never pass plaintext here).
            team_id: Id of the team this user belongs to.
            role: One of ``"Admin"``, ``"Team Manager"``.
            created_at: ISO datetime string.
            email: Optional email address.
            status: 'Active' or 'Inactive'. Defaults to 'Active', same
                as the column's own DEFAULT, for callers (e.g.
                ``utils/migrations/user_seed.py``) that don't pass it.
            password_changed_at: ISO datetime the password was set, if
                the caller wants this recorded at creation time (a
                new account's password was, by definition, just set).
                Left ``None`` (unknown) if not given.

        Returns:
            The newly created user record.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO users
                    (username, password_hash, team_id, role, created_at,
                     email, status, password_changed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (username, password_hash, team_id, role, created_at,
                 email, status, password_changed_at),
            )
        record = self.get_by_id(cursor.lastrowid)
        assert record is not None
        logger.info(
            "Created user id=%s username=%r role=%r team_id=%s",
            cursor.lastrowid, username, role, team_id,
        )
        return record

    def update(
        self,
        user_id: int,
        *,
        username: str,
        email: str | None,
        team_id: int,
        role: str,
        status: str,
        updated_at: str,
    ) -> dict[str, Any]:
        """Update an existing user's editable profile fields.

        Deliberately has no ``password_hash`` parameter — changing a
        password is a distinct, more sensitive operation
        (``update_password``, used by Reset Password) and is never
        folded into a general profile edit like this one.

        Args:
            user_id: The user to update.
            username: New username.
            email: New email (or ``None`` to clear it).
            team_id: New team assignment.
            role: New role.
            status: 'Active' or 'Inactive'.
            updated_at: ISO datetime string for this edit.

        Returns:
            The updated user record.
        """
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET username = ?, email = ?, team_id = ?, role = ?, "
                "status = ?, updated_at = ? WHERE id = ?",
                (username, email, team_id, role, status, updated_at, user_id),
            )
        record = self.get_by_id(user_id)
        assert record is not None
        logger.info(
            "Updated user id=%s username=%r role=%r team_id=%s status=%r",
            user_id, username, role, team_id, status,
        )
        return record

    def get_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Return a single user by id, or None if not found."""
        return self._fetch_one_dict("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        """Return a single user by username, or None if not found."""
        return self._fetch_one_dict("SELECT * FROM users WHERE username = ?", (username,))

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        """Return a single user by email, or None if not found.

        Used by the Forgot Password flow (``AuthService.request_password_reset``)
        — never by the login path, which still authenticates by username.
        """
        return self._fetch_one_dict("SELECT * FROM users WHERE email = ?", (email,))

    def update_password(self, user_id: int, password_hash: str) -> bool:
        """Update a user's password hash (e.g. after a Reset Password completion).

        Args:
            user_id: Id of the user to update.
            password_hash: Already-hashed password (never pass plaintext here).

        Returns:
            True if a row was updated, False if no user with that id exists.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
            )
        updated = cursor.rowcount > 0
        if updated:
            logger.info("Updated password hash for user_id=%s", user_id)
        return updated

    def update_last_login(self, user_id: int, last_login: str) -> bool:
        """Record a successful login's timestamp.

        Called only from ``AuthService.authenticate``'s success path
        (after credentials have already been verified) — never on a
        failed login attempt, so ``last_login`` only ever reflects an
        actual successful authentication, never an attempt.

        Args:
            user_id: Id of the user who just logged in successfully.
            last_login: ISO datetime string for this login.

        Returns:
            True if a row was updated, False if no user with that id exists.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?", (last_login, user_id)
            )
        updated = cursor.rowcount > 0
        if updated:
            logger.info("Updated last_login for user_id=%s", user_id)
        return updated

    def set_password(self, user_id: int, *, password_hash: str, changed_at: str) -> bool:
        """Set a user's password hash and record when it changed.

        Used by the Forgot Password / Reset Password flow
        (``AuthService.reset_password``) — both the self-service and
        Admin-triggered ("Send Reset Password Link") paths end up here,
        since both go through the same token-based reset. Kept entirely
        separate from ``update_password`` above, which nothing
        currently calls.

        Args:
            user_id: Id of the user to update.
            password_hash: Already-hashed password (never pass
                plaintext here).
            changed_at: ISO datetime string recording when this
                happened.

        Returns:
            True if a row was updated, False if no user with that id exists.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                (password_hash, changed_at, user_id),
            )
        updated = cursor.rowcount > 0
        if updated:
            logger.info(
                "Password hash and password_changed_at updated for user_id=%s (admin reset).", user_id,
            )
        return updated

    def delete(self, user_id: int) -> bool:
        """Delete a user row by id. Returns True if a row was removed.

        No safety checks here (self-deletion, last-active-Admin, etc.)
        — that's ``services.user_service.delete_user``'s job. This
        method only knows how to remove the row itself.
        """
        conn = self._conn()
        with conn:
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted user id=%s", user_id)
        return deleted

    def count_active_admins(self) -> int:
        """Return the number of accounts with role='Admin' and status='Active'.

        Used to decide whether deleting a specific Admin account would
        leave the system with no active Admin at all — see
        ``services.user_service.delete_user``.
        """
        return self._fetch_one(
            "SELECT COUNT(*) AS c FROM users WHERE role = 'Admin' AND status = 'Active'"
        )["c"]

    def list_by_team(self, team_id: int) -> list[dict[str, Any]]:
        """Return all users belonging to a team, oldest first."""
        return self._fetch_all_dicts(
            "SELECT * FROM users WHERE team_id = ? ORDER BY created_at ASC", (team_id,)
        )

    def list_all(self) -> list[dict[str, Any]]:
        """Return all users, oldest first."""
        return self._fetch_all_dicts("SELECT * FROM users ORDER BY created_at ASC")

    # Allowlist of columns ``list_page`` may sort by — interpolated
    # directly into SQL, so this must stay a fixed, code-controlled set
    # rather than accepting an arbitrary caller-supplied column name.
    _SORTABLE_COLUMNS = (
        "username", "role", "status", "team_id", "created_at", "last_login",
    )

    def list_page(
        self,
        *,
        username: str | None = None,
        email: str | None = None,
        team_id: int | None = None,
        role: str | None = None,
        status: str | None = None,
        sort_by: str = "created_at",
        sort_dir: str = "asc",
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one page of users, plus the total matching count.

        Filters and pagination are applied in SQL (WHERE + LIMIT/OFFSET),
        mirroring ``repositories.team_repository.TeamRepository.list_page``.

        Args:
            username: Case-insensitive substring match against
                ``username``, if given.
            email: Case-insensitive substring match against ``email``,
                if given. Independent of ``username`` — both apply
                together (AND) when both are given.
            team_id: Only include users belonging to this team, if given.
            role: Only include users with this exact role, if given.
            status: Only include users with this exact status
                ('Active'/'Inactive'), if given.
            sort_by: Column to sort by — must be one of
                ``_SORTABLE_COLUMNS``; silently falls back to
                ``created_at`` if given anything else (never raises on
                a bad/forgotten value from a query string).
            sort_dir: ``"asc"`` or ``"desc"``; anything else falls back
                to ``"asc"``.
            page: 1-based page number.
            per_page: Number of rows per page.

        Returns:
            ``(rows, total_matching_count)``.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if username:
            conditions.append("LOWER(username) LIKE ?")
            params.append(f"%{username.lower()}%")
        if email:
            conditions.append("LOWER(email) LIKE ?")
            params.append(f"%{email.lower()}%")
        if team_id is not None:
            conditions.append("team_id = ?")
            params.append(team_id)
        if role:
            conditions.append("role = ?")
            params.append(role)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        sort_column = sort_by if sort_by in self._SORTABLE_COLUMNS else "created_at"
        sort_direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"

        total = self._fetch_one(
            f"SELECT COUNT(*) AS c FROM users {where_clause}", tuple(params)
        )["c"]

        offset = max(page - 1, 0) * per_page
        rows = self._fetch_all_dicts(
            f"""
            SELECT * FROM users {where_clause}
            ORDER BY {sort_column} {sort_direction}
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        )
        return rows, total

    def username_exists(self, username: str, *, exclude_id: int | None = None) -> bool:
        """Return whether a user with this username already exists (case-insensitive).

        Args:
            username: Username to check.
            exclude_id: If given, ignore the row with this id — for
                validating an edit against every *other* user's username.
        """
        if exclude_id is None:
            return self._fetch_one(
                "SELECT 1 FROM users WHERE LOWER(username) = ?", (username.lower(),)
            ) is not None
        return self._fetch_one(
            "SELECT 1 FROM users WHERE LOWER(username) = ? AND id != ?",
            (username.lower(), exclude_id),
        ) is not None

    def email_exists(self, email: str, *, exclude_id: int | None = None) -> bool:
        """Return whether a user with this email already exists (case-insensitive).

        A blank/``None`` email always returns False without querying —
        this table's unique index on ``email`` already treats NULL as
        distinct (any number of accounts with no email coexist), so an
        absent email is never a real duplicate.

        Args:
            email: Email to check.
            exclude_id: If given, ignore the row with this id — for
                validating an edit against every *other* user's email.
        """
        if not email:
            return False
        if exclude_id is None:
            return self._fetch_one(
                "SELECT 1 FROM users WHERE LOWER(email) = ?", (email.lower(),)
            ) is not None
        return self._fetch_one(
            "SELECT 1 FROM users WHERE LOWER(email) = ? AND id != ?",
            (email.lower(), exclude_id),
        ) is not None
