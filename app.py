"""MHES - Man Hour Estimation System.

Flask application entry point.
"""

import os
import logging
from flask import Flask, render_template, session

from config import Config, config_by_name
from scheduler.scheduler import init_scheduler
from utils.logger import setup_logging
from utils.migration import (
    create_default_admin_user,
    create_default_team,
    merge_legacy_databases_into_mhes,
    migrate_kb_to_team_storage,
    migrate_stashes_json_to_sqlite,
    seed_development_team_export_template,
    seed_development_team_import_config,
)
from utils.permissions import login_required
from utils.team_storage import team_folders_for_team_id


def create_app(config_name: str = "development") -> Flask:
    """Create and configure the Flask application.

    Args:
        config_name: Configuration environment name.

    Returns:
        Configured Flask application instance.
    """
    app = Flask(__name__)
    app.config.from_object(config_by_name.get(config_name, Config))

    # Ensure required folders exist
    _ensure_folders(app)

    # Setup logging
    setup_logging(app.config.get("LOG_FOLDER", "logs"))

    # One-shot migrations into the single shared database/mhes.db.
    # All no-op on every startup after the first (see utils/migration.py).
    #
    # create_default_team runs first (moved ahead of the legacy-database
    # merge in Phase 6): merge_legacy_databases_into_mhes's export_history
    # merge now requires the default team to already exist, since every
    # export_history row must have a team_id.
    create_default_team(app.config["MHES_DB_PATH"])
    migrate_stashes_json_to_sqlite(app.config["TEMP_DATA_FOLDER"], app.config["MHES_DB_PATH"])
    merge_legacy_databases_into_mhes(
        legacy_temp_db_path=os.path.join(app.config["TEMP_DATA_FOLDER"], "temp_storage.db"),
        legacy_export_db_path=os.path.join(app.config["EXPORT_FOLDER"], "export_history.db"),
        mhes_db_path=app.config["MHES_DB_PATH"],
    )
    # Phase 4 of multi-team support: migrate the old shared kb_knowledge/
    # and embeddings/ folders into the default team's isolated storage
    # tree (see docs/ARCHITECTURE.md §5e). Must run after create_default_team.
    migrate_kb_to_team_storage(
        legacy_kb_folder=os.path.join(app.root_path, "kb_knowledge"),
        legacy_embeddings_folder=os.path.join(app.root_path, "embeddings"),
        teams_folder=app.config["TEAMS_FOLDER"],
        mhes_db_path=app.config["MHES_DB_PATH"],
    )
    # Phase 2 of multi-team support: create the users table and seed a
    # default Admin user for the default team (see docs/ARCHITECTURE.md §5c).
    create_default_admin_user(app.config["MHES_DB_PATH"])
    # Phase 7 of multi-team support: best-effort demo seed of Development
    # Team's Excel column mapping (see docs/ARCHITECTURE.md §5g). No-ops
    # harmlessly if that team doesn't exist in this environment.
    seed_development_team_import_config(app.config["MHES_DB_PATH"])
    # Phase 8 of multi-team support: best-effort demo seed of Development
    # Team's Excel export template (see docs/ARCHITECTURE.md §5h).
    seed_development_team_export_template(app.config["MHES_DB_PATH"])

    # Register blueprints
    _register_blueprints(app)

    # Register error handlers
    _register_error_handlers(app)

    # Start the temp data cleanup scheduler (replaces the old Windows
    # Task Scheduler + .bat file approach).
    init_scheduler(app)

    # Inject missing-embeddings count (for the current user's team) into
    # all templates.
    @app.context_processor
    def inject_missing_embeddings() -> dict:
        from services.excel_service import ExcelService
        from services.embedding_service import EmbeddingService

        team_id = session.get("team_id")
        if team_id is None:
            return {"missing_embeddings_count": 0}

        try:
            kb_folder, embeddings_folder, team_slug = team_folders_for_team_id(
                app.config["TEAMS_FOLDER"], app.config["MHES_DB_PATH"], team_id,
            )
            excel_svc = ExcelService(kb_folder=kb_folder)
            emb_svc = EmbeddingService(
                model_name=app.config["EMBEDDING_MODEL"],
                embeddings_folder=embeddings_folder,
                team_slug=team_slug,
            )
            kb_files = excel_svc.list_knowledge_files()
            count = sum(
                1 for f in kb_files if not emb_svc.has_index(f["filename"])
            )
        except Exception:
            count = 0
        return {"missing_embeddings_count": count}

    # Inject the logged-in user (or None) into all templates, so pages
    # can show a login link / account info without every route having to
    # pass it explicitly.
    @app.context_processor
    def inject_current_user() -> dict:
        from utils.auth import get_current_user

        return {"current_user": get_current_user()}

    # Register dashboard route
    @app.route("/dashboard")
    @login_required
    def dashboard() -> str:
        """Render the dashboard page (scoped to the current user's team)."""
        from services.excel_service import ExcelService
        from services.embedding_service import EmbeddingService

        kb_folder, embeddings_folder, team_slug = team_folders_for_team_id(
            app.config["TEAMS_FOLDER"], app.config["MHES_DB_PATH"], session["team_id"],
        )
        excel_svc = ExcelService(kb_folder=kb_folder)
        emb_svc = EmbeddingService(
            model_name=app.config["EMBEDDING_MODEL"],
            embeddings_folder=embeddings_folder,
            team_slug=team_slug,
        )
        kb_files = excel_svc.list_knowledge_files()
        kb_count = len(kb_files)
        embedded_count = sum(
            1 for f in kb_files if emb_svc.has_index(f["filename"])
        )
        return render_template(
            "dashboard.html",
            kb_count=kb_count,
            embedded_count=embedded_count,
        )

    # Default route — show chatbot
    @app.route("/")
    @login_required
    def index() -> str:
        """Render the chatbot page as the default landing page."""
        return render_template("chatbot.html")

    app.logger.info("MHES application initialized successfully.")
    return app


