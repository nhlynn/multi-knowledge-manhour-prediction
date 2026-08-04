"""Bamawl Team's own Export Builder.

Unlike the generic export path (``services/export_workbook_service.py``,
which builds a fresh workbook from scratch via a column-layout config),
Bamawl Team's export is built directly on top of its own real Excel
template file (``simple_resource/bamawl_import_export_format.xlsx``):

- The template workbook is loaded as-is and saved back out — every
  worksheet (``ReqDefinition``, ``FunctionList``, ``TotalManhour``,
  ``ALL_Detail``, ``Infra Manhour``, ``Business Flow(system admin)``,
  ``Milestone``) is preserved, under its original name, with its
  original formatting (fonts, borders, merges, column widths) untouched
  except for the specific cells this module writes into.
- Only the ``ALL_Detail`` worksheet's data rows are populated, from
  MHES's internal Category → Task → Activity data (the same
  ``categories`` structure the Preview page sends to
  ``routes/export.py::export_excel``) — reusing Bamawl Team's
  configured ``column_mapping`` (phases mode; see
  ``utils/migrations/bamawl_import_export_config.py``) to know which
  column each phase (Development, Code Review, ...) belongs in, the
  export-side mirror of how ``services/bamawl_import_parser.py`` reads
  that same sheet on the way in.

**Known limitation** (a direct consequence of reusing this specific
template file rather than building a fresh one): ``ALL_Detail`` has its
own internal subtotal formulas (e.g. ``=SUM(D5:D14)``) immediately below
the task rows, and ``TotalManhour`` references ``ALL_Detail`` by a fixed
row number (e.g. ``=ALL_Detail!AD15``) -- both calibrated to the
template's own built-in row range. This module writes into the existing
task-row block only (never touching or shifting the subtotal
rows/formulas below it) and raises ``BamawlExportError`` rather than
overflow into them if a project has more tasks than that block holds.
Within capacity, per-task numbers are always correct; the *subtotal*
rows/‐other sheets' formulas were not re-derived for an arbitrary task
count and may not exactly match if the count differs from the
template's own built-in sample.
"""

import logging
import os
from typing import Any

import openpyxl

from services.excel_parser import _find_column, _normalize_header, _safe_float

logger = logging.getLogger(__name__)


class BamawlExportError(ValueError):
    """Raised when Bamawl Team's export template can't be built from,
    or the system data doesn't fit it -- see ``build_bamawl_workbook``."""


def _resolve_template_columns(ws, header_row: int) -> dict[str, int]:
    """Map each header cell's column name -- reproducing the same
    duplicate-suffixing (``"Review(h)"``/``"Review(h).1"``) and
    whitespace-stripping ``services/excel_parser.py``'s import path
    applies when it reads this same sheet via pandas -- to its real
    1-indexed column position in the template, so ``column_mapping``'s
    configured names (already written in that pandas-equivalent form)
    resolve back to the actual cells to write into.
    """
    seen: dict[str, int] = {}
    name_to_col: dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        raw = ws.cell(row=header_row, column=c).value
        raw_text = "" if raw is None else str(raw)
        n = seen.get(raw_text, 0)
        seen[raw_text] = n + 1
        pandas_name = raw_text if n == 0 else f"{raw_text}.{n}"
        name_to_col[pandas_name.strip()] = c
    return name_to_col


def _column_index(name_to_col: dict[str, int], target: str | None) -> int | None:
    """Resolve a configured column name to its column index, tolerant
    of whitespace/case the same way the import side is."""
    if not target:
        return None
    matched = _find_column(list(name_to_col.keys()), target)
    return name_to_col.get(matched) if matched else None


def _template_capacity(
    ws, data_start_row: int, task_col: int, phase_cols: list[tuple[str, int]],
) -> int:
    """Return how many task rows are available below the header before
    the template's own subtotal/summary block starts.

    Scans down from ``data_start_row``: a row with a real task name is
    counted; a row with a blank task name is still counted (a blank
    separator row within the sample block) *unless* it already has a
    value in one of the phase columns -- that's the signature of a
    subtotal row (e.g. ``=SUM(D5:D14)``), which marks the boundary.
    """
    row = data_start_row
    while row <= ws.max_row:
        task_val = ws.cell(row=row, column=task_col).value
        if not task_val:
            phase_has_value = any(
                ws.cell(row=row, column=col_idx).value not in (None, "")
                for _, col_idx in phase_cols
            )
            if phase_has_value:
                break
        row += 1
    return row - data_start_row


