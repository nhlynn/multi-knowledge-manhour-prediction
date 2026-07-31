"""Small, shared HTTP request helpers used by more than one ``utils`` module.

Currently just the JSON-vs-HTML response negotiation used by both
``utils/permissions.py`` (401/403 responses) and ``utils/csrf.py``
(400 responses) — factored out so the same check isn't hand-duplicated
in both places.
"""

from flask import request


def wants_json() -> bool:
    """Best-effort guess at whether the caller wants a JSON error, not an HTML redirect.

    Covers both explicit JSON POST bodies (``request.is_json``) and
    ``fetch()``-style AJAX calls, which typically send ``Accept: */*`` —
    ``best_match`` resolves that tie in favor of the first candidate,
    ``application/json``, which is what we want for the JS-driven pages
    (Preview stash APIs, Chatbot search, etc.).
    """
    if request.is_json:
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "application/json"
