"""Business rules for user account data: validation, Create/Edit User,
and the Admin Reset Password action.

Backs the User Management screens in ``routes/admin.py`` — routes
stay thin call+render/redirect layers; validation, uniqueness,
password hashing, and persistence all live here, the same shape as
``services/team_service.py``.

Kept separate from ``services/admin_service.py`` (purely read-only data
composition) and ``services/auth_service.py`` (login/session/Forgot-Password
concerns) — this module owns user *management* mutation/validation
rules, a distinct responsibility from either (though it reuses
``AuthService.hash_password`` rather than duplicating hashing logic).

``admin_reset_password`` below is a completely separate code path from
``AuthService``'s Forgot Password / Reset Password self-service flow
(different repository method, different route, no shared state) —
this module never touches ``AuthService.request_password_reset``/
``reset_password``, so that flow is unaffected by anything here.
"""

import logging
import re
import sqlite3
from datetime import datetime
from typing import Any

from repositories.team_repository import TeamRepository
from repositories.user_repository import VALID_ROLES, UserRepository
from services.auth_service import AuthService
from utils.password_policy import validate_password_strength

logger = logging.getLogger(__name__)

_USERNAME_MIN_LENGTH = 3
_USERNAME_MAX_LENGTH = 50
_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.]+$")

_EMAIL_MAX_LENGTH = 254  # RFC 5321 practical limit
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

VALID_STATUSES = ("Active", "Inactive")


class UserValidationError(ValueError):
    """Raised when user input fails validation.

    ``errors`` maps field name -> human-readable message, so a caller
    (a future form-handling route) can surface every problem at once
    rather than stopping at the first one found — same shape as
    ``services.team_service.TeamValidationError``.
    """

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{field}: {msg}" for field, msg in errors.items()))


def validate_username(username: str) -> str | None:
    """Return an error message for ``username``, or None if valid.

    Uniqueness is checked separately (it needs a database lookup) —
    see ``validate_user_input``.
    """
    stripped = (username or "").strip()
    if not stripped:
        return "Username is required."
    if len(stripped) < _USERNAME_MIN_LENGTH:
        return f"Username must contain at least {_USERNAME_MIN_LENGTH} characters."
    if len(stripped) > _USERNAME_MAX_LENGTH:
        return f"Username must be {_USERNAME_MAX_LENGTH} characters or fewer."
    if not _USERNAME_PATTERN.match(stripped):
        return "Only letters, numbers, underscores and dots are allowed."
    return None


def validate_email(email: str | None) -> str | None:
    """Return an error message for ``email``, or None if valid.

    Optional field (the ``users.email`` column is nullable — an
    account can have no email on file) — ``None``/empty is always
    valid. Uniqueness is checked separately (it needs a database
    lookup) — see ``validate_user_input``.
    """
    if not email:
        return None
    stripped = email.strip()
    if len(stripped) > _EMAIL_MAX_LENGTH:
        return f"Email must be {_EMAIL_MAX_LENGTH} characters or fewer."
    if not _EMAIL_PATTERN.match(stripped):
        return "Invalid email format."
    return None


def validate_password(password: str) -> str | None:
    """Return an error message for ``password``, or None if valid.

    Delegates to ``utils.password_policy.validate_password_strength``
    — the same rule set already used by Reset Password, so a new
    account's password is never held to a different (weaker or
    stronger) standard than a reset one.
    """
    return validate_password_strength(password)


def validate_team(db_path: str, team_id: int) -> str | None:
    """Return an error message for ``team_id``, or None if it refers to
    a real, existing team.

    ``users.team_id`` is ``NOT NULL`` in the schema — every account
    must belong to a team — so unlike Email/Description-style optional
    fields, a missing or dangling team reference is always invalid.
    """
    if team_id is None:
        return "Team is required."
    if TeamRepository(db_path).get_by_id(team_id) is None:
        return "Selected team does not exist."
    return None


