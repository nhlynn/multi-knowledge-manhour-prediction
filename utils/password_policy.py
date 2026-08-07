"""Password strength policy for MHES.

Centralized here so the same rule set governs every place a user
chooses a new password: Forgot Password's self-service Reset Password
(``routes/auth.py::reset_password``) and Create User
(``services/user_service.py::create_user``) both call this single
function rather than each hand-rolling their own checks.
"""

import re

MIN_LENGTH = 8


def validate_password_strength(password: str) -> str | None:
    """Return an error message if ``password`` fails the minimum
    strength policy, or None if it passes.

    Policy: at least ``MIN_LENGTH`` characters, with at least one
    uppercase letter, one lowercase letter, and one digit.
    """
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters long."
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter."
    if not re.search(r"\d", password):
        return "Password must contain at least one digit."
    return None
