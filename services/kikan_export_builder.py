"""KiKan Team's own Export Builder.

Independent from ``services/bamawl_export_builder.py`` (no shared code
between the two beyond the generic, team-agnostic helpers already in
``services/excel_parser.py``) -- built the same way Bamawl Team's
export was: directly on top of KiKan Team's single official Excel
workbook, ``import/kikan/kikan_import_template.xlsx`` -- the exact
same file Template Download serves and import validation accepts (see
``KikanExportBuilder.template_path`` below and
``utils/migrations/kikan_import_export_config.py``). There is
deliberately no separate import-only or export-only template file.

Design:

- Only ``工数詳細`` is ever populated -- no other worksheet in the
  workbook is written to. ``機能一覧``, ``Milestone``, and ``工数・費用``
  ship exactly as the template has them.
- The Preview page's ``categories`` payload (Category → Task →
  Activity) is the single source of truth for everything written: only
  the functions the user actually selected are written, one per row,
  using exactly the values/man-hours/remarks as edited in Preview --
  nothing here re-derives or recomputes a number Preview already
  determined.
- Because ``機能名称`` is off-limits to touch indirectly via
  ``機能一覧`` (the sheet this module no longer populates), each
  selected function's name is written as a literal value directly into
  ``工数詳細``'s own ``機能名称`` cell, replacing that row's original
  ``=VLOOKUP(...)`` formula. Only the *value* changes; the cell's
  existing formatting (font, borders, number format) is untouched, and
  this happens only for rows a selected function is actually written
  into -- unused rows within the block are cleared, not touched.
- Every phase column (``実装工数``, ``コードレビュー``, ``仕様理解``, ...)
  on a populated row is written deterministically from that task's
  activities: a literal value for a phase Preview actually provided, or
  blank for one it didn't -- never left as the template's own row-2
  ratio formula, which would otherwise silently derive a number from
  ``実装工数`` that the user never edited or approved. This mirrors how
  the row's own hours were actually arrived at (a person's edited
  estimate per phase), rather than mechanically reconstructing a
  breakdown from a single base number. The row's own ``合計(h)`` cell
  is left as its original ``=SUM(...)`` formula, which still correctly
  totals whatever literals (and blanks) are written into the phase
  cells above it once Excel opens the file.
- A phase column on a row this export does **not** populate keeps its
  original template formula untouched (see the ``業務分類``/rollup
  note below) -- only ``実装工数`` is blanked across the whole block up
  front, which is enough to zero out every other phase formula for an
  unselected row (``0 * ratio = 0``) without touching those cells
  directly.
- ``業務分類`` (category) is merged across the whole function-row block
  (``A5:A11``) in the template -- only the merge's top-left cell is
  ever written (openpyxl requires this; the rest of a merged range
  must stay empty), so the merge itself is never touched/resized. If
  the selected functions span more than one category, that single cell
  can't represent all of them -- the first one is used and a warning is
  logged (same "known limitation, documented rather than silently
  guessed around" approach ``bamawl_export_builder.py`` takes for its
  own edge cases).
- A task's user-edited remarks have nowhere to go as a literal cell
  value -- ``工数詳細`` has no remarks/notes column of its own, and
  adding one would change the sheet's layout. Instead, remarks are
  attached as an Excel cell comment on the row's ``機能名称`` cell --
  carries the text without adding a visible column or altering the
  sheet's layout at all.
- ``Status`` (a dropdown-validated 大/中/小 field) has no matching
  field in Preview's data and is left blank, the same reasoning
  Bamawl's export blanks its ``ReqDefinition`` free-text sections.
- Unselected functions are never written: every row in the template's
  original function-row block that isn't used by a selected function is
  cleared, not left with stale sample data.

**Known limitation** (a direct consequence of reusing this specific
template file rather than building a fresh one, same category of
limitation as Bamawl's own documented one): ``工数詳細``'s rollup rows
(person-hour/day/month sums, per-role breakdowns) are calibrated to the
template's own built-in 7-row function block. This module writes into
that existing block only (never shifting/extending it) and raises
``KikanExportError`` rather than overflow into the rollup rows if a
project has more selected functions than the block holds.
"""

import logging
import os
from typing import Any

import openpyxl
from openpyxl.comments import Comment