def validate_role(role: str) -> str | None:
    """Return an error message for ``role``, or None if valid.

    Mirrors the ``CHECK(role IN ('Admin', 'Team Manager', 'Member'))``
    constraint on the ``users`` table — rejecting an invalid value
    here gives a readable error instead of letting it fail as a raw
    ``sqlite3.IntegrityError`` at the database layer.
    """
    if role not in VALID_ROLES:
        return f"Role must be one of: {', '.join(VALID_ROLES)}."
    return None


def validate_status(status: str) -> str | None:
    """Return an error message for ``status``, or None if valid.

    Mirrors the ``CHECK(status IN ('Active', 'Inactive'))`` constraint
    on the ``users`` table, same reasoning as ``validate_role``.
    """
    if status not in VALID_STATUSES:
        return f"Status must be one of: {', '.join(VALID_STATUSES)}."
    return None


def validate_user_input(
    db_path: str,
    *,
    username: str,
    email: str | None = None,
    password: str | None = None,
    team_id: int,
    role: str,
    status: str = "Active",
    exclude_id: int | None = None,
    require_password: bool = True,
) -> None:
    """Validate a full set of user input, raising ``UserValidationError``
    listing every failing field if any check fails.

    Args:
        db_path: Path to the shared MHES SQLite database (needed for
            the username/email uniqueness lookups and the team
            existence check).
        username: Login name.
        email: Optional email address.
        password: Plaintext password to validate, or None if not being
            set/changed (see ``require_password``).
        team_id: Id of the team this account belongs to.
        role: One of ``repositories.user_repository.VALID_ROLES``.
        status: 'Active' or 'Inactive'.
        exclude_id: When validating an edit to an existing account,
            its own id — so it isn't flagged as a duplicate of itself.
        require_password: If False, a ``None``/absent ``password`` is
            not an error (e.g. editing an account without changing its
            password) — but if a password IS given, it's still
            strength-checked either way.

    Raises:
        UserValidationError: If any field is invalid, with every
            failing field's message in ``.errors``.
    """
    errors: dict[str, str] = {}

    username_error = validate_username(username)
    if username_error:
        errors["username"] = username_error

    email_error = validate_email(email)
    if email_error:
        errors["email"] = email_error

    if password:
        password_error = validate_password(password)
        if password_error:
            errors["password"] = password_error
    elif require_password:
        errors["password"] = "Password is required."

    team_error = validate_team(db_path, team_id)
    if team_error:
        errors["team_id"] = team_error

    role_error = validate_role(role)
    if role_error:
        errors["role"] = role_error

    status_error = validate_status(status)
    if status_error:
        errors["status"] = status_error

    repo = UserRepository(db_path)
    if username_error is None and repo.username_exists((username or "").strip(), exclude_id=exclude_id):
        errors["username"] = "Username already exists."
    if email_error is None and email and repo.email_exists(email.strip(), exclude_id=exclude_id):
        errors["email"] = "Email already exists."

    if errors:
        raise UserValidationError(errors)


def _raise_for_integrity_error(exc: sqlite3.IntegrityError) -> None:
    """Translate a UNIQUE-constraint violation from the database into a
    field-level ``UserValidationError``.

    ``validate_user_input``'s own username/email uniqueness checks are
    check-then-insert, not atomic — two concurrent create requests for
    the same username or email can both pass that check and race to
    the actual write. The ``username``/``email`` unique indexes are
    what actually prevent the duplicate in that case — this turns the
    resulting raw ``sqlite3.IntegrityError`` into the same kind of
    error the pre-check would have raised, instead of a raw 500. Same
    approach as ``services.team_service._raise_for_integrity_error``.
    """
    message = str(exc)
    if "email" in message:
        raise UserValidationError({"email": "Email already exists."}) from exc
    if "username" in message:
        raise UserValidationError({"username": "Username already exists."}) from exc
    raise UserValidationError({"username": "A user with these details already exists."}) from exc


