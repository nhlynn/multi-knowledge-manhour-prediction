# MHES — Man-Hour Estimation System

> A multi-team, AI-powered web application that converts each team's man-hour knowledge base into
> searchable, editable, and exportable project estimates — with login, role-based permissions, and
> per-team Knowledge Base isolation.

---

## Table of Contents

- [System Purpose](#system-purpose)
- [Business Objectives](#business-objectives)
- [Main Features](#main-features)
- [Target Users](#target-users)
- [Benefits](#benefits)
- [Technology Stack](#technology-stack)
- [Quick Start](#quick-start)
- [Documentation Index](#documentation-index)
- [Future Improvements](#future-improvements)

---

## System Purpose

MHES is an internal tool designed to eliminate manual man-hour estimation for
development projects. Engineers upload historical Excel estimation sheets into their own team's
knowledge base. The system automatically indexes the data using AI embeddings, allowing users to
query it conversationally (within their own team's data only), assemble custom estimates from
search results, edit them inline, and export a formatted Excel report — all without touching a
spreadsheet manually.

Multiple teams can use the same MHES installation side by side. Every user belongs to exactly one
team and has one of three roles (Admin, Team Manager, Member); each team's Knowledge Base,
embeddings, Excel import column mapping, and Excel export template are all completely isolated
from every other team's.

---

## Business Objectives

| Objective | Description |
|---|---|
| **Reduce estimation time** | Cut the time required to produce a man-hour estimate from hours to minutes |
| **Standardize estimates** | Ensure all estimates derive from a single, version-controlled knowledge base — per team |
| **Enable reuse** | Allow historical project data to inform new estimates via semantic search |
| **Reduce errors** | Eliminate manual copy-paste between Excel files |
| **Improve traceability** | Every estimate can be traced back to its source knowledge file, team, and (where known) the user who created it |
| **Support multiple teams safely** | Let unrelated teams share one MHES installation without ever seeing each other's Knowledge Base or Export History |

---

## Main Features

### Authentication & Role-Based Access
Every screen except Login requires a session. Three roles: **Admin** (everything, plus manage
users/teams and see every team's Export History), **Team Manager** (chatbot/Preview/Export, plus
manage their own team's Knowledge Base), **Member** (chatbot/Preview/Export only).

### Team-Isolated Knowledge Base Management
Upload one or more Excel files (`.xlsx`) containing historical man-hour data — stored under the
uploading user's own team's folder only. The system automatically validates, stores, and indexes
them. Duplicate files can be renamed or overwritten. Files can be deleted or re-indexed at any
time. A team can optionally configure its own Excel column mapping (different header names, a
specific sheet/header row, or a full phase-by-phase breakdown — see Technology Stack below) instead
of MHES's generic column detection.

### AI Semantic Search (Chatbot)
A chat-style interface where users describe what they need in plain language, searching only their
own team's Knowledge Base. The system searches using a two-phase strategy: exact/partial name
matching first (including word-level matches, e.g. "wordpress documentation" correctly scopes to
the "Wordpress" category), then semantic vector search as a fallback, scoped to a single source
file to avoid mixing results from unrelated knowledge files. Results are grouped into a
Category → Task → Activity hierarchy. The conversation is remembered across a session and resumes
when returning from Preview, but starts fresh from any other entry point.

### Interactive Preview
Search results are assembled on a Preview screen showing the full estimation hierarchy.
Every field — category name, task name, activity detail, hours, and buffer — is editable
inline directly in the browser. Changes recalculate totals in real time.

### Excel Export
Generates a professionally formatted `.xlsx` file with merged category cells, numbered task
rows, working-day formulas (`=hours/8`), and a styled totals row. A team can configure its own
export column layout (which columns appear, their labels, order, and width) instead of MHES's
default 5-column layout. Exporting never modifies the Knowledge Base — it only produces the
downloadable file and a record in that team's Export History.

### Export History
Every export is recorded with the team and (where known) the actual user who created it. Team
Managers and Members see only their own team's Export History; Admins see every team's.

### Temporary Data (Preview Stashing)
In-progress Preview data is automatically backed up to the server whenever the user starts a
new chatbot session or closes/refreshes the browser with unsaved changes. Backups ("stashes")
can be reviewed, restored back into Preview, or discarded from a dedicated Temporary Data page,
and are purged automatically once older than a configurable retention period (default 7 days)
via a scheduled background job. Unlike the Knowledge Base and Export History, this store is not
yet scoped per team — a known limitation (see Future Improvements).

---

## Target Users

| Role in MHES | Typical job title | How they use MHES |
|---|---|---|
| **Admin** | System Administrator | Manages users/teams, uploads/manages KB files, sees every team's Export History |
| **Team Manager** | Technical Lead, Infrastructure Engineer | Manages their team's Knowledge Base, searches, assembles estimates, exports |
| **Member** | Project Manager, Engineer | Searches, assembles estimates, adjusts buffers, exports reports |

---

## Benefits

- **No spreadsheet skill required**: the chat interface handles search and assembly
- **Self-correcting estimates**: buffer logic adjusts automatically based on partial vs. full task scope
- **Multi-team by design**: unrelated teams can share one installation with zero visibility into each other's data
- **Configurable per team**: each team can import/export using its own Excel column layout, without a separate parser or exporter being written for it
- **Minimal database footprint**: only teams/users/session-adjacent metadata and per-team configuration live in SQLite; Knowledge Base content itself stays in plain files — portable and auditable
- **Offline capable**: runs entirely on-premises with no cloud dependency (except CDN assets and, optionally, Google Cloud Storage for export files)
- **Extensible knowledge base**: add any number of Excel files per team; the index updates automatically

---

## Technology Stack

### Frontend

| Component | Technology | Version |
|---|---|---|
| UI Framework | Bootstrap | 5.3.3 |
| Icons | Bootstrap Icons | 1.11.3 |
| Typography | Inter (Google Fonts) | Variable |
| Templating | Jinja2 | 3.1.6 |
| JavaScript | Vanilla JS (ES6+) | — |
| State management | Browser `sessionStorage` (chat/Preview state), `localStorage` (sidebar UI preference) | — |

### Backend

| Component | Technology | Version |
|---|---|---|
| Language | Python | 3.11+ |
| Web Framework | Flask | 3.1.1 |
| WSGI Server (prod) | Waitress | 3.0.2 |
| Configuration | python-dotenv | 1.1.0 |
| Scheduling | APScheduler (`BackgroundScheduler`) | 3.11.3 |

### Data Processing

| Component | Technology | Version |
|---|---|---|
| Excel parsing | pandas + openpyxl | 2.2.3 / 3.1.5 |
| Excel generation | openpyxl | 3.1.5 |
| File validation | Werkzeug | (Flask dep) |

### AI / ML

| Component | Technology | Version |
|---|---|---|
| Embedding model | sentence-transformers (`all-MiniLM-L6-v2`) | 3.4.1 |
| Vector index | FAISS (`IndexFlatL2`) | 1.9.0.post1 |
| LLM client | Ollama (`qwen2.5:3b`) | 0.4.8 |
| Numerical compute | NumPy | (transitive dep) |

> **Note**: The Ollama LLM integration is included as a dependency but is not yet connected
> to the chatbot endpoint. The current chatbot uses structured semantic search only.
> LLM-powered response generation is a planned enhancement.

### Storage

| Component | Technology |
|---|---|
| KB files | Local filesystem, one isolated tree per team (`storage/teams/<team_slug>/knowledge/*.xlsx`) |
| Vector indices | Local filesystem, per team (`storage/teams/<team_slug>/embeddings/*.faiss`) |
| Mapping/metadata data | Local filesystem, per team (`storage/teams/<team_slug>/embeddings/*.json`) |
| Teams, users, Preview stash metadata, Export History, per-team import/export configuration | SQLite (`database/mhes.db`) — see `docs/DATABASE.md` |
| Generated export files | Google Cloud Storage (private bucket, signed-URL downloads) — see `services/gcs_service.py` |
| Logs | Local filesystem (rotating `.log`) |

### Authentication

| Component | Technology |
|---|---|
| Session | Flask's built-in, `SECRET_KEY`-signed cookie — no external session/auth library |
| Password hashing | `werkzeug.security` |
| Authorization | Custom decorators/`before_request` hooks (`utils/permissions.py`) — no external RBAC library |

---

## Quick Start

```bash
# 1. Clone the repository
git clone <repository-url>
cd MHES

# 2. Create and activate virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux / macOS

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the application
python app.py
```

Open `http://localhost:4000` in your browser and log in (see "Authentication & Teams" in the root
README for the default Admin account on a fresh install).

See the root [README.md](../README.md) for full installation and running instructions
(prerequisites, Ollama setup, Google Cloud Storage setup, dev/production server commands,
default-admin login).

---

## Documentation Index

| Document | Audience | Description |
|---|---|---|
| [../README.md](../README.md) | All | Project landing page: installation, running the server, authentication/roles, folder structure, tech stack |
| [MHES_User_Manual.md](MHES_User_Manual.md) | End users | Every screen (including Login and the Admin-only Manage Users/Manage Teams screens), step-by-step procedures, roles, error messages, FAQ, known limitations |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Developers, Architects | Application architecture, frontend/backend breakdown, a consolidated multi-team architecture overview (authentication flow, team architecture, Knowledge Base isolation, embedding structure, permission model, migration history), the AI chatbot flow, and the scheduler/Temporary Data subsystem (Mermaid diagrams) |
| [DATABASE.md](DATABASE.md) | Developers, Sysadmins | Filesystem-based and SQLite-backed data stores, schema, relationships, and a consolidated schema-level migration history |

---

## Future Improvements

### AI Enhancements
- **Connect Ollama LLM**: Wire the already-installed `qwen2.5:3b` model to generate natural
  language explanations alongside search results
- **Conversational memory**: Multi-turn chat context so follow-up questions refine previous results
- **Approximate vector search**: Replace `IndexFlatL2` with FAISS `IVFFlat` or HNSW for faster
  search as the knowledge base grows
- **Multi-modal search**: Support searching by project type, duration range, or category filter
  in addition to text queries

### UI Enhancements
- **Drag-and-drop reordering**: Allow tasks and activities to be reordered in the Preview screen
- **PDF export**: Generate a PDF summary alongside the Excel export
- **Dark mode**: System-wide dark theme toggle
- **Undo/redo**: History stack for inline edits on the Preview screen

### Performance Improvements
- **Async embedding**: Run embedding generation in a background task (Celery/RQ) instead of
  blocking the upload request
- **Index caching**: Cache loaded FAISS indices in memory between requests instead of reading
  from disk on every search
- **Streaming search results**: Stream chatbot results to the frontend progressively

### Additional Features
- **Team-scope Temporary Data**: Preview stashes are still shared across every logged-in user and
  team — unlike the Knowledge Base and Export History, which are already team-isolated
- **Self-service account management**: no password reset/change, and no in-app way to create/edit
  users or teams yet — `/admin/users` and `/admin/teams` are read-only; accounts are created
  directly via `repositories/user_repository.py`/`repositories/team_repository.py`
- **Import/export configuration UI**: a team's Excel column mapping (including the phase-breakdown
  "phases mode" — see `docs/ARCHITECTURE.md` §5g) and export template are configured directly via
  `TeamImportConfigRepository`/`TeamExportTemplateRepository`; there's no admin screen for it
- **Configurable header-row offset per sheet-selection UI**: currently set via the same repository
  calls above (`header_row`, `sheet` keys) — a form to pick these visually (rather than inspecting
  the workbook by hand) would make onboarding a new team's real-world Excel format much faster
- **Named/managed project drafts**: automatic Preview stashing (see Main Features) already covers
  ad-hoc backup/restore; still missing is user-initiated naming/tagging of drafts for deliberate
  long-term reuse
- **Audit trail**: Log who changed what and when on each estimate
- **Knowledge base editor**: Edit KB Excel data directly in the browser without re-uploading
- **CI/CD pipeline**: Automated testing and deployment with GitHub Actions
- **Docker support**: `Dockerfile` and `docker-compose.yml` for containerized deployment
