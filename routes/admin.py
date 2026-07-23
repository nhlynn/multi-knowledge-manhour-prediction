"""Admin route blueprint for MHES (Phase 3).

Thin, read-only views over data that already exists via
``repositories/team_repository.py`` and ``repositories/user_repository.py``
— no new business logic is introduced here, just Admin-gated visibility
into it (see ``utils/permissions.py``). Full create/edit/delete
management (the actual "manage users" / "manage teams" mutations) is left
for a later phase; this phase adds the authorization layer plus minimal
views to exercise it end-to-end.
"""

from flask import Blueprint, current_app, render_template

from repositories.team_repository import TeamRepository
from repositories.user_repository import UserRepository
from utils.permissions import require_roles

admin_bp = Blueprint("admin", __name__)
# Manage Users / Manage Teams is an Admin-only capability.
admin_bp.before_request(require_roles("Admin"))


@admin_bp.route("/users", methods=["GET"])
def list_users() -> str:
    """List all users across all teams."""
    db_path = current_app.config["MHES_DB_PATH"]
    users = UserRepository(db_path).list_all()
    teams_by_id = {t["id"]: t for t in TeamRepository(db_path).list_all()}
    for user in users:
        team = teams_by_id.get(user["team_id"])
        user["team_name"] = team["name"] if team else "Unknown"
    return render_template("admin_users.html", users=users)


@admin_bp.route("/teams", methods=["GET"])
def list_teams() -> str:
    """List all teams."""
    db_path = current_app.config["MHES_DB_PATH"]
    teams = TeamRepository(db_path).list_all()
    return render_template("admin_teams.html", teams=teams)
