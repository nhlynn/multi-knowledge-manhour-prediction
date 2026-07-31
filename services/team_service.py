"""Business rules for team data: validation, create/update/delete.

Backs the Team Management CRUD in ``routes/admin.py`` (Create Team,
Edit Team, Delete Team) — routes stay a thin call+render/redirect
layer; validation, uniqueness, deletion-safety, and persistence all
live here.

Kept separate from ``services/admin_service.py`` since that module is
purely read-only data composition (joining users with team names) —
this one owns the mutation/validation rules, a distinct responsibility.
"""

import os
import re
import sqlite3
from datetime import datetime
from typing import Any

from repositories.team_repository import TeamRepository
from repositories.user_repository import UserRepository
from utils.team_storage import team_embeddings_folder, team_kb_folder, team_root_folder

_NAME_MAX_LENGTH = 100
_SLUG_MAX_LENGTH = 60
_DESCRIPTION_MAX_LENGTH = 500

# Slugs are used directly as filesystem folder names (see
# ``utils/team_storage.py`` — ``storage/teams/<slug>/...``), so this
# pattern isn't just cosmetic: it excludes path separators, ``..``,
# spaces, and anything else that isn't a safe folder-name character.
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

VALID_STATUSES = ("Active", "Inactive")


class TeamValidationError(ValueError):
    """Raised when team input fails validation.

    ``errors`` maps field name -> human-readable message, so a caller
    (a future form-handling route) can surface every problem at once
    rather than stopping at the first one found.
    """

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{field}: {msg}" for field, msg in errors.items()))


def validate_team_name(name: str) -> str | None:
    """Return an error message for ``name``, or None if valid.

    Uniqueness is checked separately (it needs a database lookup) —
    see ``validate_team_input``.
    """
    stripped = (name or "").strip()
    if not stripped:
        return "Team name is required."
    if len(stripped) > _NAME_MAX_LENGTH:
        return f"Team name must be {_NAME_MAX_LENGTH} characters or fewer."
    return None


def validate_team_slug(slug: str) -> str | None:
    """Return an error message for ``slug`` (the "Team Code"), or None if valid.

    Uniqueness is checked separately (it needs a database lookup) —
    see ``validate_team_input``.
    """
    stripped = (slug or "").strip()
    if not stripped:
        return "Team code is required."
    if len(stripped) > _SLUG_MAX_LENGTH:
        return f"Team code must be {_SLUG_MAX_LENGTH} characters or fewer."
    if not _SLUG_PATTERN.match(stripped):
        return (
            "Team code may only contain lowercase letters, numbers, and "
            "single hyphens between words (e.g. 'infrastructure-team')."
        )
    return None


def validate_description(description: str | None) -> str | None:
    """Return an error message for ``description``, or None if valid.

    Optional field — ``None``/empty is always valid.
    """
    if description is None:
        return None
    if len(description) > _DESCRIPTION_MAX_LENGTH:
        return f"Description must be {_DESCRIPTION_MAX_LENGTH} characters or fewer."
    return None


def validate_status(status: str) -> str | None:
    """Return an error message for ``status``, or None if valid.

    Mirrors the ``CHECK(status IN ('Active', 'Inactive'))`` constraint
    on the ``teams`` table — rejecting an invalid value here gives a
    readable error instead of letting it fail as a raw
    ``sqlite3.IntegrityError`` at the database layer.
    """
    if status not in VALID_STATUSES:
        return f"Status must be one of: {', '.join(VALID_STATUSES)}."
    return None


