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

Four teams currently have dedicated, team-specific Excel import/export support: **Bamawl Team**, **KiKan Team**, **SGL Team**, and **SSD Team** (see [Multi-Team Excel Import/Export Architecture](#multi-team-excel-importexport-architecture) below). The default seeded team, **Infrastructure Team**, uses the generic column layout and is the only team that uses per-task **Buffer** and the project/per-task **Remark** feature. Any other team falls back to a generic, config-driven or default layout.

Per-team estimation behavior differs on the Preview screen:

- **Bamawl** and **KiKan** derive every phase from a single **Development** man-hours input using fixed ratio coefficients (mirroring their Excel templates' own formulas); only Development is entered, and a collapsible **Percentage (%)** panel lets the user adjust each derived phase's ratio.
- **SGL** tasks have a fixed set of phase columns, each a single estimate value.
- **SSD** tasks have a fixed set of phase columns, each carrying a 標準 (standard) / 調整 (adjustment) / 見積 (estimate) breakdown.
- **Infrastructure Team** enters activity hours directly and can add a per-task Buffer and a rich-text Remark.

## How It Works

1. **Upload** — `.xlsx` knowledge files (Category → Task → Activity man-hour breakdowns) are uploaded and stored under the uploading user's own team folder, `storage/teams/<team_slug>/knowledge/`. Requires the Admin or Team Manager role.
2. **Embed** — each file is parsed into a nested Category/Task/Activity structure — SGL/KiKan/SSD Team each via their own dedicated parser (registered in `services/import_strategies.py::CUSTOM_IMPORT_PARSERS`), Bamawl and every other team via the generic, column-mapping-driven parser (`services/excel_parser.py`) — converted to text chunks, embedded with Sentence Transformers, and indexed with FAISS into that same team's `storage/teams/<team_slug>/embeddings/`.
3. **Search** — the chatbot matches a query against known category/task/activity names first (including partial/word-level matches) within the current user's team only, then falls back to FAISS semantic search scoped to a single source file, returning grouped results with computed totals. `SearchService` also preserves any additional fields already present on a matched task record (e.g. SGL's `work_detail`) generically, without hardcoding any team-specific field name (see [SGL 作業詳細 / work_detail Flow](#sgl-作業詳細--work_detail-flow) below).
4. **Preview** — matched results are assembled on an editable Preview screen (add/edit/delete categories, tasks, and activities; live totals). The Preview's current selection is the single source of truth for what gets exported.
5. **Export** — the Preview estimate is exported to a formatted `.xlsx` workbook — Bamawl/KiKan/SGL/SSD Team each via their own dedicated builder (dispatched through a Strategy Pattern registry, `services/export_strategies.py`) that copies and populates their official template (see below), every other team via the default column layout (`services/export_workbook_service.py`) — generated to a temporary local file, uploaded to a private Google Cloud Storage bucket, then the local temp file is deleted (see `services/gcs_service.py`). Bamawl's and KiKan's exports deliberately **keep the template's live Excel formulas**: only Development is written as a literal and the derived-phase/total formulas are re-injected per row (via `openpyxl.formula.translate.Translator`), so the workbook recomputes in Excel when Development changes. The export is recorded in Export History, scoped to the exporting user's team (Admins can see every team's exports).
6. **Temporary Data** — in-progress Preview data is automatically backed up server-side when starting a new chatbot session or closing the browser, and can be restored or discarded from the Temporary Data page. Backups older than a configurable retention period (default 7 days) are purged automatically on a daily schedule. This store is shared across all teams — it is not yet team-scoped (a known gap; see the Migration History table in `docs/ARCHITECTURE.md`).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for component diagrams, request flows, and the full multi-team architecture overview, and [docs/DATABASE.md](docs/DATABASE.md) for the complete schema (filesystem stores and SQLite tables, columns, relationships).

## Multi-Team Excel Import/Export Architecture

Each of the four specially-supported teams (Bamawl, KiKan, SGL, SSD) uses its **own official Excel format** — none share a template or column layout. Import and Export both read/write that same team-specific format; the routes dispatch to the correct implementation by team name via two registries:

- `services/import_strategies.py::CUSTOM_IMPORT_PARSERS` — maps a team name to a dedicated parser function: `"SGL Team" → sgl_excel_to_nested_json`, `"KiKan Team" → kikan_excel_to_nested_json`, `"SSD Team" → ssd_excel_to_nested_json`. **Bamawl uses no dedicated parser** — it stays 100% on the shared, config-driven `services/excel_parser.py::excel_to_nested_json`, whose column layout (phases mode) comes from a DB-seeded `column_mapping`. (KiKan's own `工数詳細` sheet is also parsed by the generic engine; its dedicated parser only adds a thin enrichment step for a second cross-reference worksheet.)
- `services/export_strategies.py::EXPORT_STRATEGY_REGISTRY` — maps a team name to a dedicated `BaseExportService` subclass: `"Bamawl Team" → BamawlExportBuilder`, `"KiKan Team" → KikanExportBuilder`, `"SGL Team" → SglExportBuilder`, `"SSD Team" → SsdExportBuilder`. Any other team falls back to `DefaultExportStrategy`, which builds a fresh workbook from that team's configured (or default) column layout rather than copying a template.

**Template Download** — `GET` route in `routes/upload.py` (`download_template`) resolves the current user's team via `services/team_template_registry.py::get_team_template_spec(team_name)` and serves that team's `sample_template_path` file (falling back to the generic `simple_resource/MHES_KB_Template.xlsx` for teams without a dedicated spec).

**Template validation** — `services/team_template_validator.py::validate_team_template()` checks, for teams with a registered `TeamTemplateSpec`: (1) all required sheet names are present, (2) the designated header sheet is present, (3) the header row is acceptable. By default check (3) is strict — the header row must match the expected headers **exactly and in order** (a reordered or renamed header fails). A spec may instead set `required_columns`, which switches to a **lenient** check: only those columns must be present in the header sheet (matched whitespace/case-tolerantly, in any order, extra columns allowed), and only the header sheet itself is required (the other template worksheets become optional). **Bamawl Team uses the lenient mode** — it accepts any upload whose `ALL_Detail` sheet has at least **ID, Requirements, Function, Development man-hours (h)**. Every other registered team keeps the strict exact-match check; teams without a registered spec skip validation entirely and use the generic, lenient parser.

**Runs without `simple_resource/`.** `simple_resource/` holds the real customer workbooks and is **git-ignored** — it is absent on a fresh deploy, and the app no longer depends on it at runtime. Each specially-supported team's official template is now the **git-tracked** `import/<team>/<team>_import_template.xlsx`, used for Template Download, import validation, **and** as the export base (copy → populate → save). These sample files share the real workbooks' column structure (sanitization only blanks data), so validation, fixed-phase resolution, and export all work on a clean checkout.

| Team | Official template (download + import validation + export base) | Knowledge worksheet |
|---|---|---|
| Bamawl Team | `import/bamawl/bamawl_import_template.xlsx` | `ALL_Detail` |
| KiKan Team | `import/kikan/kikan_import_template.xlsx` | `工数詳細` |
| SGL Team | `import/sgl/sgl_import_template.xlsx` | `詳細見積_マスタと予実比較` |
| SSD Team | `import/ssd/ssd_import_template.xlsx` | `詳細設計～システムテスト 本番移行` |

### Bamawl Team

- Official template (download + import validation + export base): the git-tracked `import/bamawl/bamawl_import_template.xlsx`. (The old `simple_resource/bamawl_import_export_format_filled.xlsx` is no longer used at runtime; it also predated the `Requirements` column.)
- Knowledge worksheet: `ALL_Detail` (real header on row 4) — read by the generic `excel_parser.py` using a DB-seeded `column_mapping` (phases mode), seeded via `utils/migrations/bamawl_import_export_config.py`. Its **Requirements** column is mapped as the task **Category** (`category_column`), so each Requirement becomes a Category above its tasks (blank cells forward-fill). Import is **lenient** (see Template validation above).
- Per-phase auto-calculation: every phase is derived from a single **Development** man-hours input via ratio coefficients (`_BAMAWL_PHASE_FORMULA` in `routes/preview.py`, exposed to the Preview as `PHASE_FORMULA`); the Preview shows only Development as editable and a Percentage (%) panel for the ratios.
- Export: `services/bamawl_export_builder.py::BamawlExportBuilder` copies the template and populates `ALL_Detail`, writing Development as a literal and re-injecting each derived phase's + `Total(h)`'s original formula per row (`Translator`), so the workbook stays live in Excel. Edited percentages from the Preview (`phaseCoefficients` → `ExportContext.phase_coefficients`) are written into the template's coefficient row (row 2). Phase columns whose effective percentage is 0 are **hidden (not deleted)** so the `SUM`/`TotalManhour` formulas keep referencing an intact range; the task's Category is written back into the Requirements column. Only dispatched by `routes/export.py::_select_export_strategy` when a non-empty DB-seeded mapping exists; otherwise falls back to `DefaultExportStrategy`.

### KiKan Team

- Official template (single file, used for download, import validation, and export base): `import/kikan/kikan_import_template.xlsx`.
- Knowledge worksheet: `工数詳細` — read by the generic `excel_parser.py` using a DB-seeded `column_mapping`, seeded via `utils/migrations/kikan_import_export_config.py`. A thin dedicated parser (`services/kikan_import_parser.py`) additionally enriches tasks from the `機能一覧` cross-reference worksheet.
- Per-phase auto-calculation: like Bamawl, every phase is derived from a single **Development** (実装工数) input via ratio coefficients (`_KIKAN_PHASE_FORMULA` in `routes/preview.py` → `PHASE_FORMULA`), with the same editable Percentage (%) panel.
- Export: `services/kikan_export_builder.py::KikanExportBuilder` copies the template and populates `工数詳細` (kept in sync with `機能一覧`), writing Development as a literal and re-injecting the template's derived-phase/total formulas per row (`Translator`) so the workbook recomputes live in Excel. Same dispatch-gating rule as Bamawl (only used when a DB-seeded mapping exists).
- **Known limitation:** the shipped `import/kikan/kikan_import_template.xlsx` has a pre-existing defect unrelated to any team's import/export logic — one of its columns depends on a formula (`VLOOKUP`) whose *cached* value was lost the last time the file was re-saved via `openpyxl`. Since `excel_parser.py` reads cell values (not live-recalculated formulas), importing this exact shipped file currently parses zero tasks until the workbook is re-opened and re-saved in Excel itself (which recalculates and re-caches formulas). This is a data/tooling issue with the shipped sample file, not application code.

### SGL Team

- Official template (download + import validation + export base): the git-tracked `import/sgl/sgl_import_template.xlsx` (never modified by import or export — always copied first; shares the real workbook's column structure, so `fixed_phase_labels` and export work on a clean checkout without `simple_resource/`). Its unedited-source `simple_resource/sgl_import_export_format.xlsx` is git-ignored and no longer read at runtime.
- SGL's export deliberately writes **literal values** (not the live, row-translated formulas Bamawl/KiKan keep).
- Knowledge worksheet: `詳細見積_マスタと予実比較` (the workbook's other sheet, `見積・金額サマリ`, is a summary/amount rollup and is never read for knowledge import).
- SGL's worksheet layout is structurally different from Bamawl/KiKan's — a header split across two rows (row 2: field names + a merged "工数（人時間）" group label; row 3: six phase sub-labels — 要件定義/設計/開発/テスト/クラウド対応/その他 — underneath it) and task rows scattered across several blocks rather than one flat appendable range. Because of this, SGL has its own dedicated parser and builder instead of using the shared config-driven pipeline:
  - **Import:** `services/sgl_import_parser.py::sgl_excel_to_nested_json` reads the two-row header and phase columns directly from the template (never hardcoded), forward-fills 区分 (category) down blank rows, and treats a row as a real task only if 項目 (task name) is non-blank and at least one phase column is > 0.
  - **Export:** `services/sgl_export_builder.py::SglExportBuilder` copies the real internal template, discovers writable task rows dynamically from the template's own subtotal/per-row `SUM` formulas (rather than hardcoded row numbers), clears every writable row first, then writes each selected task into the discovered rows in order. Also replaces `見積・金額サマリ!A1`'s sample project title with the export's actual Project Name. Raises `SglExportError` if the selection exceeds the template's writable-row capacity.
  - Dispatched by `routes/export.py::_select_export_strategy` **unconditionally** (no DB-seeded-mapping gate, unlike Bamawl/KiKan) — SGL has no `column_mapping`; its structure is derived from the template itself every time.

### SSD Team

- Official template (download + import validation + export base): the git-tracked `import/ssd/ssd_import_template.xlsx`. The unedited-source workbook under `simple_resource/` is git-ignored and no longer read at runtime.
- Knowledge worksheet: `詳細設計～システムテスト 本番移行`, whose header spans three rows with grouped columns — read by SSD's own dedicated parser (`services/ssd_import_parser.py`, registered in `CUSTOM_IMPORT_PARSERS`), not the generic engine. Each task's four fixed phases (詳細設計 / 実装 / 単体テスト / 結合テスト) each carry a 標準 (standard) / 調整 (adjustment) / 見積 (estimate) breakdown (person-days), preserved through search/Preview via the parser's generic extra-field passthrough.
- Export: `services/ssd_export_builder.py::SsdExportBuilder` copies the template and writes the standard/adjustment/estimate hour groups as **literal values** — its template has `標準作業工数` VLOOKUP-driven special rows whose values must be fixed at export time rather than left to recalculate — while the template's own `見積工数 = 標準 + 調整` totals still recompute. Dispatched unconditionally (like SGL, no DB-mapping gate).

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
- Bamawl, KiKan, SGL, and SSD Team dedicated Excel import/export (each via its own registry-dispatched parser/builder), per-team Template Download, per-team template structural validation (strict exact-match, or lenient `required_columns` for Bamawl).
- Per-phase auto-calculation from a single Development input, with an editable Percentage (%) panel, for Bamawl and KiKan; their exports keep the template's live Excel formulas (recompute when Development changes).
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
| `import/{bamawl,kikan,sgl,ssd}/` | Each specially-supported team's git-tracked official template — used for Template Download, import validation, **and** as the export base — plus (where applicable) the one-off sanitization script (`build_sample_template.py`) that generated it from the real internal file |
| `simple_resource/` | **Git-ignored** (holds real customer workbooks) and **absent on a fresh deploy** — the app no longer reads it at runtime. Each team's export base is now the git-tracked `import/<team>/…` template instead. Kept only as the source input for the `build_sample_template.py` sanitization scripts where it happens to be present |
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