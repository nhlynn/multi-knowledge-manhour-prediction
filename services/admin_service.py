"""Read-only data composition for the Admin Manage Users / Manage Teams screens.

Moved out of ``routes/admin.py`` so the route is a pure call+render — the
only "logic" here is joining each user record with its team's display
name, since ``users`` only stores ``team_id``.
"""

from typing import Any

from repositories.team_repository import TeamRepository
from repositories.user_repository import UserRepository


def list_users_with_team_names(db_path: str) -> list[dict[str, Any]]:
    """Return every user, each annotated with its team's display name."""
    users = UserRepository(db_path).list_all()
    teams_by_id = {t["id"]: t for t in TeamRepository(db_path).list_all()}
    for user in users:
        team = teams_by_id.get(user["team_id"])
        user["team_name"] = team["name"] if team else "Unknown"
    return users


def list_users_page(
    db_path: str,
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
    """Return one page of users for the User Management list page, each
    annotated with its team's display name.

    Thin pass-through to ``UserRepository.list_page`` plus the same
    team-name join ``list_users_with_team_names`` does — kept here
    (rather than called directly from the route) so the route stays a
    pure call+render, consistent with every other view in this module.
    """
    users, total = UserRepository(db_path).list_page(
        username=username, email=email, team_id=team_id, role=role, status=status,
        sort_by=sort_by, sort_dir=sort_dir, page=page, per_page=per_page,
    )
    teams_by_id = {t["id"]: t for t in TeamRepository(db_path).list_all()}
    for user in users:
        team = teams_by_id.get(user["team_id"])
        user["team_name"] = team["name"] if team else "Unknown"
    return users, total


def get_user(db_path: str, user_id: int) -> dict[str, Any] | None:
    """Return a single user by id, or None if not found."""
    return UserRepository(db_path).get_by_id(user_id)


def list_teams(db_path: str) -> list[dict[str, Any]]:
    """Return every team."""
    return TeamRepository(db_path).list_all()


def get_team(db_path: str, team_id: int) -> dict[str, Any] | None:
    """Return a single team by id, or None if not found."""
    return TeamRepository(db_path).get_by_id(team_id)


def list_teams_page(
    db_path: str,
    *,
    name: str | None = None,
    code: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """Return one page of teams for the Team Management list page.

    Thin pass-through to ``TeamRepository.list_page`` — kept here (rather
    than called directly from the route) so the route stays a pure
    call+render, consistent with every other view in this module.
    """
    return TeamRepository(db_path).list_page(
        name=name, code=code, status=status, page=page, per_page=per_page,
    )
