"""Per-team custom knowledge-import parsers.

Mirrors ``services/export_strategies.py``'s registry pattern, but for
the *import* side. Every team NOT listed in ``CUSTOM_IMPORT_PARSERS``
goes through the single, generic ``services.excel_parser.excel_to_nested_json``
(driven entirely by that team's ``column_mapping`` config) — completely
unaffected by this module's existence. A team is only added here when
its official worksheet's layout genuinely can't be expressed through
that generic, single-header-row ``column_mapping`` (see
``services/sgl_import_parser.py``'s module docstring for why SGL needs
one). Bamawl Team and KiKan Team are not, and must never be, listed
here — both stay 100% on the generic config-driven path.
"""

from typing import Any, Callable

from services.sgl_import_parser import sgl_excel_to_nested_json

CUSTOM_IMPORT_PARSERS: dict[str, Callable[[str], list[dict[str, Any]]]] = {
    "SGL Team": sgl_excel_to_nested_json,
    # "SSD Team": ssd_excel_to_nested_json,  -- only if SSD's template ever needs one too
}


def get_custom_import_parser(team_name: str | None) -> Callable[[str], list[dict[str, Any]]] | None:
    """Return ``team_name``'s dedicated nested-JSON parser function, or
    None if that team should use the generic ``excel_to_nested_json``
    (every team not explicitly registered above).
    """
    return CUSTOM_IMPORT_PARSERS.get(team_name) if team_name else None
