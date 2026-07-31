"""Generic in-memory sliding-window request rate limiter.

Not tied to any one endpoint's semantics (unlike
``utils/login_rate_limiter.py``, which specifically models login
lockout — failed vs. successful *attempts*, with lockout that clears on
success). This is just "has this key made more than N requests in the
last W seconds", usable by any caller that wants to throttle by an
arbitrary key (e.g. an email address, an IP, or a combination) —
currently used by ``routes/auth.py::forgot_password`` to limit repeated
reset requests per email/IP.

Per-process, in-memory, resets on restart — the same tradeoff already
accepted by ``utils/login_rate_limiter.py`` for this single-process app.
"""

import threading
import time

_lock = threading.Lock()
_requests: dict[str, list[float]] = {}

# Defense-in-depth bound: caps memory use if an attacker tries many
# distinct keys to grow this dict — evicts the least-recently-active
# key rather than growing forever (same pattern as login_rate_limiter).
_MAX_TRACKED_KEYS = 10_000


def is_rate_limited(key: str, *, max_requests: int, window_seconds: int) -> bool:
    """Return whether ``key`` has already made ``max_requests`` or more
    within the last ``window_seconds``.

    Does not itself record a new request — call ``record_request(key)``
    once the caller has decided to actually proceed, so a check alone
    (e.g. checking several keys before deciding) never double-counts.
    """
    now = time.monotonic()
    with _lock:
        history = [t for t in _requests.get(key, []) if now - t < window_seconds]
        _requests[key] = history
        return len(history) >= max_requests


def record_request(key: str) -> None:
    """Record one request against ``key`` for future ``is_rate_limited`` checks."""
    now = time.monotonic()
    with _lock:
        if len(_requests) >= _MAX_TRACKED_KEYS and key not in _requests:
            oldest_key = min(_requests, key=lambda k: _requests[k][-1] if _requests[k] else 0.0)
            _requests.pop(oldest_key, None)
        history = _requests.get(key, [])
        history.append(now)
        _requests[key] = history
