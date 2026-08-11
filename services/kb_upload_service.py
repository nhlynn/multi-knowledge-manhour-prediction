"""Orchestrates a batch Knowledge Base file upload.

Moved out of ``routes/upload.py`` so the upload route stays a thin
request/response translator: validate extension, save each file
(honoring the rename-vs-overwrite duplicate policy), then auto-generate
its embeddings — producing one user-facing message per meaningful step,
in the exact order/wording the route used to flash them directly.
"""

import logging
from dataclasses import dataclass
from typing import Any

from werkzeug.datastructures import FileStorage

from services.embedding_service import EmbeddingService
from services.excel_service import ExcelService

logger = logging.getLogger(__name__)


@dataclass
class FlashMessage:
    """One user-facing status message plus its Bootstrap alert category
    (``"success"``, ``"info"``, ``"warning"``, or ``"danger"``)."""

    text: str
    category: str


@dataclass
class UploadBatchResult:
    """Outcome of processing every file in one upload request."""

    messages: list[FlashMessage]
    success_count: int
    fail_count: int


def upload_and_embed_files(
    files: list[FileStorage],
    *,
    duplicate_action: str,
    excel_service: ExcelService,
    embedding_service: EmbeddingService,
    column_mapping: dict[str, Any] | None,
    team_name: str | None = None,
) -> UploadBatchResult:
    """Validate, save, and auto-embed every file in one upload batch.

    Args:
        files: Werkzeug ``FileStorage`` objects from ``request.files.getlist(...)``.
        duplicate_action: ``"overwrite"`` or ``"rename"`` (see ``ExcelService.save_file``).
        excel_service: Already scoped to the current session's team folder.
        embedding_service: Already scoped to the current session's team folder.
        column_mapping: The team's configured Excel column mapping, or None.
        team_name: The current session's team name, passed straight
            through to ``EmbeddingService.process_excel_file`` (see its
            own docstring) — only relevant for a team with a dedicated
            entry in ``services.import_strategies.CUSTOM_IMPORT_PARSERS``.

    Returns:
        Every flash-worthy message produced, in the same order the
        original per-file processing loop generated them, plus overall
        success/fail counts (files with a blank filename are skipped
        entirely and don't count toward either).
    """
    messages: list[FlashMessage] = []
    success_count = 0
    fail_count = 0

    for file in files:
        if file.filename is None or file.filename.strip() == "":
            continue

        if not ExcelService.is_valid_extension(file.filename):
            messages.append(FlashMessage(
                f"Skipped '{file.filename}': invalid file type.", "danger",
            ))
            fail_count += 1
            continue

        try:
            messages.extend(_save_and_embed_one_file(
                file, duplicate_action, excel_service, embedding_service, column_mapping, team_name,
            ))
            success_count += 1
        except Exception as e:
            logger.error("Upload failed for '%s': %s", file.filename, e)
            messages.append(FlashMessage(
                f"Failed to upload '{file.filename}': {e}", "danger",
            ))
            fail_count += 1

    if success_count:
        logger.info("Upload batch: %d succeeded, %d failed", success_count, fail_count)

    return UploadBatchResult(messages, success_count, fail_count)


def _save_and_embed_one_file(
    file: FileStorage, duplicate_action: str, excel_service: ExcelService,
    embedding_service: EmbeddingService, column_mapping: dict[str, Any] | None,
    team_name: str | None = None,
) -> list[FlashMessage]:
    """Save one file and attempt to embed it, returning its status message(s).

    A save failure propagates to the caller (counted as a batch failure).
    An embedding failure does not — the file is already saved
    successfully, so it's reported as a warning rather than an upload
    failure, exactly as before this was extracted from the route.
    """
    messages: list[FlashMessage] = []

    meta = excel_service.save_file(file, duplicate_action=duplicate_action)
    label = meta["filename"]
    if meta["overwritten"]:
        # Delete old embeddings before rebuilding.
        embedding_service.delete_index(label)
        messages.append(FlashMessage(f"Overwritten: {label} ({meta['size_kb']} KB)", "success"))
    else:
        messages.append(FlashMessage(f"Uploaded: {label} ({meta['size_kb']} KB)", "success"))

    try:
        kb_path = excel_service.get_kb_path(label)
        result = embedding_service.process_excel_file(
            kb_path, column_mapping=column_mapping, team_name=team_name,
        )
        messages.append(FlashMessage(
            f"Embeddings ready for {label}: "
            f"{result['num_vectors']} text chunks from "
            f"{result['num_categories']} category(ies).",
            "info",
        ))
    except Exception as e:
        logger.error("Embedding failed for '%s': %s", label, e)
        messages.append(FlashMessage(
            f"Uploaded '{label}' but embedding failed: {e}", "warning",
        ))

    return messages
