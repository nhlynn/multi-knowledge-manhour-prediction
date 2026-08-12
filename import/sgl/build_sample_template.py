"""One-off script that generates ``import/sgl/sgl_import_template.xlsx``
from ``simple_resource/sgl_import_export_format.xlsx``.

Not part of the application's runtime import/export code paths (see
``services/team_template_validator.py`` / ``services/sgl_export_builder.py``
for those) -- this is a standalone, one-time sanitization script, kept
alongside its output for reproducibility/traceability (e.g. if the real
source template's structure ever changes and this sample needs
regenerating). Never overwrites ``SOURCE`` -- only ever reads it and
writes to ``DEST``.

Replaces every real-project-identifying value in
"詳細見積_マスタと予実比較" (the 6 real task rows' 区分/項目/作業詳細 text
and phase-hour numbers) and "見積・金額サマリ"'s sample project title
with generic sample text/round numbers, while leaving the workbook's
structure, worksheet names, column order, formulas, merged cells,
borders, row heights, and column widths completely untouched -- only
specific cells' *values* are replaced. Column B's block labels (e.g.
"ユーザマスタ", "部署マスター") are structural section headings, not
project-identifying data, and are left as-is -- same reasoning
``import/kikan/build_sample_template.py`` applies to that template's
own structural 業務分類 values.

Known, unavoidable characteristic (an openpyxl limitation, not
something this script introduces -- see
``import/kikan/build_sample_template.py``'s own note): any workbook
re-saved via openpyxl loses the *cached* display value of every
formula cell workbook-wide, regardless of which cells were actually
edited. The formulas themselves are preserved verbatim, and opening
this file in real Microsoft Excel recalculates and displays correct
values immediately (Excel's default calculation mode is Automatic).
This doesn't affect this app's own import parsing
(``services/sgl_import_parser.py`` only ever reads the phase-hour
columns, which hold literal values, never the SUM/total formula
cells) -- it only matters for a tool that reads cached values without
recalculating first.

Also strips ``SOURCE``'s thousands of orphaned Defined Names and
dozens of External Links (see
``services/sgl_export_builder.py::_strip_legacy_workbook_bloat`` for
the full explanation) before saving ``DEST`` -- otherwise this public,
git-tracked sample file would carry that same bloat forward on every
regeneration.
"""

import openpyxl
from openpyxl.cell.cell import MergedCell

SOURCE = "simple_resource/sgl_import_export_format.xlsx"
DEST = "import/sgl/sgl_import_template.xlsx"

wb = openpyxl.load_workbook(SOURCE)

# Strip legacy bloat inherited from SOURCE -- see
# services/sgl_export_builder.py::_strip_legacy_workbook_bloat.
wb.defined_names.clear()
wb._external_links = []

detail = wb["詳細見積_マスタと予実比較"]

# Each of the 6 real task rows: (row, category, task name, work detail,
# {column_letter: sample_hours}) -- phase-hour columns are H(要件定義),
# I(設計), J(開発), K(テスト), L(クラウド対応), M(その他); only the
# columns that row's original data actually populated are replaced,
# every other phase column on that row is left at its original blank.
# Row 17's 区分 (category) is merged with row 16's (C16:C17) -- writing
# it there is skipped (would raise on the merge's read-only non-anchor
# cell); it already shows row 16's sample category once Excel renders
# the merge.
SAMPLE_ROWS = [
    (5, "Sample Category 1", "Sample Function 1", "Sample task detail 1.", {"J": 8, "K": 4}),
    (8, "Sample Category 2", "Sample Function 2", "Sample task detail 2.", {"H": 2, "J": 4, "K": 2}),
    (11, "Sample Category 3", "Sample Function 3", "Sample task detail 3.", {"H": 2, "J": 16, "K": 8}),
    (15, "Sample Category 4", "Sample Function 4", None, {"K": 4}),
    (16, "Sample Category 5", "Sample Function 5", "Sample task detail 5.", {"I": 2}),
    (17, None, "Sample Function 6", None, {"K": 1, "L": 1}),
]

for row, category, task, work_detail, hours in SAMPLE_ROWS:
    if category is not None and not isinstance(detail[f"C{row}"], MergedCell):
        detail[f"C{row}"] = category
    detail[f"D{row}"] = task
    detail[f"E{row}"] = work_detail
    for col_letter, value in hours.items():
        detail[f"{col_letter}{row}"] = value

# The sample workbook's own project title (見積・金額サマリ!A1) --
# genericized for a public download template.
summary = wb["見積・金額サマリ"]
summary["A1"] = "Sample Project"

wb.save(DEST)