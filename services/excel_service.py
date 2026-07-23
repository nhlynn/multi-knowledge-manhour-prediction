"""Excel file processing service.

Handles uploading, storing, and listing Excel knowledge files.
Files are saved directly into the kb_knowledge folder as .xlsx files.
No database is used — all metadata is derived from the filesystem.
"""

import logging
import os
from datetime import datetime
from typing import Any

import pandas as pd
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS: set[str] = {"xlsx"}


class ExcelService:
    """Service for managing Excel knowledge base files."""

    def __init__(self, kb_folder: str) -> None:
        """Initialize ExcelService.

        Args:
            kb_folder: Path to the knowledge base folder.
        """
        self.kb_folder = kb_folder
        os.makedirs(self.kb_folder, exist_ok=True)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def is_valid_extension(filename: str) -> bool:
        """Check if file has an allowed Excel extension.

        Args:
            filename: Original filename from the upload.

        Returns:
            True if the extension is .xlsx.
        """
        return (
            "." in filename
            and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
        )

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------

    def file_exists(self, filename: str) -> bool:
        """Check whether a file with the given name already exists in kb_knowledge.

        Args:
            filename: Sanitised filename to check.

        Returns:
            True if the file already exists.
        """
        return os.path.isfile(os.path.join(self.kb_folder, filename))

    def _generate_unique_name(self, filename: str) -> str:
        """Generate a unique filename by appending a timestamp.

        Args:
            filename: Original sanitised filename.

        Returns:
            A filename guaranteed not to collide with existing files.
        """
        base, ext = os.path.splitext(filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_name = f"{base}_{timestamp}{ext}"
        # In the unlikely event of a collision, add a counter
        counter = 1
        while self.file_exists(new_name):
            new_name = f"{base}_{timestamp}_{counter}{ext}"
            counter += 1
        return new_name

    # ------------------------------------------------------------------
    # Upload / save
    # ------------------------------------------------------------------

    def save_file(
        self,
        file_storage: FileStorage,
        duplicate_action: str = "rename",
    ) -> dict[str, Any]:
        """Save an uploaded Excel file into the knowledge base folder.

        Args:
            file_storage: Werkzeug FileStorage object from the upload.
            duplicate_action: What to do when the filename already exists.
                ``"overwrite"`` replaces the existing file.
                ``"rename"`` (default) appends a timestamp to create a
                unique name.

        Returns:
            Dict with keys: ``filename``, ``original_filename``,
            ``size_kb``, ``uploaded_at``, ``overwritten``.
        """
        original_filename = secure_filename(file_storage.filename or "unknown.xlsx")
        save_name = original_filename
        overwritten = False

        if self.file_exists(save_name):
            if duplicate_action == "overwrite":
                overwritten = True
                logger.info(f"Overwriting existing file: {save_name}")
            else:
                save_name = self._generate_unique_name(save_name)
                logger.info(f"Renamed to avoid duplicate: {save_name}")

        dest_path = os.path.join(self.kb_folder, save_name)
        file_storage.save(dest_path)

        size_kb = round(os.path.getsize(dest_path) / 1024, 1)
        uploaded_at = datetime.now().isoformat()

        logger.info(f"Saved KB file: {save_name} ({size_kb} KB)")

        return {
            "filename": save_name,
            "original_filename": original_filename,
            "size_kb": size_kb,
            "uploaded_at": uploaded_at,
            "overwritten": overwritten,
        }

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_knowledge_files(self) -> list[dict[str, Any]]:
        """List all Excel files in the knowledge base folder.

        Returns:
            List of dicts with keys: ``filename``, ``size_kb``,
            ``uploaded_at`` (ISO format), sorted newest-first.
        """
        results: list[dict[str, Any]] = []

        if not os.path.isdir(self.kb_folder):
            return results

        for name in os.listdir(self.kb_folder):
            filepath = os.path.join(self.kb_folder, name)
            if not os.path.isfile(filepath):
                continue
            if not name.lower().endswith(".xlsx"):
                continue

            stat = os.stat(filepath)
            results.append({
                "filename": name,
                "size_kb": round(stat.st_size / 1024, 1),
                "uploaded_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

        results.sort(key=lambda x: x["uploaded_at"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_file(self, filename: str) -> bool:
        """Delete a knowledge base file.

        Args:
            filename: Name of the file to delete.

        Returns:
            True if deleted, False if not found.
        """
        filepath = os.path.join(self.kb_folder, filename)
        if not os.path.isfile(filepath):
            logger.warning(f"File not found for deletion: {filename}")
            return False

        os.remove(filepath)
        logger.info(f"Deleted KB file: {filename}")
        return True

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_excel(self, filename: str) -> pd.DataFrame:
        """Read a knowledge base Excel file into a DataFrame.

        Args:
            filename: Name of the file in kb_knowledge.

        Returns:
            DataFrame with cleaned column headers and no all-empty rows.
        """
        filepath = os.path.join(self.kb_folder, filename)
        logger.info(f"Reading Excel file: {filepath}")
        df = pd.read_excel(filepath, engine="openpyxl")
        df.columns = df.columns.str.strip()
        df = df.dropna(how="all")
        logger.info(f"Read {len(df)} rows, {len(df.columns)} columns from {filename}")
        return df