def create_user(
    db_path: str,
    *,
    username: str,
    email: str | None = None,
    password: str,
    confirm_password: str,
    team_id: int,
    role: str,
    status: str = "Active",
    performed_by_user_id: int | None = None,
    performed_by_username: str | None = None,
) -> dict[str, Any]:
    """Validate and insert a new user account.

    The actual "create" business logic (validation, password
    confirmation, hashing, then persisting) lives here rather than in
    the route, so ``routes/admin.py`` stays a thin call+render/redirect
    layer — same shape as ``services.team_service.create_team``.

    Args:
        db_path: Path to the shared MHES SQLite database.
        username: Login name.
        email: Optional email address.
        password: Plaintext password (hashed before storage — never
            stored or logged as given).
        confirm_password: Must match ``password`` exactly, or this is
            rejected before any other validation runs its DB lookups.
        team_id: Id of the team this account belongs to.
        role: One of ``repositories.user_repository.VALID_ROLES``.
        status: 'Active' or 'Inactive'.
        performed_by_user_id: The acting Admin's id, for the audit log
            entry only.
        performed_by_username: The acting Admin's username, for the
            audit log entry only.

    Returns:
        The newly created user record.

    Raises:
        UserValidationError: If any field is invalid (including a
            password/confirm-password mismatch, or username/email
            uniqueness) — nothing is written in that case.
    """
    clean_username = (username or "").strip()
    clean_email = (email or "").strip() or None

    errors: dict[str, str] = {}
    if password != confirm_password:
        errors["confirm_password"] = "Passwords do not match."

    try:
        validate_user_input(
            db_path,
            username=clean_username,
            email=clean_email,
            password=password,
            team_id=team_id,
            role=role,
            status=status,
            require_password=True,
        )
    except UserValidationError as e:
        errors.update(e.errors)

    if errors:
        raise UserValidationError(errors)

    now = datetime.now().isoformat()
    try:
        created = UserRepository(db_path).insert(
            username=clean_username,
            password_hash=AuthService.hash_password(password),
            team_id=team_id,
            role=role,
            created_at=now,
            email=clean_email,
            status=status,
            password_changed_at=now,
        )
    except sqlite3.IntegrityError as exc:
        _raise_for_integrity_error(exc)

    logger.info(
        "User created: user_id=%s username=%r by admin_id=%s (username=%r).",
        created["id"], created["username"], performed_by_user_id, performed_by_username,
    )
    return created


