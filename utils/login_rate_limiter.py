"""In-memory login attempt rate limiting for MHES.

No new dependency (e.g. Flask-Limiter) — a small, self-contained,
per-process sliding-window lockout keyed by username. MHES runs as a
single process (see docs/ARCHITECTURE.md), so an in-memory tracker is
sufficient; it resets on restart, an acceptable tradeoff at this scale.

Without this, ``/auth/login`` had no limit at all on repeated password
guesses against a known username.
"""

import threading
import time

_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 15 * 60
_LOCKOUT_SECONDS = 15 * 60

# Defense-in-depth bound: caps memory use if an attacker tries many
# distinct (nonexistent) usernames to grow this dict — evicts the
# oldest-tracked username rather than growing forever.
_MAX_TRACKED_USERNAMES = 10_000

_lock = threading.Lock()
_attempts: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def _normalize(username: str) -> str:
    return username.strip().lower()


def is_locked_out(username: str) -> bool:
    """Return whether this username is currently locked out from logging in."""
    key = _normalize(username)
    with _lock:
        until = _locked_until.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            _locked_until.pop(key, None)
            _attempts.pop(key, None)
            return False
        return True


def record_failed_attempt(username: str) -> None:
    """Record a failed login attempt, locking the username out if it has
    reached ``_MAX_ATTEMPTS`` failures within the sliding window.
    """
    key = _normalize(username)
    now = time.monotonic()
    with _lock:
        if len(_attempts) >= _MAX_TRACKED_USERNAMES and key not in _attempts:
            oldest_key = min(_attempts, key=lambda k: _attempts[k][-1] if _attempts[k] else 0)
            _attempts.pop(oldest_key, None)
            _locked_until.pop(oldest_key, None)

        history = [t for t in _attempts.get(key, []) if now - t < _WINDOW_SECONDS]
        history.append(now)
        _attempts[key] = history
        if len(history) >= _MAX_ATTEMPTS:
            _locked_until[key] = now + _LOCKOUT_SECONDS


def record_successful_attempt(username: str) -> None:
    """Clear any tracked failures/lockout for this username after a successful login."""
    key = _normalize(username)
    with _lock:
        _attempts.pop(key, None)
        _locked_until.pop(key, None)
