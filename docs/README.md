# MHES — Man-Hour Estimation System

> An AI-powered web application that converts infrastructure man-hour knowledge bases into
> searchable, editable, and exportable project estimates.

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

MHES is an internal tool designed to eliminate manual man-hour estimation for infrastructure
projects. Engineers upload historical Excel estimation sheets into a knowledge base. The system
automatically indexes the data using AI embeddings, allowing users to query it conversationally,
assemble custom estimates from search results, edit them inline, and export a formatted Excel
report — all without touching a spreadsheet manually.

---

## Business Objectives

| Objective | Description |
|---|---|
| **Reduce estimation time** | Cut the time required to produce a man-hour estimate from hours to minutes |
| **Standardize estimates** | Ensure all estimates derive from a single, version-controlled knowledge base |
| **Enable reuse** | Allow historical project data to inform new estimates via semantic search |
| **Reduce errors** | Eliminate manual copy-paste between Excel files |
| **Improve traceability** | Every estimate can be traced back to its source knowledge file |

---

## Main Features

### Knowledge Base Management
Upload one or more Excel files (`.xlsx`) containing historical man-hour data. The system
automatically validates, stores, and indexes them. Duplicate files can be renamed or overwritten.
Files can be deleted or re-indexed at any time.

### AI Semantic Search (Chatbot)
A chat-style interface where users describe what they need in plain language. The system
searches the knowledge base using a two-phase strategy: exact/partial name matching first
(including word-level matches, e.g. "wordpress documentation" correctly scopes to the
"Wordpress" category), then semantic vector search as a fallback, scoped to a single source
file to avoid mixing results from unrelated knowledge files. Results are grouped into a
Category → Task → Activity hierarchy. The conversation is remembered across a session and
resumes when returning from Preview, but starts fresh from any other entry point.

### Interactive Preview
Search results are assembled on a Preview screen showing the full estimation hierarchy.
Every field — category name, task name, activity detail, hours, and buffer — is editable
inline directly in the browser. Changes recalculate totals in real time.

### Excel Export
Generates a professionally formatted `.xlsx` file with merged category cells, numbered task
rows, working-day formulas (`=hours/8`), and a styled totals row matching the standard
infrastructure estimation template. If the exported project contains any Category, Task, or
Activity Detail not already in the knowledge base, that data is automatically written into the
knowledge base and embedded — so a curated estimate can grow the knowledge base without a
separate manual upload step.

### Temporary Data (Preview Stashing)
In-progress Preview data is automatically backed up to the server whenever the user starts a
new chatbot session or closes/refreshes the browser with unsaved changes. Backups ("stashes")
can be reviewed, restored back into Preview, or discarded from a dedicated Temporary Data page,
and are purged automatically once older than a configurable retention period (default 7 days)
via a scheduled background job.

---

## Target Users

| Role | How they use MHES |
|---|---|
| **Infrastructure Engineers** | Search for tasks, assemble estimates, export to Excel |
| **Project Managers** | Review estimates, adjust buffers, export reports |
| **System Administrators** | Upload KB files, manage embeddings, monitor logs |
| **Technical Leads** | Validate estimates against historical data |

---

## Benefits

- **No spreadsheet skill required**: the chat interface handles search and assembly
- **Self-correcting estimates**: buffer logic adjusts automatically based on partial vs. full task scope
- **No database to manage**: all data lives in plain files — portable and auditable
- **Offline capable**: runs entirely on-premises with no cloud dependency (except CDN assets)
- **Extensible knowledge base**: add any number of Excel files; the index updates automatically

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
| KB files | Local filesystem (`.xlsx`) |
| Vector indices | Local filesystem (`.faiss`) |
| Mapping data | Local filesystem (`.json`) |
| Temporary Preview backups | Local filesystem (`temp_data/stashes.json`) |
| Logs | Local filesystem (rotating `.log`) |

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

Open `http://localhost:5000` in your browser.

See the root [README.md](../README.md) for full installation and running instructions
(prerequisites, Ollama setup, dev/production server commands).

---

## Documentation Index

| Document | Audience | Description |
|---|---|---|
| [../README.md](../README.md) | All | Project landing page: installation, running the server, folder structure, tech stack |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Developers, Architects | Application architecture, frontend/backend breakdown, AI chatbot flow, scheduler/Temporary Data subsystem (Mermaid diagrams) |
| [DATABASE.md](DATABASE.md) | Developers, Sysadmins | Filesystem-based data stores, schema, and relationships |

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
- **User authentication**: Login system with role-based access (admin vs. estimator). Also needed
  to properly scope Temporary Data stashes per-user — they are currently shared by everyone
  using the app
- **Named/managed project drafts**: automatic Preview stashing (see Main Features) already covers
  ad-hoc backup/restore; still missing is user-initiated naming/tagging of drafts for deliberate
  long-term reuse
- **Audit trail**: Log who changed what and when on each estimate
- **Knowledge base editor**: Edit KB Excel data directly in the browser without re-uploading
- **Partial knowledge contribution on export**: currently, if any part of an exported project is
  new, the *entire* project is added to the knowledge base; splitting out only the genuinely new
  Category/Task/Activity data would avoid duplicating already-known content
- **CI/CD pipeline**: Automated testing and deployment with GitHub Actions
- **Docker support**: `Dockerfile` and `docker-compose.yml` for containerized deployment
