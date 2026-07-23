"""Per-team Knowledge Base / embeddings storage paths for MHES (Phase 4,
extended in Phase 5 to also surface each team's slug for explicit team
context).

Each team gets an isolated folder tree under ``storage/teams/<team_slug>/``:

    knowledge/    -- that team's KB .xlsx files (was the shared kb_knowledge/)
    embeddings/   -- that team's FAISS indices + mapping/metadata JSON (was the shared embeddings/)

Nothing here parses Excel or touches embeddings content — it only computes
paths. ``services.excel_service.ExcelService`` and
``services.embedding_service.EmbeddingService`` are unchanged; they
already accept an arbitrary folder path, so passing a team-scoped path
instead of the old global one is the entire integration point.
"""

import os


def team_root_folder(teams_folder: str, team_slug: str) -> str:
    """Return the root storage folder for one team."""
    return os.path.join(teams_folder, team_slug)


def team_kb_folder(teams_folder: str, team_slug: str) -> str:
    """Return the Knowledge Base folder for one team."""
    return os.path.join(team_root_folder(teams_folder, team_slug), "knowledge")


def team_embeddings_folder(teams_folder: str, team_slug: str) -> str:
    """Return the embeddings folder for one team."""
    return os.path.join(team_root_folder(teams_folder, team_slug), "embeddings")


def team_folders_for_team_id(
    teams_folder: str, mhes_db_path: str, team_id: int,
) -> tuple[str, str, str]:
    """Resolve ``(kb_folder, embeddings_folder, team_slug)`` for a team id.

    Looks the team up via ``TeamRepository`` to get its slug — folder
    names use the slug, not the raw display name, so spaces/special
    characters in a team's ``name`` never leak into a filesystem path.
    ``team_slug`` is returned alongside the two folder paths (Phase 5 of
    multi-team support) so callers can pass explicit team context into
    ``EmbeddingService``/``SearchService`` instead of only encoding it
    implicitly in a folder path.

    Args:
        teams_folder: ``config["TEAMS_FOLDER"]`` — the ``storage/teams``
            root all team folders live under.
        mhes_db_path: Path to the shared MHES SQLite database.
        team_id: The team id to resolve (normally ``session["team_id"]``).

    Returns:
        ``(kb_folder, embeddings_folder, team_slug)`` for this team.

    Raises:
        ValueError: If no team exists with this id. Should not happen for
            a logged-in session, since ``team_id`` always comes from a
            ``users`` row created against an existing team.
    """
    from repositories.team_repository import TeamRepository

    team = TeamRepository(mhes_db_path).get_by_id(team_id)
    if team is None:
        raise ValueError(f"No team found with id={team_id}")
    slug = team["slug"]
    return team_kb_folder(teams_folder, slug), team_embeddings_folder(teams_folder, slug), slug