def _ensure_folders(app: Flask) -> None:
    """Create required folders if they do not exist.

    Args:
        app: Flask application instance.
    """
    folders = [
        app.config.get("UPLOAD_FOLDER", "uploads"),
        app.config.get("EXPORT_FOLDER", "exports"),
        app.config.get("LOG_FOLDER", "logs"),
        app.config.get("TEMP_DATA_FOLDER", "temp_data"),
        app.config.get("DATABASE_FOLDER", "database"),
        app.config.get("STORAGE_FOLDER", "storage"),
        app.config.get("TEAMS_FOLDER", os.path.join("storage", "teams")),
    ]
    for folder in folders:
        os.makedirs(folder, exist_ok=True)


def _register_blueprints(app: Flask) -> None:
    """Register all Flask blueprints.

    Args:
        app: Flask application instance.
    """
    from routes.upload import upload_bp
    from routes.chatbot import chatbot_bp
    from routes.preview import preview_bp
    from routes.export import export_bp
    from routes.auth import auth_bp
    from routes.admin import admin_bp

    app.register_blueprint(upload_bp, url_prefix="/upload")
    app.register_blueprint(chatbot_bp, url_prefix="/chatbot")
    app.register_blueprint(preview_bp, url_prefix="/preview")
    app.register_blueprint(export_bp, url_prefix="/export")
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")


def _register_error_handlers(app: Flask) -> None:
    """Register error handlers for common HTTP errors.

    Args:
        app: Flask application instance.
    """

    @app.errorhandler(404)
    def not_found(error: Exception) -> tuple[str, int]:
        """Handle 404 errors."""
        app.logger.warning(f"Page not found: {error}")
        return render_template("base.html", error="Page not found"), 404

    @app.errorhandler(500)
    def internal_error(error: Exception) -> tuple[str, int]:
        """Handle 500 errors."""
        app.logger.error(f"Internal server error: {error}")
        return render_template("base.html", error="Internal server error"), 500


if __name__ == "__main__":
    env = os.environ.get("FLASK_ENV", "development")
    application = create_app(env)
    application.run(host="0.0.0.0", port=3500, debug=(env == "development"))
