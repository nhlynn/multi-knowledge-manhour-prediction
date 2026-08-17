"""Excel-to-nested-JSON converter for man-hour knowledge files.

Parses hierarchical Excel files with the structure:
    Category | Task List | Activity Details | Estimate (Hours) | Buffer (Hours)

Produces a nested JSON structure with rich ``text`` fields optimised
for semantic search, matching the format in ``simple_resource/``.

Team-specific column mapping (Phase 7 of multi-team support): different
teams may label these same five roles with completely different
headers (e.g. Development Team's "Feature"/"Technology"/"Hours" instead
of "Task List"/"Category"/"Estimate (Hours)"). Rather than a separate
parser per team, ``excel_to_nested_json``/``_map_columns`` accept an
optional ``column_mapping`` (role -> that team's actual header name,
looked up via ``repositories/team_import_config_repository.py``). Any
role the mapping doesn't cover still falls back to the original generic
keyword matching below, and passing no mapping at all reproduces the
exact pre-Phase-7 behavior — so existing teams/files are unaffected.

Phases mode (later addition): some teams' real-world files break one
task's hours down across many phase columns (e.g. "Development (h)",
"Code Review (h)", "QA (h)", ... ending in a "Total (h)" column), with
the real header row sometimes several rows down from row 1 (a
percentage/phase-group block sits above it). Collapsing each row to
just its ``Total(h)`` would silently discard every phase breakdown —
exactly the historical detail a future estimate most needs to reuse. So
when ``column_mapping`` contains a ``phase_columns`` list, each phase
column becomes its own Activity Detail under the task (not one Activity
holding a single total) — see ``_process_phases_sheet``. A
``column_mapping`` without ``phase_columns`` runs the original flat
category/task/detail/estimate logic (``_process_flat_sheet``), unchanged.
"""

