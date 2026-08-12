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

The `admin` password comes from the `MHES_DEFAULT_ADMIN_PASSWORD` environment variable if it's set *before* the very first startup; otherwise a random password is generated and logged **once**, at `WARNING` level, to `logs/mhes.log` — capture it from there (or set the env var ahead of time) since it cannot be recovered afterward.

Users can self-service reset their own password via `/auth/forgot-password` (requests a single-use, time-limited reset link) and `/auth/reset-password/<token>`. An Admin can also trigger a reset link for another user from Manage Users. To create additional users or teams, use `/admin/users` / `/admin/teams` (Admin role) or the relevant repository directly (`repositories/user_repository.py`, `repositories/team_repository.py`) — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)'s Migration History for which phase added what.

### Roles

| Role | Can do |
|---|---|
| **Admin** | Everything below, plus manage users (`/admin/users`) and teams (`/admin/teams`), and see every team's Export History |
| **Team Manager** | Use the chatbot, create/preview/export estimates, and manage their own team's Knowledge Base (`/upload/...`) |

There is no separate "Member" role — every user is either Admin or Team Manager. New users must be created with an email address (required for the self-service password reset flow above).

### Teams

Every user belongs to exactly one team. A team's Knowledge Base, embeddings, Excel import parsing, and Excel export template are all isolated per team — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §1a for the full architecture and [docs/DATABASE.md](docs/DATABASE.md) for the schema.