def _phase_value(activities: list[dict[str, Any]], label: str) -> float:
    """Return the estimate_hours of the activity matching ``label``
    (whitespace/case-insensitive), or 0.0 if this task has none."""
    target = _normalize_header(label)
    for act in activities:
        if _normalize_header(act.get("task_detail") or "") == target:
            return _safe_float(act.get("estimate_hours"))
    return 0.0


def build_bamawl_workbook(
    filepath: str,
    categories: list[dict[str, Any]],
    column_mapping: dict[str, Any],
    template_path: str,
) -> None:
    """Populate Bamawl Team's own Excel template with system data and
    save it to ``filepath``.

    Args:
        filepath: Where to save the populated workbook.
        categories: The exported Category → Task → Activity data (same
            shape ``services/export_workbook_service.py`` receives).
            Every task across every category is written, in order, as
            one ``ALL_Detail`` row -- Bamawl Team's sheet has no
            category column of its own (see
            ``BAMAWL_IMPORT_COLUMN_MAPPING``'s fixed literal
            ``category``), so category boundaries aren't represented.
        column_mapping: Bamawl Team's configured phases-mode column
            mapping (``sheet``, ``header_row``, ``task_column``,
            ``id_column``, ``phase_columns``, ``total_column``).
        template_path: Path to Bamawl Team's real template workbook
            (``simple_resource/bamawl_import_export_format.xlsx``).

    Raises:
        BamawlExportError: if the template file/worksheet/columns
            can't be found, or the project has more tasks than the
            template's task-row block can hold without touching its
            subtotal rows (see module docstring).
    """
    if not os.path.isfile(template_path):
        raise BamawlExportError(f"Bamawl Team's export template file is missing: {template_path}")

    sheet_name = column_mapping.get("sheet")
    header_row = column_mapping.get("header_row") or 1

    wb = openpyxl.load_workbook(template_path)

    if sheet_name not in wb.sheetnames:
        raise BamawlExportError(
            f"Bamawl Team's export template is missing the required '{sheet_name}' worksheet."
        )
    ws = wb[sheet_name]

    name_to_col = _resolve_template_columns(ws, header_row)
    task_col = _column_index(name_to_col, column_mapping.get("task_column"))
    if task_col is None:
        raise BamawlExportError(
            "Bamawl Team's export template's task column could not be located; "
            "cannot populate ALL_Detail."
        )
    id_col = _column_index(name_to_col, column_mapping.get("id_column"))
    total_col = _column_index(name_to_col, column_mapping.get("total_column"))
    phase_cols = [
        (phase["label"], _column_index(name_to_col, phase["column"]))
        for phase in column_mapping.get("phase_columns", [])
    ]
    phase_cols = [(label, idx) for label, idx in phase_cols if idx is not None]

    tasks = [task for cat in categories for task in cat.get("tasks", [])]

    data_start_row = header_row + 1
    capacity = _template_capacity(ws, data_start_row, task_col, phase_cols)
    if len(tasks) > capacity:
        raise BamawlExportError(
            f"This project has {len(tasks)} task(s), but Bamawl Team's export template's "
            f"'{sheet_name}' worksheet only has room for {capacity} before its built-in "
            f"subtotal rows -- reduce the number of tasks, or update the template."
        )

    # Clear the template's own sample rows across the whole task-row
    # block first, so no leftover sample values (or their formulas)
    # linger past however many real rows are written below.
    for r in range(data_start_row, data_start_row + capacity):
        for c in range(1, ws.max_column + 1):
            ws.cell(row=r, column=c).value = None

    unmatched_labels: set[str] = set()
    row = data_start_row
    for i, task in enumerate(tasks, start=1):
        activities = task.get("activities", []) or []
        configured_labels = {_normalize_header(label) for label, _ in phase_cols}
        for act in activities:
            norm = _normalize_header(act.get("task_detail") or "")
            if norm and norm not in configured_labels:
                unmatched_labels.add(act.get("task_detail"))

        if id_col:
            ws.cell(row=row, column=id_col, value=i)
        ws.cell(row=row, column=task_col, value=task.get("task", ""))

        row_total = 0.0
        for label, col_idx in phase_cols:
            value = _phase_value(activities, label)
            if value:
                ws.cell(row=row, column=col_idx, value=value)
                row_total += value

        if total_col:
            ws.cell(row=row, column=total_col, value=row_total)

        row += 1

    if unmatched_labels:
        logger.warning(
            "Bamawl export: %d activity label(s) didn't match any configured phase "
            "column and were left out of '%s': %s",
            len(unmatched_labels), sheet_name, sorted(unmatched_labels),
        )

    wb.save(filepath)
    logger.info(
        "Built Bamawl Team export workbook: %s (%d task row(s) written into '%s')",
        filepath, len(tasks), sheet_name,
    )