import logging
import re
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    ``\\w`` is Unicode-aware by default for ``str`` patterns in Python 3
    (matches any script's letters/digits, not just ASCII a-z0-9) --
    required so non-Latin task/category names (e.g. KiKan Team's
    Japanese function names) don't all collapse to the same empty
    slug, silently merging distinct tasks together. Every existing
    ASCII input (every team's data before KiKan) slugifies identically
    to before: ``\\w`` still matches exactly a-z0-9 plus underscore for
    ASCII text, and no team's real category/task names contain a
    literal underscore, so this is behavior-preserving for them.
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s]+", "-", text)
    return text


def _safe_float(val: Any) -> float:
    """Convert a value to float, defaulting to 0.0 for NaN/None."""
    try:
        if pd.isna(val):
            return 0.0
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def excel_to_nested_json(
    excel_path: str, column_mapping: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert a man-hour Excel file into nested JSON.

    Handles merged cells by forward-filling Category and Task List columns.
    Builds a 3-level hierarchy: Category → Task → Activity.

    Each level has a ``text`` field with full context for embedding.

    Args:
        excel_path: Path to the Excel file.
        column_mapping: Optional per-team mapping. None uses the
            original generic keyword-based detection for every role
            (Category/Task/Detail/Estimate/Buffer), unchanged from
            before Phase 7. Two shapes are recognized:

            - Flat (Phase 7): ``{"category": ..., "task": ..., "detail":
              ..., "estimate": ..., "buffer": ...}`` — role name -> that
              team's header name. See ``_map_columns``.
            - Phases (later addition): ``{"sheet": ..., "header_row":
              ..., "task_column": ..., "category_column": ... /
              "category": <fixed text>, "phase_columns": [{"label":
              ..., "column": ...}, ...], "total_column": ...}`` — each
              phase column becomes its own Activity Detail under the
              task, instead of collapsing the row to a single total.
              See ``_process_phases_sheet``.

    Returns:
        List of category-level dictionaries (one per category).
    """
    sheet_dict = _read_sheets(excel_path, column_mapping)
    phases_mode = bool(column_mapping and column_mapping.get("phase_columns"))

    all_categories: dict[str, dict[str, Any]] = {}

    for sheet_name, df in sheet_dict.items():
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")
        if df.empty:
            continue

        if phases_mode:
            _process_phases_sheet(df, column_mapping, all_categories, sheet_name)
        else:
            _process_flat_sheet(df, column_mapping, all_categories, sheet_name)

    result = _build_nested_output(all_categories)
    _log_conversion_summary(excel_path, result)
    return result


def _read_sheets(
    excel_path: str, column_mapping: dict[str, Any] | None,
) -> dict[str, "pd.DataFrame"]:
    """Read the workbook's sheet(s) as DataFrames, honoring an optional
    per-team ``sheet``/``header_row`` override in ``column_mapping``.

    Omitting both reproduces the original pre-Phase-9 behavior: every
    sheet is read, with row 1 as the header.
    """
    sheet_name_filter = column_mapping.get("sheet") if column_mapping else None
    header_row = column_mapping.get("header_row") if column_mapping else None
    header_arg = (header_row - 1) if header_row else 0

    if not sheet_name_filter:
        return pd.read_excel(excel_path, sheet_name=None, header=header_arg, engine="openpyxl")

    try:
        return {
            sheet_name_filter: pd.read_excel(
                excel_path, sheet_name=sheet_name_filter, header=header_arg, engine="openpyxl",
            )
        }
    except ValueError:
        logger.warning(
            "Configured sheet %r not found in %s; nothing to import.",
            sheet_name_filter, excel_path,
        )
        return {}


def _log_conversion_summary(excel_path: str, result: list[dict[str, Any]]) -> None:
    """Log how many categories/text chunks a conversion produced."""
    total_texts = sum(
        1  # category text
        + len(cat.get("tasks", []))  # task texts
        + sum(len(t.get("task_details", [])) for t in cat.get("tasks", []))
        for cat in result
    )
    logger.info(
        f"Converted Excel to nested JSON ({excel_path}): "
        f"{len(result)} categories, {total_texts} text chunks"
    )


def _process_flat_sheet(
    df: "pd.DataFrame",
    column_mapping: dict[str, Any] | None,
    all_categories: dict[str, dict[str, Any]],
    sheet_name: str,
) -> None:
    """Populate ``all_categories`` from one flat category/task/detail/estimate
    sheet — the original (pre-"phases mode") one-Activity-per-row logic.

    Args:
        df: The sheet's DataFrame (columns already stripped, blank rows
            already dropped by the caller).
        column_mapping: Optional per-team role -> header-name mapping,
            resolved here via ``_map_columns``.
        all_categories: Accumulator, mutated in place (same shape
            ``_process_phases_sheet`` builds).
        sheet_name: For log messages only.
    """
    col_map = _map_columns(df.columns.tolist(), column_mapping)
    if not col_map:
        logger.warning(
            f"Sheet '{sheet_name}': could not map columns "
            f"{list(df.columns)}, skipping"
        )
        return

    cat_col = col_map["category"]
    task_col = col_map["task"]
    detail_col = col_map["detail"]
    est_col = col_map["estimate"]
    buf_col = col_map.get("buffer")

    # Forward-fill Category and Task List for merged cells
    df[cat_col] = df[cat_col].ffill()
    df[task_col] = df[task_col].ffill()

    for _, row in df.iterrows():
        _process_flat_row(row, cat_col, task_col, detail_col, est_col, buf_col, all_categories)


def _process_flat_row(
    row: "pd.Series",
    cat_col: str,
    task_col: str,
    detail_col: str,
    est_col: str,
    buf_col: str | None,
    all_categories: dict[str, dict[str, Any]],
) -> None:
    """Fold one flat-mode Excel row into ``all_categories`` as one Activity
    under its Category/Task (creating either as needed).
    """
    category = str(row[cat_col]).strip() if pd.notna(row[cat_col]) else ""
    task = str(row[task_col]).strip() if pd.notna(row[task_col]) else ""
    detail = str(row[detail_col]).strip() if pd.notna(row[detail_col]) else ""
    estimate = _safe_float(row[est_col])
    buffer_hrs = _safe_float(row[buf_col]) if buf_col else 0.0

    if not category or not detail:
        return

    cat_slug = _slugify(category)
    task_slug = _slugify(task) if task else "general"

    if cat_slug not in all_categories:
        all_categories[cat_slug] = {"category": category, "tasks": {}}
    cat_data = all_categories[cat_slug]

    task_key = f"{cat_slug}_{task_slug}"
    if task_key not in cat_data["tasks"]:
        cat_data["tasks"][task_key] = {
            "task": task,
            "buffer_hours": buffer_hrs,
            "activities": [],
        }
    task_data = cat_data["tasks"][task_key]

    # If this row has buffer, it's the task-level buffer
    if buffer_hrs > 0:
        task_data["buffer_hours"] = buffer_hrs

    activity_slug = _slugify(detail)
    activity_id = f"{cat_slug}_{task_slug}_{activity_slug}"

    task_data["activities"].append({
        "id": activity_id,
        "task_detail": detail,
        "estimate_hours": estimate,
    })


def _generic_role_matches(columns: list[str]) -> dict[str, str]:
    """Best-effort keyword-based column detection (the original,
    pre-Phase-7 heuristic). Used as the whole-sheet fallback for teams
    with no import configuration, and per-role for any role a
    configured mapping doesn't cover.

    Column order matters here exactly as it did before Phase 7: when
    multiple columns match the same role's keywords, the last one
    (in the sheet's left-to-right order) wins, unchanged from the
    original single-pass loop this replaces.
    """
    col_lower = {c.lower(): c for c in columns}
    matches: dict[str, str] = {}

    for key, original in col_lower.items():
        if "category" in key or "project" in key:
            matches["category"] = original
        elif "task" in key and "detail" not in key:
            matches["task"] = original
        elif "detail" in key or "activity" in key:
            matches["detail"] = original
        elif "estimate" in key or "hour" in key and "buffer" not in key:
            matches["estimate"] = original
        elif "buffer" in key:
            matches["buffer"] = original

    return matches


def _map_columns(
    columns: list[str], column_mapping: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Map Excel column names to expected MHES roles.

    Without a team-specific ``column_mapping`` (Phase 7), this uses only
    the original flexible keyword matching (``_generic_role_matches``)
    for every role — identical behavior to every KB file parsed before
    this phase, so teams with no import configuration are completely
    unaffected.

    With a ``column_mapping`` (one row per team in the
    ``team_import_configs`` table — see
    ``repositories/team_import_config_repository.py``), each role it
    lists is resolved by an exact, case-insensitive header match against
    this sheet's actual columns. Any role the mapping *doesn't* cover
    (or whose configured header isn't actually present in this sheet)
    still falls back to the same generic keyword matching as the
    no-config case — a team only needs to configure the roles that
    genuinely differ from the generic vocabulary (e.g. Development
    Team's ``{"category": "Technology", "task": "Feature", "detail":
    "Feature", "estimate": "Hours"}`` — mapping both ``task`` and
    ``detail`` onto the same column, since that team's format has no
    separate per-activity breakdown).

    Args:
        columns: Column headers from one sheet, in original order.
        column_mapping: Optional per-team role -> header-name overrides.

    Returns:
        Dict mapping role names to actual column names, or None if
        category/task/detail/estimate could not all be resolved.
    """
    resolved = _generic_role_matches(columns)

    if column_mapping:
        col_by_lower = {c.strip().lower(): c for c in columns}
        for role, configured_name in column_mapping.items():
            if not configured_name:
                continue
            actual = col_by_lower.get(configured_name.strip().lower())
            if actual is None:
                logger.warning(
                    "Configured column %r for role %r not found in sheet "
                    "columns %r; falling back to generic detection for "
                    "this role.", configured_name, role, columns,
                )
                continue
            resolved[role] = actual

    if not all(resolved.get(r) for r in ("category", "task", "detail", "estimate")):
        return None

    result = {
        "category": resolved["category"],
        "task": resolved["task"],
        "detail": resolved["detail"],
        "estimate": resolved["estimate"],
    }
    if resolved.get("buffer"):
        result["buffer"] = resolved["buffer"]
    return result


def _normalize_header(text: Any) -> str:
    """Collapse a header's internal whitespace/newlines to single spaces,
    lowercased, for tolerant matching. Real-world workbook headers are
    often wrapped across lines (e.g. ``"\\nDevelopment man-hours (h)\\n"``)
    purely for column-width reasons, with no semantic difference from
    ``"Development man-hours (h)"``.
    """
    return " ".join(str(text).split()).strip().lower()


def _find_column(columns: list[str], target: str | None) -> str | None:
    """Return the actual column name matching ``target`` (whitespace/case
    insensitive), or None if not found."""
    if not target:
        return None
    target_norm = _normalize_header(target)
    for c in columns:
        if _normalize_header(c) == target_norm:
            return c
    return None


def _process_phases_sheet(
    df: "pd.DataFrame",
    config: dict[str, Any],
    all_categories: dict[str, dict[str, Any]],
    sheet_name: str,
) -> None:
    """Populate ``all_categories`` from one "phases mode" sheet.

    Unlike the flat category/task/detail/estimate mode, each row here
    produces *one Activity Detail per configured phase column* under a
    single Task — so a row with a "Development (h)", "Code Review (h)",
    "QA (h)" ... breakdown keeps every one of those as its own,
    individually searchable/reusable activity, instead of collapsing
    them into one activity holding only the row's grand total.

    Args:
        df: The sheet's DataFrame (already header-resolved by the
            caller via ``header_row``/``sheet`` in ``column_mapping``).
        config: The team's ``column_mapping``, expected to contain
            ``task_column``, ``phase_columns`` (list of
            ``{"label": ..., "column": ...}``), and either
            ``category_column`` (a column to read/forward-fill per row)
            or ``category`` (a fixed literal category name applied to
            every row). Optional ``total_column`` is used only as a
            sanity cross-check against the sum of matched phase columns
            — never stored, since the per-phase activities already carry
            the real breakdown. Optional ``id_column``: a column that
            must hold a real number for a row to count as a task row —
            checked *before* ``task_column``'s forward-fill, so a
            trailing summary/rollup block (blank or label-text id, but
            numeric phase values, sometimes even its own non-blank
            "task name") doesn't get silently folded into the last real
            task above it. Rows are only skipped by this check if
            ``id_column`` is configured — omitting it preserves the
            exact prior behavior. Optional ``extra_columns``: a list of
            ``{"field": ..., "column": ...}`` entries for any other
            same-sheet column a team's task shape needs verbatim (e.g.
            KiKan's own ``Status``/``機能ID`` columns) — each resolved
            column's value is set on the task's ``field`` key, passed
            through generically all the way to the final JSON/search
            output by ``_build_task_output``'s own generic-field
            passthrough (see its ``_KNOWN_TASK_DATA_FIELDS``) — no
            further plumbing needed here for a team to gain a new
            simple, same-row, same-sheet field this way. This is NOT
            for anything requiring cross-row accumulation (like SGL's
            own multi-row ``work_detail``) or a second worksheet (like
            KiKan's own ``機能一覧`` cross-reference, still handled by
            ``services/kikan_import_parser.py``'s own thin
            enrichment wrapper) — both of those still need dedicated
            handling beyond this generic mechanism.
        all_categories: Accumulator, mutated in place (same shape
            ``_process_flat_sheet`` builds).
        sheet_name: For log messages only.
    """
    columns = df.columns.tolist()

    task_col = _find_column(columns, config.get("task_column"))
    if not task_col:
        logger.warning(
            "Sheet '%s': task_column %r not found in columns %r; skipping sheet.",
            sheet_name, config.get("task_column"), columns,
        )
        return

    category_col = _find_column(columns, config.get("category_column"))
    fixed_category = config.get("category") if not category_col else None
    if not category_col and not fixed_category:
        logger.warning(
            "Sheet '%s': no category_column found and no fixed 'category' "
            "configured; skipping sheet.", sheet_name,
        )
        return

    phase_columns = _resolve_phase_columns(columns, config, sheet_name)
    if not phase_columns:
        logger.warning("Sheet '%s': no phase columns resolved; skipping sheet.", sheet_name)
        return

    total_col = _find_column(columns, config.get("total_column"))
    id_col = _find_column(columns, config.get("id_column"))
    extra_columns = [
        (extra["field"], _find_column(columns, extra["column"]))
        for extra in config.get("extra_columns", [])
    ]
    extra_columns = [(field, col) for field, col in extra_columns if col]

    if category_col:
        df[category_col] = df[category_col].ffill()
    df[task_col] = df[task_col].ffill()

    for _, row in df.iterrows():
        _process_phases_row(
            row, task_col, category_col, fixed_category, phase_columns, total_col,
            config, all_categories, sheet_name, id_col=id_col, extra_columns=extra_columns,
        )


def _resolve_phase_columns(
    columns: list[str], config: dict[str, Any], sheet_name: str,
) -> list[tuple[str, str]]:
    """Resolve each configured ``{"label": ..., "column": ...}`` phase
    entry to an actual column present in this sheet, logging (and
    skipping) any that aren't found.
    """
    phase_columns: list[tuple[str, str]] = []
    for phase in config.get("phase_columns", []):
        actual = _find_column(columns, phase.get("column"))
        if actual:
            phase_columns.append((phase["label"], actual))
        else:
            logger.warning(
                "Sheet '%s': configured phase column %r (label %r) not "
                "found in columns %r; that phase will be skipped for "
                "every row.", sheet_name, phase.get("column"), phase.get("label"), columns,
            )
    return phase_columns


def _process_phases_row(
    row: "pd.Series",
    task_col: str,
    category_col: str | None,
    fixed_category: str | None,
    phase_columns: list[tuple[str, str]],
    total_col: str | None,
    config: dict[str, Any],
    all_categories: dict[str, dict[str, Any]],
    sheet_name: str,
    *,
    id_col: str | None = None,
    extra_columns: list[tuple[str, str]] | None = None,
) -> None:
    """Fold one phases-mode Excel row into ``all_categories`` — every
    phase column with a nonzero value becomes its own Activity Detail
    under one Task.
    """
    if id_col is not None:
        id_val = row[id_col]
        is_valid_id = not pd.isna(id_val)
        if is_valid_id:
            try:
                float(id_val)
            except (TypeError, ValueError):
                is_valid_id = False
        if not is_valid_id:
            # A trailing summary/rollup block (e.g. a per-role subtotal
            # table below the real task list) has a blank or non-numeric
            # (label text) id but often still carries numeric
            # phase-column values, sometimes even its own non-blank
            # "task name" text -- checked before task_col's
            # forward-fill below so neither is mistaken for a real task
            # row. Only enforced when id_column is configured (see
            # _process_phases_sheet).
            return

    task = str(row[task_col]).strip() if pd.notna(row[task_col]) else ""
    if not task:
        # Rows with no task name are typically group-rollup/summary
        # rows in these workbooks (their totals are already implied
        # by summing the detail rows below them) — skip, not an error.
        return

    if category_col:
        category = str(row[category_col]).strip() if pd.notna(row[category_col]) else ""
        if not category:
            return
    else:
        category = fixed_category

    row_activities = [
        (label, _safe_float(row[col]))
        for label, col in phase_columns
        if _safe_float(row[col]) > 0
    ]
    if not row_activities:
        # A named task with every phase at 0/blank — nothing to import.
        return

    cat_slug = _slugify(category)
    task_slug = _slugify(task)

    if cat_slug not in all_categories:
        all_categories[cat_slug] = {"category": category, "tasks": {}}
    cat_data = all_categories[cat_slug]

    task_key = f"{cat_slug}_{task_slug}"
    if task_key not in cat_data["tasks"]:
        cat_data["tasks"][task_key] = {"task": task, "buffer_hours": 0.0, "activities": []}
    task_data = cat_data["tasks"][task_key]

    for label, hours in row_activities:
        activity_slug = _slugify(label)
        task_data["activities"].append({
            "id": f"{cat_slug}_{task_slug}_{activity_slug}",
            "task_detail": label,
            "estimate_hours": hours,
        })

    for field, col in extra_columns or []:
        val = row[col]
        if pd.notna(val) and str(val).strip():
            task_data.setdefault(field, str(val).strip())

    _warn_if_total_mismatch(row, total_col, row_activities, config, sheet_name, task)


def _warn_if_total_mismatch(
    row: "pd.Series",
    total_col: str | None,
    row_activities: list[tuple[str, float]],
    config: dict[str, Any],
    sheet_name: str,
    task: str,
) -> None:
    """Log a warning if a row's configured ``total_column`` disagrees
    with the sum of its resolved phase columns by more than 0.5h — a
    sanity cross-check only; the phase-column sum is always what's
    actually stored (see ``_process_phases_row``).
    """
    if not total_col:
        return
    total_val = _safe_float(row[total_col])
    computed = sum(h for _, h in row_activities)
    if total_val and abs(total_val - computed) > 0.5:
        logger.warning(
            "Sheet '%s' task '%s': phase columns sum to %.2fh but "
            "%r says %.2fh — using the phase-column sum.",
            sheet_name, task, computed, config.get("total_column"), total_val,
        )


def _build_nested_output(
    categories: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the final nested JSON with summary and text fields."""
    return [
        _build_category_output(cat_slug, cat_data)
        for cat_slug, cat_data in categories.items()
    ]


def _build_category_output(cat_slug: str, cat_data: dict[str, Any]) -> dict[str, Any]:
    """Build one category's full output record, including all its tasks."""
    category_name = cat_data["category"]
    tasks_output = [
        _build_task_output(cat_slug, category_name, task_key, task_data)
        for task_key, task_data in cat_data["tasks"].items()
    ]

    cat_total_estimate = sum(t["estimate_hours"] for t in tasks_output)
    cat_total_buffer = sum(t["buffer_hours"] for t in tasks_output)
    cat_grand_total = cat_total_estimate + cat_total_buffer

    return {
        "id": f"{cat_slug}_summary",
        "type": "category_summary",
        "category": category_name,
        "task_count": len(tasks_output),
        "total_estimate_hours": cat_total_estimate,
        "total_buffer_hours": cat_total_buffer,
        "grand_total_hours": cat_grand_total,
        "tasks": tasks_output,
        "text": _category_context_text(
            category_name, len(tasks_output), cat_total_estimate, cat_total_buffer, cat_grand_total,
        ),
    }


# The fixed set of keys _add_task_activities-style accumulators always
# populate on a task_data dict. Anything else a team-specific parser
# adds (e.g. SGL's own "work_detail") is passed through generically by
# _build_task_output below, with no per-field/per-team hardcoding here.
_KNOWN_TASK_DATA_FIELDS = {"task", "buffer_hours", "activities"}


def _build_task_output(
    cat_slug: str, category_name: str, task_key: str, task_data: dict[str, Any],
) -> dict[str, Any]:
    """Build one task's full output record, including all its activity
    details, plus any extra team-specific field already present on
    ``task_data`` (e.g. SGL's ``work_detail``) passed through as-is.
    """
    task_name = task_data["task"]
    task_buffer = task_data["buffer_hours"]
    activities = task_data["activities"]
    extra_fields = {k: v for k, v in task_data.items() if k not in _KNOWN_TASK_DATA_FIELDS}

    task_estimate = sum(a["estimate_hours"] for a in activities)
    task_total = task_estimate + task_buffer

    details_output = [
        _build_activity_output(category_name, task_name, task_buffer, act)
        for act in activities
    ]

    task_slug = _slugify(task_name)
    return {
        "id": f"{cat_slug}_{task_slug}_summary",
        "task": task_name,
        "estimate_hours": task_estimate,
        "buffer_hours": task_buffer,
        "total_hours": task_total,
        "task_details": details_output,
        "text": _task_context_text(
            category_name, task_name, len(activities), task_estimate, task_buffer, task_total,
        ),
        **extra_fields,
    }


# The fixed set of keys _build_activity_output itself populates. Any
# other field a team-specific parser adds to an activity (e.g. SSD's
# per-phase ``standard_hours``/``adjustment_hours`` breakdown) is passed
# through generically below, mirroring _build_task_output's own
# extra-field passthrough. Teams whose activities carry none of these
# are entirely unaffected.
_KNOWN_ACTIVITY_FIELDS = {
    "id", "task_detail", "estimate_hours", "buffer_scope",
    "buffer_note", "standalone_buffer_hours", "text",
}


def _build_activity_output(
    category_name: str, task_name: str, task_buffer: float, act: dict[str, Any],
) -> dict[str, Any]:
    """Build one activity detail's full output record, including its
    embedding-ready ``text`` and buffer-scope explanation, plus any
    extra team-specific field already present on ``act`` (e.g. SSD's
    ``standard_hours``/``adjustment_hours`` per-phase breakdown) passed
    through as-is.
    """
    extra_fields = {k: v for k, v in act.items() if k not in _KNOWN_ACTIVITY_FIELDS}
    return {
        "id": act["id"],
        "task_detail": act["task_detail"],
        "estimate_hours": act["estimate_hours"],
        "buffer_scope": "task-level",
        "buffer_note": _activity_buffer_note(task_name, task_buffer),
        "standalone_buffer_hours": 0.5,
        "text": _activity_context_text(category_name, task_name, task_buffer, act),
        **extra_fields,
    }


# ------------------------------------------------------------------
# Context generation — the natural-language "text" fields embedded and
# searched at each level. Kept separate from the record-assembly
# functions above so the wording that actually drives search quality
# can be read/changed in one place, without touching how the
# Category/Task/Activity records themselves are built.
# ------------------------------------------------------------------

def _category_context_text(
    category_name: str, task_count: int, total_estimate: float, total_buffer: float, grand_total: float,
) -> str:
    """Embedding text for one category's summary record."""
    return (
        f'{category_name} project overview: '
        f'{task_count} tasks, '
        f'total estimate {total_estimate}h, '
        f'total buffer {total_buffer}h, '
        f'grand total {grand_total}h including buffer.'
    )


def _task_context_text(
    category_name: str, task_name: str, activity_count: int,
    task_estimate: float, task_buffer: float, task_total: float,
) -> str:
    """Embedding text for one task's summary record."""
    return (
        f'{category_name} → {task_name}: '
        f'{activity_count} activities, '
        f'total estimate {task_estimate}h, '
        f'buffer {task_buffer}h, '
        f'grand total {task_total}h including buffer. '
        f'Buffer applies to the whole task, not to '
        f'individual activities.'
    )


def _activity_context_text(
    category_name: str, task_name: str, task_buffer: float, act: dict[str, Any],
) -> str:
    """Embedding text for one activity detail record."""
    return (
        f'{category_name} → {task_name} → {act["task_detail"]}. '
        f'Estimate: {act["estimate_hours"]}h. '
        f'If done as part of the "{task_name}" task, buffer is '
        f'not counted per-activity — the task has a {task_buffer}h '
        f'buffer total. If this activity is scoped and done '
        f'standalone on its own, use a fixed 0.5h buffer instead.'
    )


def _activity_buffer_note(task_name: str, task_buffer: float) -> str:
    """Human-readable explanation of how buffer applies to one activity
    (shown in the UI, not embedded — distinct from ``_activity_context_text``).
    """
    return (
        f'When this activity is done as PART of the task '
        f'"{task_name}", buffer is not counted per-activity '
        f'— use the task-level buffer ({task_buffer}h) instead. '
        f'When this activity is done STANDALONE (scoped and '
        f'delivered on its own, separate from the rest of the '
        f'task), use standalone_buffer_hours (fixed 0.5h) instead.'
    )


def extract_texts_from_nested(nested_json: list[dict[str, Any]]) -> list[str]:
    """Extract all ``text`` fields from nested JSON for embedding.

    Collects texts at all three levels: category, task, and activity.

    Args:
        nested_json: The nested JSON structure from ``excel_to_nested_json``.

    Returns:
        List of text strings ready for embedding.
    """
    texts: list[str] = []
    for category in nested_json:
        if category.get("text"):
            texts.append(category["text"])
        for task in category.get("tasks", []):
            if task.get("text"):
                texts.append(task["text"])
            for detail in task.get("task_details", []):
                if detail.get("text"):
                    texts.append(detail["text"])
    return texts