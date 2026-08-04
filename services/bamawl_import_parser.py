"""Bamawl Team's own Import Parser.

Bamawl Team's Knowledge Base source (``simple_resource/bamawl_import_export_format*.xlsx``)
is a distinctly-structured, multi-sheet workbook -- not a plain flat
category/task/detail/estimate sheet like the generic upload path
expects. This module is the dedicated, validating front door for that
file:

- Accepts only a workbook that actually looks like Bamawl Team's
  template (checked structurally: the required worksheet and header
  columns, not just the filename).
- Reads only the ``ALL_Detail`` worksheet (or whatever ``sheet`` Bamawl
  Team's ``column_mapping`` configures -- see
  ``utils/migrations/bamawl_import_export_config.py``).
- Converts it into MHES's internal nested category/task/activity data
  model, delegating to the existing shared "phases mode" converter
  (``services.excel_parser.excel_to_nested_json`` /
  ``docs/ARCHITECTURE.md`` §5i) once validation has passed.
- Rejects anything else with a clear, specific error instead of
  ``excel_to_nested_json``'s normal lenient behavior (log a warning,
  return an empty/partial result) -- appropriate for the *generic*
  best-effort upload path other teams use, but not for Bamawl Team's
  single, known template.

Only used for Bamawl Team (see ``routes/upload.py``); every other
team's upload path is unaffected by this module's existence.
"""

import logging
from typing import Any

import openpyxl
import pandas as pd

from services.excel_parser import _find_column, excel_to_nested_json

logger = logging.getLogger(__name__)


class BamawlTemplateError(ValueError):
    """Raised when a workbook doesn't structurally match Bamawl Team's
    expected Excel template -- see ``validate_bamawl_template``."""


def _rewind(source: Any) -> None:
    """Seek a file-like ``source`` back to its start, if it supports it.

    Validation reads ``source`` twice (worksheet list, then header
    row) before the caller (or ``parse_bamawl_template``) reads it a
    third time to actually convert it -- a plain file path re-opens
    fine each time, but an in-memory upload stream (e.g. Flask's
    ``FileStorage.stream``, read directly so nothing is written to
    disk before validation passes) must be rewound between reads.
    """
    if hasattr(source, "seek"):
        source.seek(0)


def validate_bamawl_template(source: Any, column_mapping: dict[str, Any]) -> None:
    """Validate that ``source`` structurally matches Bamawl Team's
    Excel template, as described by ``column_mapping`` (Bamawl Team's
    configured ``team_import_configs`` row).

    Args:
        source: A file path, or a file-like/stream object (e.g. an
            upload's ``FileStorage.stream``) -- left rewound to its
            start when this function returns, whether it raises or not.
        column_mapping: Bamawl Team's phases-mode column mapping (see
            ``utils.migrations.bamawl_import_export_config.BAMAWL_IMPORT_COLUMN_MAPPING``).

    Raises:
        BamawlTemplateError: with a specific, human-readable reason --
            the workbook can't be opened at all, its required
            worksheet is missing, or that worksheet's header row is
            missing one or more required columns (task/id/phase
            columns).
    """
    sheet_name = column_mapping.get("sheet")
    header_row = column_mapping.get("header_row") or 1

    try:
        wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
        sheet_names = list(wb.sheetnames)
        wb.close()
    except Exception as e:
        raise BamawlTemplateError(
            "This file could not be opened as an Excel workbook. "
            "Please upload the Bamawl Team template (.xlsx)."
        ) from e
    finally:
        _rewind(source)

    if sheet_name not in sheet_names:
        raise BamawlTemplateError(
            f"This doesn't look like the Bamawl Team template: the required "
            f"'{sheet_name}' worksheet is missing. This workbook has: "
            f"{', '.join(sheet_names)}."
        )

    try:
        header_df = pd.read_excel(
            source, sheet_name=sheet_name, header=header_row - 1, nrows=0, engine="openpyxl",
        )
    except Exception as e:
        raise BamawlTemplateError(
            f"Could not read the '{sheet_name}' worksheet's header row "
            f"(expected on row {header_row}). Please upload the Bamawl Team template."
        ) from e
    finally:
        _rewind(source)

    columns = header_df.columns.tolist()

    required: list[tuple[str, str | None]] = [("Task", column_mapping.get("task_column"))]
    if column_mapping.get("id_column"):
        required.append(("ID", column_mapping["id_column"]))
    for phase in column_mapping.get("phase_columns", []):
        required.append((phase["label"], phase["column"]))

    missing = [label for label, col in required if _find_column(columns, col) is None]
    if missing:
        raise BamawlTemplateError(
            f"This file doesn't match the Bamawl Team template: missing required "
            f"column(s) in '{sheet_name}' (row {header_row}): {', '.join(missing)}."
        )


def parse_bamawl_template(source: Any, column_mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate, then convert Bamawl Team's workbook into MHES's
    internal nested category/task/activity data model.

    Args:
        source: A file path, or a file-like/stream object.
        column_mapping: Bamawl Team's phases-mode column mapping.

    Returns:
        The same category/task/activity structure
        ``excel_to_nested_json`` produces for every other team.

    Raises:
        BamawlTemplateError: if ``source`` doesn't structurally match
            Bamawl Team's template -- nothing is parsed in that case.
    """
    validate_bamawl_template(source, column_mapping)
    _rewind(source)
    return excel_to_nested_json(source, column_mapping=column_mapping)
