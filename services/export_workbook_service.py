"""Builds the downloadable Excel workbook for an exported estimate.

Moved out of ``routes/export.py`` so the route stays thin — this is the
export-side counterpart to ``services/excel_parser.py`` (which turns an
uploaded Knowledge Base workbook *into* structured data; this module
turns structured Preview data *into* a workbook). No behavior changed
from the original ``routes/export.py::_build_workbook``.
"""

import logging
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

DEFAULT_EXPORT_TEMPLATE = {
    "sheet_title": "Manhour",
    "columns": [
        {"key": "category", "label": "Category", "width": 25},
        {"key": "task", "label": "Task List", "width": 45},
        {"key": "estimate_hours", "label": "Estimate (Hours)", "width": 22},
        {"key": "working_day", "label": "Working Day", "width": 15},
        {"key": "remarks", "label": "Remarks", "width": 35},
    ],
}
"""The pre-Phase-8 column layout, reproduced exactly as data instead of
hardcoded Excel column letters — this is what every team without a
configured ``team_export_templates`` row gets, so existing exports are
byte-for-byte unaffected by Phase 8 (see docs/ARCHITECTURE.md §5h).

Recognized ``columns[].key`` values (each renders one data-table column;
unknown keys render as an empty column and are logged):
    category        -- category name, merged across that category's task rows
    task            -- numbered task name ("1. <task>")
    estimate_hours  -- the task's total hours
    working_day     -- formula: whichever "estimate_hours" column's value / 8
                       (blank if the template has no "estimate_hours" column)
    remarks         -- free-text remarks (task.get("remarks", ""))
"""


def _style_row(ws, row: int, num_cols: int, *, border=None, fill=None, font=None) -> None:
    """Apply the given border/fill/font (whichever aren't None) to every
    cell in ``row`` across ``num_cols`` columns.

    Replaces what would otherwise be several separate, near-identical
    ``for col_idx in range(1, num_cols + 1): ws.cell(...)...`` loops
    (e.g. the total row) with one helper — same cells styled with the
    same objects in the same order, just not duplicated per call site.
    """
    for col_idx in range(1, num_cols + 1):
        cell = ws.cell(row=row, column=col_idx)
        if border is not None:
            cell.border = border
        if fill is not None:
            cell.fill = fill
        if font is not None:
            cell.font = font


