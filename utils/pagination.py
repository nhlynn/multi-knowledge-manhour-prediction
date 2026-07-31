"""Shared request/pagination helpers for server-side paginated list endpoints.

Both the Export History (``routes/export.py::list_exports``) and
Temporary Data (``routes/preview.py::list_stashes_page``) endpoints
independently implemented the same "parse a 1-based page number from
the query string" and "clamp a stale page number down to the last real
page" logic. Neither depends on Flask beyond the raw string value
already pulled out of ``request.args`` by the caller, so both live here
as plain functions rather than duplicated per route.
"""


def parse_page_param(raw_value: str | None, default: int = 1) -> int:
    """Parse a 1-based page number from a query-string value.

    Falls back to ``default`` for anything missing or non-numeric, and
    never returns a page below 1.
    """
    try:
        page = int(raw_value) if raw_value is not None else default
    except (TypeError, ValueError):
        page = default
    return max(page, 1)


def total_pages_for(total: int, per_page: int) -> int:
    """Return how many pages of ``per_page`` items ``total`` needs (at least 1)."""
    return max((total + per_page - 1) // per_page, 1)