def validate_team_input(
    db_path: str,
    *,
    name: str,
    slug: str,
    description: str | None = None,
    status: str = "Active",
    exclude_id: int | None = None,
) -> None:
    """Validate a full set of team input, raising ``TeamValidationError``
    listing every failing field if any check fails.

    Args:
        db_path: Path to the shared MHES SQLite database (needed for
            the name/slug uniqueness lookups).
        name: Team display name.
        slug: Team code — the unique, URL/folder-safe identifier.
        description: Optional free-text description.
        status: 'Active' or 'Inactive'.
        exclude_id: When validating an edit to an existing team, its
            own id — so it isn't flagged as a duplicate of itself.

    Raises:
        TeamValidationError: If any field is invalid, with every
            failing field's message in ``.errors``.
    """
    errors: dict[str, str] = {}

    name_error = validate_team_name(name)
    if name_error:
        errors["name"] = name_error

    slug_error = validate_team_slug(slug)
    if slug_error:
        errors["slug"] = slug_error

    description_error = validate_description(description)
    if description_error:
        errors["description"] = description_error

    status_error = validate_status(status)
    if status_error:
        errors["status"] = status_error

    repo = TeamRepository(db_path)
    if name_error is None and repo.name_exists(name.strip(), exclude_id=exclude_id):
        errors["name"] = "A team with this name already exists."
    if slug_error is None and repo.slug_exists(slug.strip(), exclude_id=exclude_id):
        errors["slug"] = "A team with this code already exists."

    if errors:
        raise TeamValidationError(errors)


_SLUGIFY_INVALID_CHARS = re.compile(r"[^a-z0-9]+")
_SLUGIFY_EDGE_HYPHENS = re.compile(r"^-+|-+$")


def slugify_team_name(name: str) -> str:
    """Derive a candidate Team Code (slug) from a Team Name.

    Lowercases, replaces runs of anything that isn't a-z/0-9 with a
    single hyphen, trims leading/trailing hyphens, and truncates to
    the same max length ``validate_team_slug`` enforces. Purely
    mechanical — this only produces a *starting point*; the Create
    Team form pre-fills its Team Code field with this value but always
    leaves it editable, and whatever is actually submitted is what
    gets validated/saved (this function's output is never trusted as
    already-unique or already-valid on its own).

    Args:
        name: The Team Name typed so far (may be partial/empty).

    Returns:
        A slug candidate. Empty string if ``name`` has no
        slug-able characters (e.g. it's blank or all punctuation).
    """
    lowered = (name or "").strip().lower()
    slug = _SLUGIFY_INVALID_CHARS.sub("-", lowered)
    slug = _SLUGIFY_EDGE_HYPHENS.sub("", slug)
    return slug[:_SLUG_MAX_LENGTH].rstrip("-")


def _raise_for_integrity_error(exc: sqlite3.IntegrityError) -> None:
    """Translate a UNIQUE-constraint violation from the database into a
    field-level ``TeamValidationError``.

    ``validate_team_input``/``update_team``'s own name/slug uniqueness
    checks are check-then-insert, not atomic — two concurrent
    create/edit requests for the same name or slug can both pass that
    check and race to the actual write. The ``teams.slug`` UNIQUE
    constraint and the ``idx_teams_name_unique`` index (see
    ``repositories.team_repository.TeamRepository._ensure_name_unique_index``)
    are what actually prevent the duplicate in that case — this turns
    the resulting raw ``sqlite3.IntegrityError`` into the same kind of
    error the pre-check would have raised, instead of a raw 500.
    """
    message = str(exc)
    if "slug" in message:
        raise TeamValidationError({"slug": "A team with this code already exists."}) from exc
    if "name" in message:
        raise TeamValidationError({"name": "A team with this name already exists."}) from exc
    raise TeamValidationError({"name": "A team with these details already exists."}) from exc


def create_team(
    db_path: str,
    *,
    name: str,
    slug: str,
    description: str | None = None,
    status: str = "Active",
) -> dict[str, Any]:
    """Validate and insert a new team.

    The actual "create" business logic (validation, then persisting)
    lives here rather than in the route, so ``routes/admin.py`` stays a
    thin call+render/redirect layer.

    Args:
        db_path: Path to the shared MHES SQLite database.
        name: Team display name.
        slug: Team code — normally pre-filled from ``slugify_team_name``
            by the form's JS, but submitted (and validated) as
            free-editable input, not regenerated here.
        description: Optional free-text description.
        status: 'Active' or 'Inactive'.

    Returns:
        The newly created team record.

    Raises:
        TeamValidationError: If any field fails validation (including
            name/slug uniqueness) — nothing is written in that case.
    """
    clean_name = (name or "").strip()
    clean_slug = (slug or "").strip()
    clean_description = (description or "").strip() or None

    validate_team_input(
        db_path, name=clean_name, slug=clean_slug, description=clean_description, status=status,
    )

    try:
        return TeamRepository(db_path).insert(
            name=clean_name,
            slug=clean_slug,
            created_at=datetime.now().isoformat(),
            description=clean_description,
            status=status,
        )
    except sqlite3.IntegrityError as exc:
        _raise_for_integrity_error(exc)


