"""Lightweight, dependency-free CSRF protection for MHES.

Every route is guarded by a signed session cookie (see
``utils/auth.py``), which means every state-changing request was
vulnerable to CSRF: a page on another origin can still make the
victim's browser submit a form or fire a ``fetch()`` call that rides
along with their existing MHES session cookie, since browsers attach
cookies to same-site requests regardless of which page triggered them.

This implements the standard synchronizer-token pattern (the same
approach Flask-WTF's ``CSRFProtect`` uses) without adding a new
dependency: a random token is generated once per session and must be
echoed back — as a form field or an ``X-CSRFToken`` header — on every
unsafe-method request (POST/PUT/PATCH/DELETE). ``GET``/``HEAD``/
``OPTIONS`` are never checked (by definition safe/idempotent, and this
is also how the login *page* itself stays reachable pre-token).
"""

import hmac
import secrets

from flask import current_app, flash, jsonify, redirect, request, session

from utils.http import wants_json as _wants_json
from utils.permissions import GateResponse

_SESSION_KEY = "_csrf_token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_FORM_FIELD = "csrf_token"
_HEADER_NAME = "X-CSRFToken"


def get_csrf_token() -> str:
    """Return this session's CSRF token, generating one on first use.

    Exposed to Jinja templates as ``csrf_token()`` (see
    ``app.py``'s context processor) for forms, and via a ``<meta>`` tag
    in ``templates/base.html``/``templates/auth_base.html`` for
    JavaScript ``fetch()`` calls to read and send as a header.
    """
    token = session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_hex(32)
        session[_SESSION_KEY] = token
    return token


def _submitted_token() -> str | None:
    return request.form.get(_FORM_FIELD) or request.headers.get(_HEADER_NAME)


def validate_csrf_request() -> GateResponse | None:
    """``before_request`` hook: reject an unsafe-method request whose
    CSRF token is missing or doesn't match this session's token.

    Returns None to let the request through — Flask's before_request
    contract, matching ``utils/permissions.py``'s hooks.
    """
    if request.method in _SAFE_METHODS:
        return None

    expected = session.get(_SESSION_KEY)
    submitted = _submitted_token()
    if expected and submitted and hmac.compare_digest(expected, submitted):
        return None

    current_app.logger.warning(
        "Rejected request with missing/invalid CSRF token: %s %s", request.method, request.path,
    )
    if _wants_json():
        return jsonify({"error": "Your session has expired. Please refresh the page and try again."}), 400
    flash("Your session has expired. Please refresh the page and try again.", "warning")
    return redirect(request.referrer or "/")