from services.base_export_service import BaseExportService, ExportContext
from services.excel_parser import _find_column, _normalize_header, _safe_float

logger = logging.getLogger(__name__)

_COMMENT_AUTHOR = "MHES"


class KikanExportError(ValueError):
    """Raised when KiKan Team's export template can't be built from,
    or the system data doesn't fit it -- see ``build_kikan_workbook``."""


def _resolve_template_columns(ws, header_row: int) -> dict[str, int]:
    """Map each header cell's (stripped) name to its 1-indexed column
    position -- same approach ``bamawl_export_builder.py`` uses,
    reimplemented independently here rather than imported, so this
    module has no dependency on Bamawl's own export code."""
    name_to_col: dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        raw = ws.cell(row=header_row, column=c).value
        name_to_col[("" if raw is None else str(raw)).strip()] = c
    return name_to_col


def _column_index(name_to_col: dict[str, int], target: str | None) -> int | None:
    """Resolve a configured column name to its column index, tolerant
    of whitespace/case the same way the import side is."""
    if not target:
        return None
    matched = _find_column(list(name_to_col.keys()), target)
    return name_to_col.get(matched) if matched else None


def _template_capacity(ws, data_start_row: int, name_col: int, phase_cols: list[int]) -> int:
    """Return how many function rows are available below the header
    before the template's own rollup/summary block starts.

    Computed from the freshly-loaded, still-untouched template (before
    any clearing/writing happens below): a row with a real function
    name (here: a non-blank ``機能名称`` formula result) is counted; a
    row with that column blank is still counted *unless* one of the
    phase columns already has a value -- that's the signature of a
    rollup row (e.g. the person-hour/day/month sums below the real
    function rows), which marks the boundary.
    """
    row = data_start_row
    while row <= ws.max_row:
        name_val = ws.cell(row=row, column=name_col).value
        if not name_val:
            phase_has_value = any(
                ws.cell(row=row, column=c).value not in (None, "") for c in phase_cols
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


def _clear_block(ws, data_start_row: int, capacity: int, columns: list[int]) -> None:
    """Blank every cell in ``columns`` across the template's whole
    function-row block, before writing the selected functions into it
    -- so no leftover sample value (or stale comment) lingers past
    however many real rows are written below (mirrors
    ``bamawl_export_builder.py``'s own sample-row clearing step)."""
    for r in range(data_start_row, data_start_row + capacity):
        for c in columns:
            if c:
                cell = ws.cell(row=r, column=c)
                cell.value = None
                cell.comment = None


def build_kikan_workbook(
    filepath: str,
    categories: list[dict[str, Any]],
    column_mapping: dict[str, Any],
    template_path: str,
) -> None:
    """Populate KiKan Team's own Excel template's ``工数詳細`` worksheet
    with Preview's data and save the result to ``filepath``. No other
    worksheet in the workbook is touched.

    Args:
        filepath: Where to save the populated workbook.
        categories: The Preview page's Category -> Task -> Activity
            data (same shape ``services/export_workbook_service.py``
            receives) -- the single source of truth for what's
            written. Every task across every category is one selected
            function; only these are exported, one per ``工数詳細``
            row, in order.
        column_mapping: KiKan Team's configured phases-mode column
            mapping (``sheet``, ``header_row``, ``task_column``,
            ``category_column``, ``id_column``, ``phase_columns`` --
            see ``utils/migrations/kikan_import_export_config.py``).
        template_path: Path to KiKan Team's single official template
            workbook (``import/kikan/kikan_import_template.xlsx``).

    Raises:
        KikanExportError: if the template file/worksheet/columns can't
            be found or understood, or the project has more selected
            functions than the template's function-row block can hold
            without touching its rollup rows (see module docstring).
    """
    if not os.path.isfile(template_path):
        raise KikanExportError(f"KiKan Team's export template file is missing: {template_path}")

    sheet_name = column_mapping.get("sheet")
    header_row = column_mapping.get("header_row") or 1

    wb = openpyxl.load_workbook(template_path)

    if sheet_name not in wb.sheetnames:
        raise KikanExportError(
            f"KiKan Team's export template is missing the required '{sheet_name}' worksheet."
        )
    ws = wb[sheet_name]

    name_to_col = _resolve_template_columns(ws, header_row)
    name_col = _column_index(name_to_col, column_mapping.get("task_column"))
    if name_col is None:
        raise KikanExportError(
            "KiKan Team's export template's function-name column could not be located; "
            "cannot populate 工数詳細."
        )
    category_col = _column_index(name_to_col, column_mapping.get("category_column"))
    no_col = _column_index(name_to_col, column_mapping.get("id_column"))
    func_id_col = _column_index(name_to_col, "機能ID")
    status_col = _column_index(name_to_col, "Status")

    phase_cols = [
        (phase["label"], _column_index(name_to_col, phase["column"]))
        for phase in column_mapping.get("phase_columns", [])
    ]
    phase_cols = [(label, idx) for label, idx in phase_cols if idx is not None]
    if not phase_cols:
        raise KikanExportError(
            "KiKan Team's export template's phase-hour columns could not be located; "
            "cannot populate 工数詳細."
        )
    # Of all the phase columns, only "Development" (実装工数) ever holds
    # a literal sample value in the pristine template -- every other
    # phase column (コードレビュー, 仕様理解, QA, ...) is one of the
    # template's own ratio formulas (e.g. G5 = F5*G$2). Blanking just
    # this one column is enough to zero out every downstream formula
    # for a row this export doesn't populate (0 * ratio = 0), so those
    # untouched rows' other phase cells can keep their original
    # template formulas rather than being blanked outright -- more
    # faithful to "keep the worksheet layout identical to the template".
    _base_hours_col = next(
        (idx for label, idx in phase_cols if _normalize_header(label) == _normalize_header("Development")),
        None,
    )

    tasks_with_category = [
        (cat.get("category", ""), task) for cat in categories for task in cat.get("tasks", [])
    ]

    data_start_row = header_row + 1
    phase_col_indexes = [idx for _label, idx in phase_cols]
    capacity = _template_capacity(ws, data_start_row, name_col, phase_col_indexes)
    if len(tasks_with_category) > capacity:
        raise KikanExportError(
            f"This project has {len(tasks_with_category)} function(s), but KiKan Team's "
            f"export template's '{sheet_name}' worksheet only has room for {capacity} "
            f"before its built-in rollup rows -- reduce the number of selected functions, "
            f"or update the template."
        )

    # Clear the template's whole function-row block first -- 機能名称
    # (whose original VLOOKUP formula is replaced with a literal name
    # only for rows a selected function is actually written into),
    # 番号/機能ID/Status, and the one phase column that ever holds a
    # literal sample value (see above) -- so no leftover sample name or
    # sample hours lingers past however many real rows are written
    # below. Every other phase column's formula, and 合計(h), are left
    # exactly as the template has them for rows this export doesn't
    # populate.
    #
    # category_col is excluded here -- it's the top-left cell of a
    # merge spanning the whole block (A5:A11); every other cell in that
    # merged range is a read-only MergedCell placeholder, not a real
    # cell, and is cleared/set only once below, at the merge's top-left
    # row.
    _clear_block(
        ws, data_start_row, capacity,
        [name_col, no_col, func_id_col, status_col, _base_hours_col],
    )
    if category_col:
        ws.cell(row=data_start_row, column=category_col).value = None

    categories_used = {cat for cat, _task in tasks_with_category if cat}
    if len(categories_used) > 1:
        logger.warning(
            "KiKan export: selected functions span %d categories (%s), but '%s' can only "
            "show one category label for the whole block (merged cell) -- using %r.",
            len(categories_used), sorted(categories_used), sheet_name,
            next(iter(categories_used)),
        )
    block_category = next((cat for cat, _task in tasks_with_category if cat), None)
    if category_col and block_category:
        ws.cell(row=data_start_row, column=category_col, value=block_category)

    unmatched_labels: set[str] = set()
    configured_labels = {_normalize_header(label) for label, _idx in phase_cols}

    for i, (_category, task) in enumerate(tasks_with_category, start=1):
        row = data_start_row + i - 1
        activities = task.get("activities", []) or []

        for act in activities:
            norm = _normalize_header(act.get("task_detail") or "")
            if norm and norm not in configured_labels:
                unmatched_labels.add(act.get("task_detail"))

        if no_col:
            ws.cell(row=row, column=no_col, value=i)
        if func_id_col:
            ws.cell(row=row, column=func_id_col, value=f"F{i:03d}")

        name_cell = ws.cell(row=row, column=name_col, value=task.get("task", ""))
        remarks = task.get("remarks")
        if remarks:
            name_cell.comment = Comment(str(remarks), _COMMENT_AUTHOR)

        # Every phase column on a populated row is deterministically
        # set to exactly what Preview provided -- its literal
        # estimate_hours, or blank if this task has no matching
        # activity. A phase the user never edited is never left as the
        # template's own ratio formula (which would otherwise silently
        # derive a number from 実装工数 that Preview never actually
        # produced or the user never approved).
        for label, col_idx in phase_cols:
            value = _phase_value(activities, label)
            # Direct attribute assignment, not the value= kwarg above --
            # ws.cell(row, column, value=None) deliberately leaves a
            # cell untouched when value is None (openpyxl's "no value
            # given" convenience behavior, not "clear it"), which would
            # silently leave the template's ratio formula in place for
            # exactly the un-edited-phase case this is meant to blank.
            ws.cell(row=row, column=col_idx).value = value or None

    if unmatched_labels:
        logger.warning(
            "KiKan export: %d activity label(s) didn't match any configured phase column "
            "and were left out of '%s': %s",
            len(unmatched_labels), sheet_name, sorted(unmatched_labels),
        )

    wb.save(filepath)
    logger.info(
        "Built KiKan Team export workbook: %s (%d selected function(s) written into '%s' "
        "only).",
        filepath, len(tasks_with_category), sheet_name,
    )


class KikanExportBuilder(BaseExportService):
    """KiKan Team's export builder (Strategy Pattern) -- the single
    home for everything KiKan-specific about exporting: the Strategy
    Pattern wiring (``build``), and how to resolve KiKan Team's own
    ``column_mapping``/template path (``resolve_column_mapping``,
    ``template_path``), which used to live in ``routes/export.py`` as
    KiKan-only helper functions -- mirroring exactly how
    ``BamawlExportBuilder`` (``services/bamawl_export_builder.py``)
    consolidates the same shape of logic for Bamawl Team.

    Independent from ``BamawlExportBuilder`` -- no shared code between
    the two beyond the generic, team-agnostic ``BaseExportService`` and
    ``services/excel_parser.py`` helpers both already used. Nothing
    here imports from, or is imported by,
    ``services/bamawl_export_builder.py``.

    ``build`` itself still simply delegates to ``build_kikan_workbook``
    above (unchanged) -- this class is a thin, dedicated container
    around KiKan's own already-existing, already-tested logic, not a
    reimplementation of it.
    """

    team_name = "KiKan Team"

    @staticmethod
    def resolve_column_mapping(mhes_db_path: str, team_id: int) -> dict[str, Any] | None:
        """Return KiKan Team's configured import column mapping for
        ``team_id``, or None if it hasn't been seeded yet.

        The export builder reuses this (rather than a separate
        config) — it already describes exactly which worksheet/columns
        ``工数詳細``'s data lives in, the same mapping
        ``services/team_template_validator.py`` reads it with.
        """
        from repositories.team_import_config_repository import TeamImportConfigRepository

        repo = TeamImportConfigRepository(mhes_db_path)
        config = repo.get_by_team_id(team_id)
        return config["column_mapping"] if config else None

    @staticmethod
    def template_path(app_root_path: str) -> str:
        """Path to KiKan Team's single official Excel template.

        ``import/kikan/kikan_import_template.xlsx`` -- the same public
        sample workbook downloaded from Template Download and accepted
        by import validation -- is the *only* official KiKan Team
        template: there is deliberately no separate, internal-only
        export template. Export builds directly on top of this exact
        file (copy, then populate, then save), the same "one workbook
        for everything" design Bamawl Team's own template uses.

        ``simple_resource/kikan_import_export_template.xlsx`` (the
        original real-data workbook this public template was generated
        from -- see ``import/kikan/build_sample_template.py``) is no
        longer read anywhere at runtime; it exists on disk only as that
        script's source input, kept for reproducibility if the sample
        ever needs regenerating.
        """
        return os.path.join(app_root_path, "import", "kikan", "kikan_import_template.xlsx")

    def build(self, context: ExportContext) -> None:
        build_kikan_workbook(
            context.filepath, context.categories, context.column_mapping, context.template_path,
        )