def update_user(
    db_path: str,
    user_id: int,
    *,
    username: str,
    email: str | None = None,
    team_id: int,
    role: str,
    status: str = "Active",
    performed_by_user_id: int | None = None,
    performed_by_username: str | None = None,
) -> dict[str, Any]:
    """Validate and update an existing user's Username/Email/Team/Role/Status.

    Deliberately has no password parameter at all — Edit User never
    displays or changes a password from this screen (that's Reset
    Password's job); see ``repositories.user_repository.UserRepository.update``,
    which likewise has no ``password_hash`` argument.

    Logs a generic "User updated" line, plus a separate, dedicated
    "User status changed" line whenever ``status`` actually differs
    from the account's current value — status flips (Active/Inactive)
    are security-relevant enough to be independently greppable rather
    than buried in a general edit entry.

    Also enforces the Admin account management rules:

    - An Admin editing their OWN account can never change their own
      role or deactivate their own account — an absolute rule,
      regardless of how many other active Admins exist.
    - A DIFFERENT admin editing someone else's account is refused if
      changing that account's role away from Admin, or its status
      away from Active, would leave the system with zero active
      Admins (mirrors ``get_user_deletion_blockers``'s last-active-Admin
      rule for Delete User — without this, Edit User could achieve the
      exact lockout Delete User is guarded against, just via a
      different form).

    Args:
        db_path: Path to the shared MHES SQLite database.
        user_id: The account being edited.
        username: New username.
        email: New email address (optional).
        team_id: New team assignment.
        role: New role.
        status: 'Active' or 'Inactive'.
        performed_by_user_id: The acting Admin's id, for the audit log
            entries only.
        performed_by_username: The acting Admin's username, for the
            audit log entries only.

    Returns:
        The updated user record.

    Raises:
        ValueError: If no user exists with ``user_id``.
        UserValidationError: If any field fails validation (including
            username/email uniqueness, or a last-active-Admin
            demotion/deactivation) — nothing is written in that case.
    """
    repo = UserRepository(db_path)
    existing = repo.get_by_id(user_id)
    if existing is None:
        raise ValueError(f"No user found with id={user_id}")

    clean_username = (username or "").strip()
    clean_email = (email or "").strip() or None

    errors: dict[str, str] = {}
    try:
        validate_user_input(
            db_path,
            username=clean_username,
            email=clean_email,
            password=None,
            team_id=team_id,
            role=role,
            status=status,
            exclude_id=user_id,
            require_password=False,
        )
    except UserValidationError as e:
        errors.update(e.errors)

    is_self_edit = performed_by_user_id is not None and user_id == performed_by_user_id

    if is_self_edit:
        # Absolute rules: an Admin can never change their own role or
        # deactivate their own account, regardless of how many other
        # active Admins exist — distinct from (and checked before) the
        # last-active-Admin rule below, which is about a *different*
        # admin editing someone else's account.
        if role != existing["role"]:
            errors["role"] = "You cannot change your own role."
        if status != existing["status"]:
            errors["status"] = "You cannot deactivate your own account."
    else:
        was_active_admin = existing["role"] == "Admin" and existing["status"] == "Active"
        stays_active_admin = role == "Admin" and status == "Active"
        if was_active_admin and not stays_active_admin and repo.count_active_admins() <= 1:
            if role != "Admin":
                errors["role"] = "Cannot change the role of the last active Admin account."
            if status != "Active":
                errors["status"] = "Cannot deactivate the last active Admin account."

    if errors:
        raise UserValidationError(errors)

    try:
        updated = repo.update(
            user_id,
            username=clean_username,
            email=clean_email,
            team_id=team_id,
            role=role,
            status=status,
            updated_at=datetime.now().isoformat(),
        )
    except sqlite3.IntegrityError as exc:
        _raise_for_integrity_error(exc)

    logger.info(
        "User updated: user_id=%s username=%r by admin_id=%s (username=%r).",
        user_id, updated["username"], performed_by_user_id, performed_by_username,
    )
    if existing["status"] != status:
        logger.info(
            "User status changed: user_id=%s username=%r status %r -> %r by admin_id=%s (username=%r).",
            user_id, updated["username"], existing["status"], status,
            performed_by_user_id, performed_by_username,
        )
    return updated


def admin_reset_password(
    db_path: str,
    user_id: int,
    *,
    new_password: str,
    confirm_password: str,
    performed_by_user_id: int | None = None,
    performed_by_username: str | None = None,
) -> dict[str, Any]:
    """Admin-driven password reset: directly set a new password for an
    account, without the Forgot Password email/token flow.

    This is a distinct code path from ``AuthService.request_password_reset``/
    ``reset_password`` (self-service Forgot Password) — it writes via
    ``UserRepository.set_password``, a separate method from the
    ``update_password`` that flow uses, so nothing here changes that
    flow's behavior.

    Args:
        db_path: Path to the shared MHES SQLite database.
        user_id: The account whose password is being reset.
        new_password: The new plaintext password (hashed before
            storage — never stored or logged as given).
        confirm_password: Must match ``new_password`` exactly.
        performed_by_user_id: The acting Admin's id, recorded in the
            audit log entry only.
        performed_by_username: The acting Admin's username, recorded
            in the audit log entry only.

    Returns:
        The affected user's record.

    Raises:
        ValueError: If no user exists with ``user_id``.
        UserValidationError: If the passwords don't match or the new
            password fails the strength policy — nothing is changed
            in that case.
    """
    repo = UserRepository(db_path)
    user = repo.get_by_id(user_id)
    if user is None:
        raise ValueError(f"No user found with id={user_id}")

    errors: dict[str, str] = {}
    if new_password != confirm_password:
        errors["confirm_password"] = "Passwords do not match."
    password_error = validate_password(new_password)
    if password_error:
        errors["new_password"] = password_error
    if errors:
        raise UserValidationError(errors)

    now = datetime.now().isoformat()
    repo.set_password(user_id, password_hash=AuthService.hash_password(new_password), changed_at=now)

    logger.info(
        "User password reset: user_id=%s username=%r by admin_id=%s (username=%r).",
        user_id, user["username"], performed_by_user_id, performed_by_username,
    )
    return repo.get_by_id(user_id)