Three teams currently have dedicated, team-specific Excel import/export support: **Bamawl Team**, **KiKan Team**, and **SGL Team** (see [Multi-Team Excel Import/Export Architecture](#multi-team-excel-importexport-architecture) below). Any other team falls back to a generic, config-driven or default layout.

## How It Works

1. **Upload** — `.xlsx` knowledge files (Category → Task → Activity man-hour breakdowns) are uploaded and stored under the uploading user's own team folder, `storage/teams/<team_slug>/knowledge/`. Requires the Admin or Team Manager role.
2. **Embed** — each file is parsed into a nested Category/Task/Activity structure — SGL Team via its own dedicated parser (`services/sgl_import_parser.py`), every other team via the generic, column-mapping-driven parser (`services/excel_parser.py`) — converted to text chunks, embedded with Sentence Transformers, and indexed with FAISS into that same team's `storage/teams/<team_slug>/embeddings/`.
3. **Search** — the chatbot matches a query against known category/task/activity names first (including partial/word-level matches) within the current user's team only, then falls back to FAISS semantic search scoped to a single source file, returning grouped results with computed totals. `SearchService` also preserves any additional fields already present on a matched task record (e.g. SGL's `work_detail`) generically, without hardcoding any team-specific field name (see [SGL 作業詳細 / work_detail Flow](#sgl-作業詳細--work_detail-flow) below).
4. **Preview** — matched results are assembled on an editable Preview screen (add/edit/delete categories, tasks, and activities; live totals). The Preview's current selection is the single source of truth for what gets exported.
5. **Export** — the Preview estimate is exported to a formatted `.xlsx` workbook — Bamawl/KiKan/SGL Team each via their own dedicated builder that copies and populates their official template (see below), every other team via the default column layout — generated to a temporary local file, uploaded to a private Google Cloud Storage bucket, then the local temp file is deleted (see `services/gcs_service.py`). The export is recorded in Export History, scoped to the exporting user's team (Admins can see every team's exports).
6. **Temporary Data** — in-progress Preview data is automatically backed up server-side when starting a new chatbot session or closing the browser, and can be restored or discarded from the Temporary Data page. Backups older than a configurable retention period (default 7 days) are purged automatically on a daily schedule. This store is shared across all teams — it is not yet team-scoped (a known gap; see the Migration History table in `docs/ARCHITECTURE.md`).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for component diagrams, request flows, and the full multi-team architecture overview, and [docs/DATABASE.md](docs/DATABASE.md) for the complete schema (filesystem stores and SQLite tables, columns, relationships).

## Multi-Team Excel Import/Export Architecture

Each of the three specially-supported teams (Bamawl, KiKan, SGL) uses its **own official Excel format** — none share a template or column layout. Import and Export both read/write that same team-specific format; the routes dispatch to the correct implementation by team name via two registries:

- `services/import_strategies.py::CUSTOM_IMPORT_PARSERS` — maps a team name to a dedicated parser function. Currently only `"SGL Team"` has an entry (`sgl_excel_to_nested_json`); Bamawl and KiKan use the shared, config-driven `services/excel_parser.py::excel_to_nested_json` (their column layout comes from a DB-seeded `column_mapping`, not a dedicated parser module).
- `services/export_strategies.py::EXPORT_STRATEGY_REGISTRY` — maps a team name to a dedicated `BaseExportService` subclass: `"Bamawl Team" → BamawlExportBuilder`, `"KiKan Team" → KikanExportBuilder`, `"SGL Team" → SglExportBuilder`. Any other team falls back to `DefaultExportStrategy`, which builds a fresh workbook from that team's configured (or default) column layout rather than copying a template.

**Template Download** — `GET` route in `routes/upload.py` (`download_template`) resolves the current user's team via `services/team_template_registry.py::get_team_template_spec(team_name)` and serves that team's `sample_template_path` file (falling back to the generic `simple_resource/MHES_KB_Template.xlsx` for teams without a dedicated spec).

**Template validation** — `services/team_template_validator.py::validate_team_template()` checks, for teams with a registered `TeamTemplateSpec`: (1) all required sheet names are present, (2) the designated header sheet is present, (3) that sheet's header row matches the expected headers exactly, position-by-position (a reordered or renamed header fails validation). Teams without a registered spec skip this check and fall back to the generic, lenient parser.

**One official template per team, not per purpose.** Bamawl and SGL each use a **two-file split**: a real internal workbook (used for import validation and as the export base) plus a separate, sanitized copy used only for Template Download so real project data is never exposed publicly. KiKan uses a **single file** for all three purposes (download, import validation, export base) — its template has no sample-data rows to sanitize.

| Team | Real/internal file (import + export base) | Public download/import-validation file | Knowledge worksheet |
|---|---|---|---|
| Bamawl Team | `simple_resource/bamawl_import_export_format_filled.xlsx` | `import/bamawl/bamawl_import_template.xlsx` | `ALL_Detail` |
| KiKan Team | `import/kikan/kikan_import_template.xlsx` (same file for everything) | *(same file)* | `工数詳細` |
| SGL Team | `simple_resource/sgl_import_export_format.xlsx` | `import/sgl/sgl_import_template.xlsx` | `詳細見積_マスタと予実比較` |

### Bamawl Team

- Official/export-base template: `simple_resource/bamawl_import_export_format_filled.xlsx`. Import validation and Template Download instead use the sanitized `import/bamawl/bamawl_import_template.xlsx`.
- Knowledge worksheet: `ALL_Detail` — read by the generic `excel_parser.py` using a DB-seeded `column_mapping` (single header row, phases mode), seeded via `utils/migrations/bamawl_import_export_config.py`.
- Export: `services/bamawl_export_builder.py::BamawlExportBuilder` copies the real internal template, populates it from the DB-seeded column mapping, and replaces the template's own sample project title cell with the current export's Project Name. Only dispatched by `routes/export.py::_select_export_strategy` when a non-empty DB-seeded mapping exists for the team; otherwise falls back to `DefaultExportStrategy`.

### KiKan Team

- Official template (single file, used for download, import validation, and export base): `import/kikan/kikan_import_template.xlsx`.
- Knowledge worksheet: `工数詳細` — read by the generic `excel_parser.py` using a DB-seeded `column_mapping`, seeded via `utils/migrations/kikan_import_export_config.py`.
- Export: `services/kikan_export_builder.py::KikanExportBuilder` copies the template and populates it, same dispatch-gating rule as Bamawl (only used when a DB-seeded mapping exists).
- **Known limitation:** the shipped `import/kikan/kikan_import_template.xlsx` has a pre-existing defect unrelated to any team's import/export logic — one of its columns depends on a formula (`VLOOKUP`) whose *cached* value was lost the last time the file was re-saved via `openpyxl`. Since `excel_parser.py` reads cell values (not live-recalculated formulas), importing this exact shipped file currently parses zero tasks until the workbook is re-opened and re-saved in Excel itself (which recalculates and re-caches formulas). This is a data/tooling issue with the shipped sample file, not application code.

### SGL Team

- Official/export-base template: `simple_resource/sgl_import_export_format.xlsx` (never modified by import or export — always copied first). Import validation and Template Download instead use the sanitized `import/sgl/sgl_import_template.xlsx`, generated from the official template by `import/sgl/build_sample_template.py`.
- Knowledge worksheet: `詳細見積_マスタと予実比較` (the workbook's other sheet, `見積・金額サマリ`, is a summary/amount rollup and is never read for knowledge import).
- SGL's worksheet layout is structurally different from Bamawl/KiKan's — a header split across two rows (row 2: field names + a merged "工数（人時間）" group label; row 3: six phase sub-labels — 要件定義/設計/開発/テスト/クラウド対応/その他 — underneath it) and task rows scattered across several blocks rather than one flat appendable range. Because of this, SGL has its own dedicated parser and builder instead of using the shared config-driven pipeline:
  - **Import:** `services/sgl_import_parser.py::sgl_excel_to_nested_json` reads the two-row header and phase columns directly from the template (never hardcoded), forward-fills 区分 (category) down blank rows, and treats a row as a real task only if 項目 (task name) is non-blank and at least one phase column is > 0.
  - **Export:** `services/sgl_export_builder.py::SglExportBuilder` copies the real internal template, discovers writable task rows dynamically from the template's own subtotal/per-row `SUM` formulas (rather than hardcoded row numbers), clears every writable row first, then writes each selected task into the discovered rows in order. Also replaces `見積・金額サマリ!A1`'s sample project title with the export's actual Project Name. Raises `SglExportError` if the selection exceeds the template's writable-row capacity.
  - Dispatched by `routes/export.py::_select_export_strategy` **unconditionally** (no DB-seeded-mapping gate, unlike Bamawl/KiKan) — SGL has no `column_mapping`; its structure is derived from the template itself every time.

## SGL 作業詳細 / work_detail Flow

SGL's worksheet has a `作業詳細` (work detail) column (column E) holding free-text task descriptions, distinct from `項目` (the task name) and from any phase/activity label. The implemented end-to-end flow for this field is:

```
Excel (作業詳細 column)
  → services/sgl_import_parser.py   (extracts the text, keyed per task)
  → Knowledge Base                  (stored as task_output["work_detail"];
                                      also appended into the task's embeddable text)
  → services/search_service.py      (preserves it as an extra field on the returned task)
  → Preview                         (task object carries work_detail through untouched)
  → services/sgl_export_builder.py  (reads task.get("work_detail"))
  → Exported Excel's 作業詳細 column
```

- `sgl_import_parser.py` extracts each task's 作業詳細 text from column E (accumulating across multiple rows for the same task, separated by newlines) and stores it as a dedicated `work_detail` field on that task's entry in the nested JSON, in addition to including it in the task's embeddable `text` field.
- `services/search_service.py` is **shared by every team**. It does not hardcode `work_detail` or any other team-specific field name. Instead, `_extra_task_fields()` generically copies through any field on a source task record that isn't one of the small set of universally-known fields (`id`, `task`, `activities`, `task_details`, `estimate_hours`, `buffer_hours`, `total_hours`, `text`) — so `work_detail` (or any future team-specific field) survives both the exact-match and FAISS-fallback search paths without any team-specific branching in the search logic itself. Teams/tasks without a `work_detail` field are entirely unaffected — search result shape for Bamawl/KiKan tasks is unchanged.
- `services/sgl_export_builder.py` reads `task.get("work_detail")` directly (not derived from `activities[]`, which only holds phase labels like "開発", not descriptive text) and writes it into the exported row's 作業詳細 cell only if present and non-blank.
- Each exported task shows only its own 作業詳細 — no cross-task leakage in either direction — and a task with no `work_detail` is left blank rather than showing another task's text or a placeholder.

## SGL Export Selection

- Only the 項目 (tasks) present in the current Preview selection are written into the exported workbook.
- Every writable row in the template is cleared (区分, 項目, 作業詳細, priority/status columns, all phase-hour columns, remarks) **before** any selected task is written, so a row not used by the current selection can never retain the template's own sample data or a previous export's leftover values.
- Tasks/categories not present in the Preview selection — including their own 作業詳細 — do not appear anywhere in the exported file.
- Total Man Hour and Total Tasks in the exported workbook (computed by the template's own existing `SUM` formulas) reflect only the selected/exported tasks, since every non-written row is blank.

## SearchService

`services/search_service.py::SearchService` is the single shared search implementation used by every team (team isolation comes entirely from the `EmbeddingService` instance it's constructed with, which is already scoped to one team's `embeddings_folder`). Both of its result-construction paths — exact/partial name matching (`_exact_match_search`) and FAISS semantic fallback (`_faiss_fallback_search`) — converge on the same two functions, `_load_full_task` and `_build_task_row`, which build the final task object returned to the client. Both now spread in `**_extra_task_fields(task)`: any field already present on the source task record that isn't one of the fixed set of universally-known fields is preserved as-is, with no team- or field-specific hardcoding. This is what allows SGL's `work_detail` to reach Preview/export without any SGL-specific code inside the shared search service, and leaves every other team's result shape byte-for-byte unchanged.

## Team Isolation

SGL-specific work (its dedicated import parser, export builder, and template files) lives entirely in SGL-only modules (`services/sgl_import_parser.py`, `services/sgl_export_builder.py`, `import/sgl/`) and is registered only under `"SGL Team"` in the `CUSTOM_IMPORT_PARSERS`/`EXPORT_STRATEGY_REGISTRY` registries — it is never reached for Bamawl or KiKan requests. The one shared file touched to support SGL, `services/search_service.py`, was changed additively only (a generic extra-field passthrough, no hardcoded field names or team checks), and verified via real end-to-end route tests (`/upload/`, `/chatbot/search`, `/export/excel`) for both Bamawl and KiKan that: their search result shape is unchanged (no extra fields appear), their own official template files are never modified by any test run, and their own import/export routes continue to succeed.

## Testing / Verification

The SGL import → Knowledge Base → SearchService → Preview → export flow, and Bamawl/KiKan isolation, have been verified with real end-to-end tests exercising the actual Flask routes (`/upload/`, `/chatbot/search`, `/export/excel`) against a temporary SQLite database and temp team-storage folders — not hand-built payloads. Verified, at the time of writing:

- A real SGL import of the official template, a real chatbot search for specific tasks (one with 作業詳細, one without, and a deliberately-unselected task with its own 作業詳細), a Preview-selection payload built the same way `templates/chatbot.html` builds it (a full JSON deep-copy of the search result), and a real export — confirming: only selected 項目 are exported; each selected task's own 作業詳細 is exported exactly, with no cross-task leakage in either direction; the task without 作業詳細 stays blank; the deliberately-unselected task and its 作業詳細 are absent everywhere in the output; every unused writable row is fully clear; Total Man Hour and Total Tasks match the Preview selection exactly; worksheet names, merged cells, formulas, and column widths are all preserved; the original template file is byte-for-byte and mtime-unchanged after export.
- Real Bamawl and KiKan import/search/export routes continue to work unchanged; their search result shape carries no extra fields; their own official template files are untouched.
- KiKan's known pre-existing sanitized-template defect (see KiKan Team section above) was encountered again during this verification and confirmed, via `git status`, to be unrelated to any change made for SGL — it was not introduced or worsened by this work.

These were ad hoc verification scripts run during development, not a committed automated test suite — there is currently no `tests/` directory or CI-integrated test runner in this repository for the import/export flows.

## Current Implementation Status

**Implemented:**
- Bamawl, KiKan, and SGL Team dedicated Excel import/export (each via its own registry-dispatched parser/builder), per-team Template Download, per-team template structural validation.
- SGL's `work_detail` (作業詳細) extraction, storage, generic preservation through `SearchService`, and export.
- Generic extra-task-field passthrough in `SearchService`, available to any team/field without code changes.

**Verified (via real end-to-end route tests, see above):**
- SGL import → Knowledge Base → search → Preview-shaped selection → export round trip for 作業詳細, including no-leakage and blank-when-absent behavior.
- Bamawl/KiKan search-result-shape and template-file isolation from SGL's changes.

**Remaining / Not implemented:**
- No automated/CI test suite for import/export flows — verification so far has been manual, ad hoc scripts run during development.
- KiKan's shipped sanitized sample template (`import/kikan/kikan_import_template.xlsx`) currently imports zero tasks due to a lost cached formula value (see KiKan Team section) — a pre-existing, unrelated defect, not fixed as part of this work.
- Ollama chatbot wiring and Temporary Data team-scoping remain unimplemented (see the Authentication & Teams and How It Works sections above, and `docs/ARCHITECTURE.md`'s Migration History) — unrelated to the Excel import/export work described in this section.

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
| `routes/` | Flask Blueprint route handlers, incl. `auth.py` (login/logout/password reset), `admin.py` (user/team management), `upload.py` (import + Template Download), `preview.py` (Preview stash CRUD), `export.py` (per-team export dispatch) |
| `services/` | Business logic service classes — Excel I/O/parsing, embeddings, search, auth, export history, plus per-team modules: `bamawl_export_builder.py`, `kikan_export_builder.py`, `sgl_import_parser.py`, `sgl_export_builder.py`, and the dispatch registries `import_strategies.py`/`export_strategies.py` |
| `repositories/` | Raw-SQL data access classes for SQLite-backed tables (teams, users, temp stashes, import/export config) |
| `scheduler/` | APScheduler integration and the Temporary Data store/cleanup logic |
| `utils/` | Utility functions and helpers (migrations, incl. `utils/migrations/{bamawl,kikan,sgl}_import_export_config.py`; permissions; team storage path resolution) |
| `import/{bamawl,kikan,sgl}/` | Each team's public download/import-validation template file, plus the one-off sanitization script (`build_sample_template.py`) that generated it from the real internal file (Bamawl/SGL only — KiKan has no separate real file) |
| `simple_resource/` | Misc. reference/sample resources, including each team's real internal Excel format used as the export base (`bamawl_import_export_format_filled.xlsx`, `sgl_import_export_format.xlsx`) |
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