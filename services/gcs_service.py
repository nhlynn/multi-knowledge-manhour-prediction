"""Google Cloud Storage integration for MHES export files.

Generated Excel workbooks are written to a temporary local file, uploaded
to a private GCS bucket under a fixed folder prefix
(``mhes/bcmm/1001/<file_name>``), then the local temp file is deleted —
the bucket is the only persistent copy afterward. Downloads are served via
short-lived v4 signed URLs so the bucket never needs to be made public.

Configuration (see config.py / .env.example):
    GCP_PROJECT_ID                  — GCP project id (optional; inferred
                                       from the service account credentials
                                       if omitted).
    GCP_BUCKET_NAME                 — target bucket, e.g. "ai-team-001".
    GOOGLE_APPLICATION_CREDENTIALS  — path to a service account JSON key;
                                       read directly by the underlying
                                       google-cloud-storage/google-auth
                                       client libraries from the process
                                       environment, not by this module.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from flask import current_app
from google.cloud import storage

logger = logging.getLogger(__name__)

GCS_EXPORT_PREFIX = "mhes/bcmm/1001"
DEFAULT_SIGNED_URL_EXPIRATION_MINUTES = 15

_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Cached (project_id, bucket_name) -> storage.Bucket, so repeated calls in
# the same process (e.g. one per row when rendering the Export History
# list) reuse one storage.Client/bucket instead of reconnecting to GCS
# every time.
_bucket_cache: dict[tuple[str | None, str], storage.Bucket] = {}


class GCSError(Exception):
    """Raised when a Google Cloud Storage upload/download/signed-URL operation fails."""


class GCSConflictError(GCSError):
    """Raised when an upload is rejected because an object already exists
    at the target path (see ``upload_excel_to_gcs``'s ``if_generation_match=0``
    precondition). Distinct from other GCSErrors so callers can retry with
    a different file name instead of treating it as a hard failure.
    """


def object_path_for(file_name: str) -> str:
    """Return the fixed-convention GCS object path for an export file name.

    Example: ``object_path_for("estimate_001.xlsx")`` ->
    ``"mhes/bcmm/1001/estimate_001.xlsx"``.
    """
    return f"{GCS_EXPORT_PREFIX}/{file_name}"


def is_local_path(path: str) -> bool:
    """Return True if ``path`` looks like a local filesystem path rather
    than a GCS object key.

    Export history rows created before the GCS migration store an
    absolute local path in ``file_path`` (e.g. ``"D:\\...\\exports\\foo.xlsx"``
    or ``"/srv/.../foo.xlsx"``); rows created after it store a GCS object
    path instead (e.g. ``"mhes/bcmm/1001/foo.xlsx"``, no drive letter or
    leading slash). Shared by the export routes and the one-off
    ``utils/migrate_exports_to_gcs.py`` backfill script so both agree on
    which rows still need migrating.
    """
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("/") or path.startswith("\\")


def _get_bucket() -> storage.Bucket:
    """Return the configured GCS bucket, reusing a cached client/bucket
    across calls instead of reconnecting to GCS every time.

    Raises:
        GCSError: If ``GCP_BUCKET_NAME`` isn't configured, or the client/
            bucket can't be resolved (e.g. missing or invalid credentials).
    """
    bucket_name = current_app.config.get("GCP_BUCKET_NAME")
    if not bucket_name:
        raise GCSError(
            "GCP_BUCKET_NAME is not configured. Set it in your .env file "
            "(see .env.example and docs/ARCHITECTURE.md for GCS setup)."
        )
    project_id = current_app.config.get("GCP_PROJECT_ID") or None
    cache_key = (project_id, bucket_name)
    bucket = _bucket_cache.get(cache_key)
    if bucket is not None:
        return bucket
    try:
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
    except Exception as e:
        raise GCSError(f"Could not connect to Google Cloud Storage: {e}") from e
    _bucket_cache[cache_key] = bucket
    return bucket


def upload_excel_to_gcs(local_file_path: str, file_name: str) -> str:
    """Upload a generated Excel file to the private GCS export bucket.

    Uses ``if_generation_match=0``, which tells GCS to accept the upload
    only if no object currently exists at that path — so a file-name
    collision (e.g. two exports landing in the same millisecond, despite
    the timestamp suffix export routes add — see routes/export.py) is
    rejected by GCS itself instead of silently overwriting the earlier
    file. Callers that want to retry on that specific case should catch
    ``GCSConflictError`` (a subclass of ``GCSError``).

    Args:
        local_file_path: Path to the temporary local Excel file to upload.
        file_name: Name to store the file as, e.g. ``"estimate_001.xlsx"``.

    Returns:
        The GCS object path, e.g. ``"mhes/bcmm/1001/estimate_001.xlsx"``
        — this is what gets saved in ``export_history.file_path`` (not a
        ``gs://`` URI).

    Raises:
        GCSConflictError: If an object already exists at that exact path.
        GCSError: If the bucket isn't configured, or the upload fails for
            any other reason (network error, permission error, missing
            credentials, etc).
    """
    from google.api_core.exceptions import PreconditionFailed

    object_path = object_path_for(file_name)
    try:
        bucket = _get_bucket()
        blob = bucket.blob(object_path)
        blob.upload_from_filename(local_file_path, content_type=_XLSX_CONTENT_TYPE, if_generation_match=0)
    except GCSError:
        raise
    except PreconditionFailed as e:
        logger.warning("Upload rejected — an object already exists at '%s'.", object_path)
        raise GCSConflictError(f"An object already exists at '{object_path}'.") from e
    except Exception as e:
        logger.exception("Failed to upload '%s' to GCS as '%s'.", local_file_path, object_path)
        raise GCSError(f"Failed to upload '{file_name}' to Google Cloud Storage: {e}") from e

    logger.info("Uploaded export file to GCS: gs://%s/%s", current_app.config.get("GCP_BUCKET_NAME"), object_path)
    return object_path


def generate_signed_download_url(
    file_path: str,
    *,
    download_name: str | None = None,
    expiration_minutes: int = DEFAULT_SIGNED_URL_EXPIRATION_MINUTES,
) -> str:
    """Generate a short-lived v4 signed URL for downloading a private GCS object.

    Args:
        file_path: The GCS object path, e.g.
            ``"mhes/bcmm/1001/estimate_001.xlsx"`` (as stored in
            ``export_history.file_path``).
        download_name: If given, forces the browser to save the download
            under this filename (via a response-content-disposition
            header) instead of GCS's default.
        expiration_minutes: How long the signed URL stays valid for.

    Returns:
        A signed HTTPS URL for downloading the object directly from GCS —
        the bucket itself is never made public.

    Raises:
        GCSError: If the signed URL can't be generated (missing config,
            missing/invalid credentials, etc).
    """
    try:
        bucket = _get_bucket()
        blob = bucket.blob(file_path)
        extra_params = {}
        if download_name:
            extra_params["response_disposition"] = f'attachment; filename="{download_name}"'
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="GET",
            **extra_params,
        )
    except GCSError:
        raise
    except Exception as e:
        logger.exception("Failed to generate a signed URL for '%s'.", file_path)
        raise GCSError(f"Failed to generate a download link for '{file_path}': {e}") from e


def blob_exists(file_path: str) -> bool:
    """Return whether a GCS object exists at the given path.

    Mirrors the local-disk ``os.path.isfile`` check the Export History list
    page used before files were moved to GCS, so missing/deleted exports
    are still flagged the same way.
    """
    try:
        bucket = _get_bucket()
        return bucket.blob(file_path).exists()
    except Exception:
        logger.exception("Failed to check existence of GCS object '%s'.", file_path)
        return False


def download_excel_bytes(file_path: str) -> bytes:
    """Download a GCS object's raw bytes.

    Used to open a workbook with openpyxl for the read-only in-browser
    Export Detail view, without writing a temp file to disk.

    Raises:
        GCSError: If the download fails (missing object, permission error,
            network error, etc).
    """
    try:
        bucket = _get_bucket()
        return bucket.blob(file_path).download_as_bytes()
    except GCSError:
        raise
    except Exception as e:
        logger.exception("Failed to download GCS object '%s'.", file_path)
        raise GCSError(f"Failed to download '{file_path}' from Google Cloud Storage: {e}") from e