class UserDeletionBlockedError(ValueError):
    """Raised when a user account can't be deleted because a safety
    rule forbids it.

    ``reasons`` lists every rule violated (not just the first), so the
    admin sees the full picture in one error message — same shape as
    ``services.team_service.TeamDeletionBlockedError``.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def get_user_deletion_blockers(db_path: str, user_id: int, *, current_user_id: int | None) -> list[str]:
    """Return every reason this user account can't currently be deleted
    (empty if none).

    Checks:

    - Self-deletion: an Admin can't delete their own currently
      logged-in account (a route-level "delete myself" click would
      otherwise immediately invalidate the session it came from, and
      is almost never intentional).
    - Last active Admin: deleting this account must not leave the
      system with zero accounts that are both ``role='Admin'`` and
      ``status='Active'`` — otherwise no one could administer it
      going forward. Only counts *active* Admins, so an Inactive
      Admin account doesn't itself block deleting the one truly
      active Admin, nor does deleting an already-Inactive Admin ever
      trip this rule (removing it doesn't reduce the active count).

    Args:
        db_path: Path to the shared MHES SQLite database.
        user_id: The account being considered for deletion.
        current_user_id: The id of the admin performing the deletion
            (from the session), to check against self-deletion.

    Returns:
        Human-readable blocker messages, one per rule violated. Empty
        list means the account is safe to delete.
    """
    repo = UserRepository(db_path)
    user = repo.get_by_id(user_id)
    if user is None:
        return []

    reasons: list[str] = []

    if current_user_id is not None and user_id == current_user_id:
        reasons.append("You cannot delete your own account.")

    if user["role"] == "Admin" and user["status"] == "Active" and repo.count_active_admins() <= 1:
        reasons.append("Cannot delete the last active Admin account.")

    return reasons


def delete_user(
    db_path: str,
    user_id: int,
    *,
    current_user_id: int | None = None,
    current_username: str | None = None,
) -> None:
    """Delete a user account, refusing if a safety rule forbids it.

    Args:
        db_path: Path to the shared MHES SQLite database.
        user_id: The account to delete.
        current_user_id: The id of the admin performing the deletion
            (from the session) — checked against self-deletion, and
            included in the audit log entry.
        current_username: The acting admin's username, for the audit
            log entry only.

    Raises:
        ValueError: If no user exists with ``user_id``.
        UserDeletionBlockedError: If deleting this account would be
            self-deletion or would remove the last active Admin —
            nothing is deleted in that case.
    """
    repo = UserRepository(db_path)
    user = repo.get_by_id(user_id)
    if user is None:
        raise ValueError(f"No user found with id={user_id}")

    blockers = get_user_deletion_blockers(db_path, user_id, current_user_id=current_user_id)
    if blockers:
        raise UserDeletionBlockedError(blockers)

    repo.delete(user_id)
    logger.info(
        "User deleted: user_id=%s username=%r by admin_id=%s (username=%r).",
        user_id, user["username"], current_user_id, current_username,
    )