def is_team_code_locked(db_path: str, team_id: int, *, teams_folder: str | None = None) -> bool:
    """Return whether this team's Team Code (slug) is already relied on
    elsewhere in the system, and so must not be changed.

    A slug is considered "in use" if either:

    - Any user account belongs to this team (``users.team_id``) — every
      request in that user's session resolves storage paths from the
      team's *current* slug (see ``utils/team_storage.py``), so
      renaming it would silently orphan that user's KB/embeddings.
    - Its storage folder (``storage/teams/<slug>/``) already exists on
      disk — renaming would leave existing KB files/embeddings behind
      under the old, now-unreferenced folder name.

    A brand new team with no users yet and no storage folder created
    yet has nothing depending on its slug, so it's still safe to
    correct/change at that point.

    Args:
        db_path: Path to the shared MHES SQLite database.
        team_id: The team to check.
        teams_folder: ``config["TEAMS_FOLDER"]``. If omitted, the
            filesystem check is skipped (only the users check runs) —
            callers without a teams-folder path (e.g. a non-request
            context) still get a safe, if partial, answer.

    Returns:
        True if the Team Code must be treated as read-only.
    """
    team = TeamRepository(db_path).get_by_id(team_id)
    if team is None:
        return False

    if UserRepository(db_path).list_by_team(team_id):
        return True

    if teams_folder and os.path.isdir(team_root_folder(teams_folder, team["slug"])):
        return True

    return False


def update_team(
    db_path: str,
    team_id: int,
    *,
    name: str,
    slug: str | None = None,
    description: str | None = None,
    status: str = "Active",
    teams_folder: str | None = None,
) -> dict[str, Any]:
    """Validate and update an existing team's Name/Description/Status.

    Team Code (slug) is only ever changed if ``is_team_code_locked``
    says it's still safe to — see that function for what "in use"
    means. A caller's submitted ``slug`` is silently ignored (the
    team's existing slug is kept) once it's locked, rather than
    erroring, since a locked field is normally rendered read-only in
    the form anyway and shouldn't surface as a validation failure.

    Args:
        db_path: Path to the shared MHES SQLite database.
        team_id: The team being edited.
        name: New display name.
        slug: New team code, only honored while unlocked. Ignored
            (existing slug kept) once the team is locked.
        description: New free-text description.
        status: 'Active' or 'Inactive'.
        teams_folder: ``config["TEAMS_FOLDER"]`` — passed through to
            ``is_team_code_locked`` for the storage-folder check.

    Returns:
        The updated team record.

    Raises:
        ValueError: If no team exists with ``team_id``.
        TeamValidationError: If any field fails validation (including
            name/slug uniqueness) — nothing is written in that case.
    """
    existing = TeamRepository(db_path).get_by_id(team_id)
    if existing is None:
        raise ValueError(f"No team found with id={team_id}")

    locked = is_team_code_locked(db_path, team_id, teams_folder=teams_folder)
    effective_slug = existing["slug"] if locked else ((slug or "").strip() or existing["slug"])

    clean_name = (name or "").strip()
    clean_description = (description or "").strip() or None

    errors: dict[str, str] = {}

    name_error = validate_team_name(clean_name)
    if name_error:
        errors["name"] = name_error

    slug_error = None
    if not locked:
        slug_error = validate_team_slug(effective_slug)
        if slug_error:
            errors["slug"] = slug_error

    description_error = validate_description(clean_description)
    if description_error:
        errors["description"] = description_error

    status_error = validate_status(status)
    if status_error:
        errors["status"] = status_error

    repo = TeamRepository(db_path)
    if name_error is None and repo.name_exists(clean_name, exclude_id=team_id):
        errors["name"] = "A team with this name already exists."
    if not locked and slug_error is None and repo.slug_exists(effective_slug, exclude_id=team_id):
        errors["slug"] = "A team with this code already exists."

    if errors:
        raise TeamValidationError(errors)

    try:
        return repo.update(
            team_id, name=clean_name, slug=effective_slug, description=clean_description, status=status,
        )
    except sqlite3.IntegrityError as exc:
        _raise_for_integrity_error(exc)


