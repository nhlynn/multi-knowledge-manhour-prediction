"""Chatbot route blueprint for MHES.

Handles AI-powered semantic search interactions.
"""

from flask import Blueprint, current_app, jsonify, render_template, request, session

from services.embedding_service import EmbeddingService
from services.search_service import SearchService
from utils.permissions import require_roles
from utils.team_storage import team_folders_for_team_id

chatbot_bp = Blueprint("chatbot", __name__)
# Team Manager only -- Admin manages teams/config rather than doing
# estimation work; see app.py's own "/" route (the chatbot PAGE) for
# the matching gate, and its comment for why redirect_endpoint points
# at /dashboard rather than the default "index".
chatbot_bp.before_request(require_roles("Team Manager", redirect_endpoint="dashboard"))


@chatbot_bp.route("/", methods=["GET"])
def chatbot_page() -> str:
    """Render the chatbot page."""
    return render_template("chatbot.html")


@chatbot_bp.route("/search", methods=["POST"])
def search():
    """Perform semantic search on the knowledge base."""
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()

    if not query:
        return jsonify({"error": "Please enter a search query."}), 400

    _, embeddings_folder, team_slug = team_folders_for_team_id(
        current_app.config["TEAMS_FOLDER"],
        current_app.config["MHES_DB_PATH"],
        session["team_id"],
    )
    emb_svc = EmbeddingService(
        model_name=current_app.config["EMBEDDING_MODEL"],
        embeddings_folder=embeddings_folder,
        team_slug=team_slug,
    )
    search_svc = SearchService(embedding_service=emb_svc)

    top_k = data.get("top_k", 10)
    result = search_svc.semantic_search(query, top_k=top_k)

    return jsonify({"query": query, **result})