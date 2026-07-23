"""Application configuration for MHES."""

import os

from dotenv import load_dotenv

# Load variables from a .env file (if present) into the process environment
# before any of the os.environ.get(...) calls below run. Real secrets
# (GCP service account key, bucket name, etc.) live in .env, which is
# git-ignored — see .env.example for the variables this app expects.
load_dotenv()

BASE_DIR: str = os.path.abspath(os.path.dirname(__file__))


class Config:
    """Base configuration."""

    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
    DEBUG: bool = False
    TESTING: bool = False

    # Session cookie (Flask's built-in, SECRET_KEY-signed session — used
    # for both flash messages and, as of Phase 2, login sessions)
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"

    # Folder paths
    UPLOAD_FOLDER: str = os.path.join(BASE_DIR, "uploads")
    EXPORT_FOLDER: str = os.path.join(BASE_DIR, "exports")
    LOG_FOLDER: str = os.path.join(BASE_DIR, "logs")
    TEMP_DATA_FOLDER: str = os.path.join(BASE_DIR, "temp_data")
    DATABASE_FOLDER: str = os.path.join(BASE_DIR, "database")
    MHES_DB_PATH: str = os.path.join(DATABASE_FOLDER, "mhes.db")

    # Team-isolated Knowledge Base / embeddings storage (Phase 4 of
    # multi-team support). Replaces the old global KB_FOLDER/EMBEDDINGS_FOLDER
    # — each team's data now lives under storage/teams/<team_slug>/{knowledge,embeddings}
    # (see utils/team_storage.py). The pre-Phase-4 kb_knowledge/ and
    # embeddings/ folders are migrated into the default team's tree on
    # startup (utils/migration.py::migrate_kb_to_team_storage).
    STORAGE_FOLDER: str = os.path.join(BASE_DIR, "storage")
    TEAMS_FOLDER: str = os.path.join(STORAGE_FOLDER, "teams")

    # Upload settings
    MAX_CONTENT_LENGTH: int = 10 * 1024 * 1024  # 10 MB
    ALLOWED_EXTENSIONS: set[str] = {"xlsx"}

    # AI settings
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    OLLAMA_MODEL: str = "qwen2.5:3b" #"llama3.1:latest" #"qwen2.5:3b"
    OLLAMA_BASE_URL: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

    # Google Cloud Storage (export file storage — see services/gcs_service.py)
    # GOOGLE_APPLICATION_CREDENTIALS is intentionally not read here: the
    # underlying google-cloud-storage client reads that env var directly,
    # so it only needs to be set in the environment/.env, not threaded
    # through this config object.
    GCP_PROJECT_ID: str | None = os.environ.get("GCP_PROJECT_ID") or None
    GCP_BUCKET_NAME: str | None = os.environ.get("GCP_BUCKET_NAME") or None

    # Temp data cleanup (APScheduler)
    TEMP_DATA_RETENTION_DAYS: int = int(os.environ.get("TEMP_DATA_RETENTION_DAYS", "7"))
    TEMP_DATA_CLEANUP_TIMES: list[str] = [
        t.strip()
        for t in os.environ.get("TEMP_DATA_CLEANUP_TIMES", "10:00,15:00").split(",")
        if t.strip()
    ]
    TEMP_DATA_TIMEZONE: str = os.environ.get("TEMP_DATA_TIMEZONE", "Asia/Yangon")


class DevelopmentConfig(Config):
    """Development configuration."""

    DEBUG: bool = True


class ProductionConfig(Config):
    """Production configuration."""

    DEBUG: bool = False


class TestingConfig(Config):
    """Testing configuration."""

    TESTING: bool = True


config_by_name: dict[str, type[Config]] = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