class TeamDeletionBlockedError(ValueError):
    """Raised when a team can't be deleted because something still depends on it.

    ``reasons`` lists every dependency found (not just the first), so
    the admin sees the full picture in one error message rather than
    fixing one thing and hitting the next blocker on retry.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def _team_has_knowledge_base_data(teams_folder: str | None, slug: str) -> bool:
    """Return whether this team's KB/embeddings storage folder has anything in it.

    Checks the filesystem directly rather than going through
    ``services.excel_service.ExcelService`` — that class's constructor
    creates the KB folder as a side effect (``os.makedirs(..., exist_ok=True)``),
    which would be wrong to trigger just to *check* whether a team has
    data, especially right before potentially deleting that same team.
    """
    if not teams_folder:
        return False
    for folder in (team_kb_folder(teams_folder, slug), team_embeddings_folder(teams_folder, slug)):
        if os.path.isdir(folder) and any(os.scandir(folder)):
            return True
    return False


def get_team_deletion_blockers(
    db_path: str, team_id: int, *, teams_folder: str | None = None,
) -> list[str]:
    """Return every reason this team can't currently be deleted (empty if none).

    Checks, in order:

    - Users: any user account still assigned to this team.
    - Knowledge Base: any KB/embeddings files still stored for this team.
    - Export History: any export history record still recorded for this team.

    Temporary Data (``temp_stashes``) is deliberately NOT checked here:
    that table has no ``team_id`` (or any other team-linkage) column at
    all — ``created_by`` is free text typed into a form, not tied to a
    login or a team, so there's no reliable way to attribute a stash to
    a team. Matching on it would be a guess dressed up as a safety
    check, so it's left out rather than giving false confidence either
    way (see the decision recorded when this was implemented).

    Args:
        db_path: Path to the shared MHES SQLite database.
        team_id: The team being considered for deletion.
        teams_folder: ``config["TEAMS_FOLDER"]`` — needed for the
            Knowledge Base check. If omitted, that check is skipped.

    Returns:
        Human-readable blocker messages, one per dependency found.
        Empty list means the team is safe to delete.
    """
    from services.export_history_service import ExportHistoryService

    team = TeamRepository(db_path).get_by_id(team_id)
    if team is None:
        return []

    reasons: list[str] = []

    user_count = len(UserRepository(db_path).list_by_team(team_id))
    if user_count:
        reasons.append(
            f"{user_count} user account(s) are still assigned to this team."
        )

    if _team_has_knowledge_base_data(teams_folder, team["slug"]):
        reasons.append("This team still has Knowledge Base files/embeddings in storage.")

    if ExportHistoryService(db_path).has_records_for_team(team_id):
        reasons.append("This team still has Export History records.")

    return reasons


def delete_team(db_path: str, team_id: int, *, teams_folder: str | None = None) -> None:
    """Delete a team, refusing if anything still depends on it.

    Args:
        db_path: Path to the shared MHES SQLite database.
        team_id: The team to delete.
        teams_folder: ``config["TEAMS_FOLDER"]`` — passed through to
            ``get_team_deletion_blockers`` for the Knowledge Base check.

    Raises:
        ValueError: If no team exists with ``team_id``.
        TeamDeletionBlockedError: If Users, Knowledge Base, or Export
            History still reference this team — nothing is deleted in
            that case.
    """
    team = TeamRepository(db_path).get_by_id(team_id)
    if team is None:
        raise ValueError(f"No team found with id={team_id}")

    blockers = get_team_deletion_blockers(db_path, team_id, teams_folder=teams_folder)
    if blockers:
        raise TeamDeletionBlockedError(blockers)

    TeamRepository(db_path).delete(team_id)
