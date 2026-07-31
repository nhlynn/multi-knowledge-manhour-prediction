"""Filename generation and upload-retry logic for the export pipeline.

Moved out of ``routes/export.py`` — none of this depends on Flask's
request/session context, so it belongs in the service layer rather than
the route.
"""

import logging
import os
import re
import unicodedata
from datetime import datetime

from services.gcs_service import GCSConflictError, GCSError, upload_excel_to_gcs

logger = logging.getLogger(__name__)

_INVALID_FILENAME_CHARS = re.compile(r'[\\/*?:"<>|]')
_MAX_SANITIZED_NAME_LENGTH = 150
_FALLBACK_PROJECT_NAME = "Export"


def sanitize_project_name_for_filename(project_name: str) -> str:
    """Turn a user-entered project name into a safe Windows/GCS filename fragment.

    Beyond replacing the characters Windows/GCS reject outright, this
    also strips control characters (e.g. a stray newline/tab pasted into
    the Project Name field), trims characters Windows silently drops
    from the end of a filename (trailing dots/spaces), and caps the
    length so the final, timestamp-suffixed filename stays well under
    filesystem/GCS object-name limits. None of this affects a normal,
    short, plain-text project name — only pathological input.
    """
    stripped_controls = "".join(
        ch for ch in project_name if unicodedata.category(ch)[0] != "C"
    )
    safe = _INVALID_FILENAME_CHARS.sub("_", stripped_controls).strip()
    safe = safe.rstrip(". ")
    safe = safe[:_MAX_SANITIZED_NAME_LENGTH].rstrip(". ")
    return safe or _FALLBACK_PROJECT_NAME


def build_export_filename(safe_name: str) -> str:
    """Build an export filename with a millisecond-precision timestamp
    suffix, so repeat exports of the same project never collide/overwrite
    each other in GCS (object paths are keyed by file_name — see
    ``services/gcs_service.py``). Colons/periods aren't valid in Windows
    filenames, so dd-mm-yyyy_HH-mm-ss-SSS is used instead of
    dd-mm-yyyy HH:mm:ss.SSS.
    """
    timestamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S-%f")[:-3]
    return f"{safe_name}_manhour_{timestamp}.xlsx"


def upload_export_with_retry(
    temp_dir: str, safe_name: str, file_bytes: bytes, max_attempts: int = 3,
) -> tuple[str, str]:
    """Upload the given Excel bytes to GCS under a fresh timestamped
    filename, retrying with a new timestamp if GCS rejects the write
    because an object already exists at that exact path — an extremely
    unlikely millisecond-timestamp collision (e.g. two exports of the same
    project landing in the same millisecond), rejected by GCS itself via
    ``upload_excel_to_gcs``'s ``if_generation_match=0`` precondition rather
    than silently overwritten. Transparent to the caller: on success this
    just looks like one upload with a slightly later timestamp.

    Returns:
        ``(filename, object_path)`` for the attempt that succeeded.

    Raises:
        GCSError: The error from the final attempt, if every attempt failed,
            or if ``max_attempts`` is less than 1 (a caller error, not a
            GCS failure — this still raises ``GCSError`` so every failure
            mode of this function is one exception type for callers to catch).
    """
    if max_attempts < 1:
        raise GCSError(f"upload_export_with_retry called with max_attempts={max_attempts!r} (must be >= 1).")

    last_error: GCSError | None = None
    for attempt in range(1, max_attempts + 1):
        filename = build_export_filename(safe_name)
        temp_path = os.path.join(temp_dir, filename)
        try:
            with open(temp_path, "wb") as f:
                f.write(file_bytes)
            object_path = upload_excel_to_gcs(temp_path, filename)
            return filename, object_path
        except GCSConflictError as e:
            last_error = e
            logger.warning(
                "Export filename collided in GCS (attempt %d/%d): %s. Retrying with a new timestamp.",
                attempt, max_attempts, filename,
            )
        except GCSError as e:
            last_error = e
            break
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                logger.warning("Could not remove temporary export file: %s", temp_path)
    raise last_error