def build_workbook(
    filepath: str,
    project_name: str,
    created_by: str,
    categories: list,
    template_config: dict | None = None,
) -> None:
    """Build an Excel workbook using a team's configured column template.

    Args:
        template_config: Optional per-team template (Phase 8 — see
            ``DEFAULT_EXPORT_TEMPLATE`` above and
            ``repositories/team_export_template_repository.py``). None
            uses ``DEFAULT_EXPORT_TEMPLATE``, reproducing the exact
            pre-Phase-8 layout.

    The title/Created-By/Date metadata rows, per-category row merging,
    and totals row are shared structure — identical for every team
    regardless of template; only the data table's columns (which ones
    appear, their order, label, and width) are configurable.
    """
    template_config = template_config or DEFAULT_EXPORT_TEMPLATE
    columns = template_config.get("columns") or DEFAULT_EXPORT_TEMPLATE["columns"]
    sheet_title = template_config.get("sheet_title") or DEFAULT_EXPORT_TEMPLATE["sheet_title"]
    num_cols = len(columns)
    col_index_by_key = {col["key"]: i for i, col in enumerate(columns, 1)}
    category_col = col_index_by_key.get("category")
    estimate_col = col_index_by_key.get("estimate_hours")
    working_day_col = col_index_by_key.get("working_day")

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title

    # --- Styles ---
    title_font = Font(bold=True, size=14)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="333333", end_color="333333", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")
    cat_font = Font(bold=True)
    total_font = Font(bold=True)
    total_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    center_align = Alignment(horizontal="center", vertical="center")
    wrap_align = Alignment(vertical="center", wrap_text=True)

    # Column widths
    for i, col in enumerate(columns, 1):
        if col.get("width"):
            ws.column_dimensions[get_column_letter(i)].width = col["width"]

    # --- Row 1-2: Title (merged across every configured column) ---
    ws.merge_cells(start_row=1, start_column=1, end_row=2, end_column=num_cols)
    title_cell = ws.cell(row=1, column=1, value=f"{project_name} {sheet_title}")
    title_cell.font = title_font
    title_cell.alignment = Alignment(horizontal="center", vertical="center")

    # --- Rows 3-4: Created By / Date (label in the second-to-last
    # column, value in the last — generalizes the original D3/E3, D4/E4
    # for whatever column count this template has) ---
    label_col = max(num_cols - 1, 1)
    value_col = num_cols
    ws.cell(row=3, column=label_col, value="Created By")
    created_by_cell = ws.cell(row=3, column=value_col, value=created_by)
    created_by_cell.alignment = Alignment(horizontal="left", vertical="center")

    ws.cell(row=4, column=label_col, value="Date")
    date_cell = ws.cell(row=4, column=value_col, value=datetime.now())
    date_cell.number_format = r"yyyy\-mm\-dd"
    date_cell.alignment = Alignment(horizontal="left", vertical="center")

    # --- Row 5: Headers ---
    for i, col in enumerate(columns, 1):
        cell = ws.cell(row=5, column=i, value=col.get("label") or col["key"])
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    # --- Data rows ---
    row = 6
    grand_total = 0

    for cat in categories:
        cat_start_row = row
        cat_name = cat.get("category", "")

        # Each task as a numbered row (no activity detail flattening)
        task_num = 1
        cat_total_hours = 0

        for task in cat.get("tasks", []):
            task_name = task.get("task", "")
            total_hours = task.get("total_hours", 0)

            for i, col in enumerate(columns, 1):
                key = col["key"]
                if key == "category":
                    continue  # written once per category block, after this loop
                elif key == "task":
                    cell = ws.cell(row=row, column=i, value=f"{task_num}. {task_name}")
                    cell.alignment = wrap_align
                elif key == "estimate_hours":
                    cell = ws.cell(row=row, column=i, value=total_hours)
                    cell.alignment = center_align
                elif key == "working_day":
                    value = f"={get_column_letter(estimate_col)}{row}/8" if estimate_col else None
                    cell = ws.cell(row=row, column=i, value=value)
                    cell.alignment = center_align
                elif key == "remarks":
                    cell = ws.cell(row=row, column=i, value=task.get("remarks", ""))
                    cell.alignment = wrap_align
                else:
                    logger.warning("Unknown export template column key %r; left blank.", key)
                    cell = ws.cell(row=row, column=i)
                cell.border = thin_border

            cat_total_hours += total_hours
            task_num += 1
            row += 1

        cat_end_row = row - 1
        if cat_end_row < cat_start_row:
            continue

        # Category column (merged), if this template has one
        if category_col:
            cat_row_count = cat_end_row - cat_start_row + 1
            if cat_row_count > 1:
                ws.merge_cells(
                    start_row=cat_start_row, start_column=category_col,
                    end_row=cat_end_row, end_column=category_col,
                )
            cat_cell = ws.cell(row=cat_start_row, column=category_col, value=cat_name)
            cat_cell.font = cat_font
            cat_cell.alignment = Alignment(vertical="center")
            cat_cell.border = thin_border
            for r in range(cat_start_row, cat_end_row + 1):
                ws.cell(row=r, column=category_col).border = thin_border

        grand_total += cat_total_hours

    # --- Total row ---
    total_row = row
    ws.cell(row=total_row, column=1, value="Total").font = total_font
    _style_row(ws, total_row, num_cols, border=thin_border, fill=total_fill, font=total_font)

    if estimate_col:
        ws.cell(row=total_row, column=estimate_col, value=grand_total).alignment = center_align
    if working_day_col and estimate_col:
        ws.cell(
            row=total_row, column=working_day_col,
            value=f"={get_column_letter(estimate_col)}{total_row}/8",
        ).alignment = center_align

    wb.save(filepath)