# MHES - Man Hour Estimation System

A Flask-based, multi-team system that helps engineering teams estimate man-hours by searching their own team's imported Excel knowledge files using AI semantic search, assembling results on an editable Preview screen, and exporting a formatted estimate — with login, role-based permissions, and per-team Knowledge Base isolation.

Knowledge base files, embeddings, and metadata are persisted on the local filesystem, one isolated folder tree per team. A SQLite database (`database/mhes.db`) holds teams, users, Preview stash/export history metadata, and per-team import/export configuration. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DATABASE.md](docs/DATABASE.md) for full details, including a consolidated multi-team architecture overview and migration history.

Looking to just *use* the app rather than install/develop it? See [docs/MHES_User_Manual.md](docs/MHES_User_Manual.md) instead.

## Installation

### Prerequisites

- Python 3.11+
- Ollama (with Qwen 2.5 3B model) — optional; the client library is included but not yet wired into the chatbot (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md))

### Windows Setup Commands

```cmd
REM 1. Navigate to project directory
cd D:\Infa\infra_manhour_estimation\multi-vendor\MHES

REM 2. Create virtual environment
python -m venv venv

REM 3. Activate virtual environment
venv\Scripts\activate

REM 4. Install packages
pip install -r requirements.txt

REM 5. Run Flask development server
python app.py

REM 6. Verify installation - open browser
start http://localhost:3500
```

### Ollama Setup

```cmd
REM Install Ollama from https://ollama.com
REM Pull the Qwen 2.5 3B model
ollama pull qwen2.5:3b
```

### Google Cloud Storage Setup

Generated Excel exports are stored in a private Google Cloud Storage bucket rather than the local filesystem (see `services/gcs_service.py`).

1. **Create a bucket** (private — do not enable public access):
   ```cmd
   gcloud storage buckets create gs://ai-team-001 --project=YOUR_PROJECT_ID --location=asia-southeast1 --uniform-bucket-level-access
   ```
2. **Create a service account** for the app to use, and grant it access to the bucket only:
   ```cmd
   gcloud iam service-accounts create mhes-export-service ^
     --display-name="MHES Export Storage"

   gcloud storage buckets add-iam-policy-binding gs://ai-team-001 ^
     --member="serviceAccount:mhes-export-service@YOUR_PROJECT_ID.iam.gserviceaccount.com" ^
     --role="roles/storage.objectAdmin"
   ```
   `roles/storage.objectAdmin` (scoped to just this bucket, not project-wide) grants upload, download, and signed-URL generation without any broader project permissions.
3. **Download a JSON key** for that service account and save it somewhere outside the repo:
   ```cmd
   gcloud iam service-accounts keys create mhes-gcs-key.json ^
     --iam-account=mhes-export-service@YOUR_PROJECT_ID.iam.gserviceaccount.com
   ```
4. **Configure environment variables** — copy `.env.example` to `.env` and fill in:
   ```
   GCP_PROJECT_ID=YOUR_PROJECT_ID
   GCP_BUCKET_NAME=ai-team-001
   GOOGLE_APPLICATION_CREDENTIALS=D:\path\to\mhes-gcs-key.json
   ```

Exported files are stored under a fixed object path: `mhes/bcmm/1001/{file_name}` (e.g. `gs://ai-team-001/mhes/bcmm/1001/estimate_001.xlsx`). Downloads are served via short-lived (15-minute) v4 signed URLs generated on demand — the bucket itself is never made public. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full upload/download flow.

## Running the Server

```cmd
REM Development
set FLASK_ENV=development
python app.py

REM Production (using waitress)
waitress-serve --host=0.0.0.0 --port=3500 app:create_app()
```

The app listens on **port 3500** by default (see `app.py`).

## Authentication & Teams

MHES requires login on every page except `/auth/login` itself. On first startup, the app automatically:

1. Creates a default team, **Infrastructure Team**, and attributes all pre-existing (pre-multi-team) data to it.
2. Creates a default `admin` account (role **Admin**) attached to that team.

The `admin` password comes from the `MHES_DEFAULT_ADMIN_PASSWORD` environment variable if it's set *before* the very first startup; otherwise a random password is generated and logged **once**, at `WARNING` level, to `logs/mhes.log` — capture it from there (or set the env var ahead of time) since it cannot be recovered afterward. There is no self-service password reset yet; to change a password or create additional users/teams, use the relevant repository directly (`repositories/user_repository.py`, `repositories/team_repository.py`) — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)'s Migration History for which phase added what.

### Roles

| Role | Can do |
|---|---|
| **Admin** | Everything below, plus manage users (`/admin/users`) and teams (`/admin/teams`), and see every team's Export History |
| **Team Manager** | Use the chatbot, create/preview/export estimates, and manage their own team's Knowledge Base (`/upload/...`) |
| **Member** | Use the chatbot, create/preview/export estimates — no Knowledge Base management |

### Teams

Every user belongs to exactly one team. A team's Knowledge Base, embeddings, Excel import column mapping, and Excel export template are all isolated per team — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §1a for the full architecture and [docs/DATABASE.md](docs/DATABASE.md) for the schema.

