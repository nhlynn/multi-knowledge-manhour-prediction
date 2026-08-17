"""Per-team custom knowledge-import parsers.

Mirrors ``services/export_strategies.py``'s registry pattern, but for
the *import* side. Every team NOT listed in ``CUSTOM_IMPORT_PARSERS``
goes through the single, generic ``services.excel_parser.excel_to_nested_json``
(driven entirely by that team's ``column_mapping`` config) — completely
unaffected by this module's existence. A team is only added here when
its official workbook's data genuinely can't come from that generic,
config-driven engine alone. Two different reasons currently justify
that, and they're NOT interchangeable:

- A worksheet whose header shape itself can't be expressed as one
  ``column_mapping`` (e.g. SGL's own two-row header) — that team's
  parser reads its sheet directly, reusing only the generic engine's
  final ``_build_nested_output``/``_log_conversion_summary`` assembly
  step (see ``services/sgl_import_parser.py``'s own module docstring).
- A workbook whose real data spans a SECOND worksheet the generic
  engine's single-``sheet`` config can't reach (e.g. KiKan's own
  ``機能一覧`` cross-reference) — that team's parser instead delegates
  its *entire* main parse to the generic engine unchanged, and only
  adds a thin post-hoc enrichment step for that one extra worksheet
  (see ``services/kikan_import_parser.py``'s own module docstring).
  KiKan's own ``工数詳細`` sheet is itself still 100% generic,
  config-driven parsing underneath -- registering it here does not
  reintroduce any bespoke row-walking logic for that sheet.

Bamawl Team has no such need at all and must never be listed here — it
stays 100% on the generic config-driven path with no wrapper of any
kind.
"""

from typing import Any, Callable

from services.kikan_import_parser import kikan_excel_to_nested_json
from services.sgl_import_parser import sgl_excel_to_nested_json
from services.ssd_import_parser import ssd_excel_to_nested_json

CUSTOM_IMPORT_PARSERS: dict[str, Callable[[str], list[dict[str, Any]]]] = {
    "SGL Team": sgl_excel_to_nested_json,
    "KiKan Team": kikan_excel_to_nested_json,
    "SSD Team": ssd_excel_to_nested_json,
}


def get_custom_import_parser(team_name: str | None) -> Callable[[str], list[dict[str, Any]]] | None:
    """Return ``team_name``'s dedicated nested-JSON parser function, or
    None if that team should use the generic ``excel_to_nested_json``
    (every team not explicitly registered above).
    """
    return CUSTOM_IMPORT_PARSERS.get(team_name) if team_name else None