## How It Works

1. **Upload** — `.xlsx` knowledge files (Category → Task → Activity man-hour breakdowns) are uploaded and stored under the uploading user's own team folder, `storage/teams/<team_slug>/knowledge/`. Requires the Admin or Team Manager role.
2. **Embed** — each file is parsed into a nested Category/Task/Activity structure (using that team's configured Excel column mapping, if any — otherwise flexible generic keyword matching), converted to text chunks, embedded with Sentence Transformers, and indexed with FAISS into that same team's `storage/teams/<team_slug>/embeddings/`.
3. **Search** — the chatbot matches a query against known category/task/activity names first (including partial/word-level matches) within the current user's team only, then falls back to FAISS semantic search scoped to a single source file, returning grouped results with computed totals.
4. **Preview** — matched results are assembled on an editable Preview screen (add/edit/delete categories, tasks, and activities; live totals).
5. **Export** — the Preview estimate is exported to a formatted `.xlsx` workbook (using the current team's configured export column template, if any — otherwise the default 5-column layout), generated to a temporary local file, uploaded to a private Google Cloud Storage bucket, then the local temp file is deleted (see `services/gcs_service.py`). The export is recorded in Export History, scoped to the exporting user's team (Admins can see every team's exports).
6. **Temporary Data** — in-progress Preview data is automatically backed up server-side when starting a new chatbot session or closing the browser, and can be restored or discarded from the Temporary Data page. Backups older than a configurable retention period (default 7 days) are purged automatically on a daily schedule. This store is shared across all teams — it is not yet team-scoped (a known gap; see the Migration History table in `docs/ARCHITECTURE.md`).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for component diagrams, request flows, and the full multi-team architecture overview, and [docs/DATABASE.md](docs/DATABASE.md) for the complete schema (filesystem stores and SQLite tables, columns, relationships).

## Folder Structure

| Folder | Description |
|---|---|
| `storage/teams/<team_slug>/knowledge/` | Per-team stored knowledge base files (processed Excel data) |
| `storage/teams/<team_slug>/embeddings/` | Per-team FAISS vector index, mapping JSON, and embedding metadata |
| `kb_knowledge.bak/`, `embeddings.bak/` | Retired pre-multi-team shared folders, kept as a migration safety net (never read by the running app) |
| `temp_data/` | Server-side backups of in-progress Preview data (auto-purged on a schedule; shared across all teams) |
| `uploads/` | Temporary storage for uploaded Excel files |
| `exports/` | Temporary local staging area only — export workbooks are built here, uploaded to Google Cloud Storage, then deleted (see `services/gcs_service.py`) |
| `logs/` | Application log files (including the one-time default-admin password, if auto-generated) |
| `templates/` | Jinja2 HTML templates |
| `static/` | CSS, JavaScript, and image assets |
| `routes/` | Flask Blueprint route handlers (including `auth.py` for login/logout and `admin.py` for user/team management) |
| `services/` | Business logic service classes (Excel I/O, parsing, embeddings, search, auth, export history) |
| `repositories/` | Raw-SQL data access classes for SQLite-backed tables (teams, users, temp stashes, import/export config) |
| `scheduler/` | APScheduler integration and the Temporary Data store/cleanup logic |
| `utils/` | Utility functions and helpers (migrations, permissions, team storage path resolution) |
| `database/` | The shared SQLite database (`mhes.db`) and its connection helper |
| `models/` | Reserved for future ML model artifacts (currently empty) |
| `docs/` | Architecture, database, and end-user manual documentation |

## Tech Stack

- **Backend:** Flask, Jinja2, Bootstrap 5
- **Auth:** Flask session (built-in, signed cookie) + `werkzeug.security` password hashing — no external auth library
- **Data:** Pandas, OpenPyXL
- **AI:** Sentence Transformers, FAISS, Ollama (Qwen 2.5 3B, not yet connected)
- **Scheduling:** APScheduler (in-process background jobs)
- **Storage:** Local filesystem, one isolated tree per team (Knowledge Base, embeddings) + SQLite (`database/mhes.db`, for teams, users, export history, temp-data stash metadata, and per-team import/export configuration) + Google Cloud Storage (generated export files — see `services/gcs_service.py`)

## Documentation

- [docs/MHES_User_Manual.md](docs/MHES_User_Manual.md) — end-user manual: every screen (including Login and the Admin-only Manage Users/Manage Teams screens), step-by-step procedures, roles, error messages, FAQ, and known limitations. Start here if you just need to *use* MHES.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system overview, application architecture, frontend/backend breakdown, **a consolidated multi-team architecture overview (authentication flow, team architecture, Knowledge Base isolation, embedding structure, permission model, and migration history)**, the AI chatbot flow, and the scheduler/Temporary Data subsystem (with Mermaid diagrams)
- [docs/DATABASE.md](docs/DATABASE.md) — filesystem-based and SQLite-backed data stores, schema, relationships, and a consolidated schema-level migration history
#   m u l t i - k n o w l e d g e - m a n h o u r - p r e d i c t i o n  
 