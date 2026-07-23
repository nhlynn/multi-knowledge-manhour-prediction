"""Embedding generation and management service.

Handles text embedding using Sentence Transformers and FAISS indexing.

- Reads **all worksheets** from each uploaded Excel file.
- Converts every row into a text chunk for embedding.
- Stores one FAISS index per file and a row-level mapping JSON.
- Maintains a central ``metadata.json`` that tracks every embedded file
  (no database required).

Every instance is scoped to a single team: ``embeddings_folder`` is
already that team's isolated folder (see ``utils/team_storage.py``), and
``team_slug`` is carried alongside it purely for traceability — it's
never used to compute a path (Phase 5 of multi-team support). This keeps
team context explicit and self-describing (stamped into every
``metadata.json`` record and log line) rather than only implicit in
which folder happened to be passed in.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

import faiss
import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

METADATA_FILE = "metadata.json"


class EmbeddingService:
    """Service for generating and managing text embeddings."""

    def __init__(self, model_name: str, embeddings_folder: str, team_slug: str) -> None:
        """Initialize EmbeddingService, scoped to a single team.

        Args:
            model_name: Name of the sentence-transformer model.
            embeddings_folder: Path to store this team's FAISS indices and
                metadata (normally ``storage/teams/<team_slug>/embeddings/``
                — see ``utils/team_storage.py``).
            team_slug: The team this instance is scoped to. Carried only
                for traceability (written into every ``metadata.json``
                record and log line) — isolation itself comes entirely
                from ``embeddings_folder`` already being that team's own
                folder, not from this value.
        """
        self.model_name = model_name
        self.embeddings_folder = embeddings_folder
        self.team_slug = team_slug
        self.model: SentenceTransformer | None = None
        self.index: faiss.IndexFlatL2 | None = None
        os.makedirs(self.embeddings_folder, exist_ok=True)

    # ------------------------------------------------------------------
    # Model management
    # ------------------------------------------------------------------

    def load_model(self) -> None:
        """Load the sentence transformer model (lazy, once)."""
        if self.model is None:
            logger.info(f"Loading embedding model: {self.model_name}")
            self.model = SentenceTransformer(self.model_name)
            logger.info("Embedding model loaded successfully.")

    def generate_embeddings(self, texts: list[str]) -> np.ndarray:
        """Generate embeddings for a list of texts.

        Args:
            texts: List of text strings to embed.

        Returns:
            NumPy array of embedding vectors (float32).
        """
        self.load_model()
        assert self.model is not None
        logger.info(f"Generating embeddings for {len(texts)} texts...")
        embeddings = self.model.encode(
            texts, show_progress_bar=False, convert_to_numpy=True
        )
        return np.array(embeddings, dtype=np.float32)

    # ------------------------------------------------------------------
    # FAISS index operations
    # ------------------------------------------------------------------

    def build_index(self, embeddings: np.ndarray) -> None:
        """Build a FAISS index from embeddings.

        Args:
            embeddings: NumPy array of embedding vectors.
        """
        dimension = embeddings.shape[1]
        self.index = faiss.IndexFlatL2(dimension)
        self.index.add(embeddings)
        logger.info(
            f"FAISS index built: {self.index.ntotal} vectors, dim={dimension}"
        )

    def save_index(self, index_name: str) -> str:
        """Save FAISS index to disk.

        Args:
            index_name: Name of the index file (without extension).

        Returns:
            Path to the saved index file.
        """
        if self.index is None:
            raise RuntimeError("No index to save — call build_index first.")

        index_path = os.path.join(self.embeddings_folder, f"{index_name}.faiss")
        faiss.write_index(self.index, index_path)
        logger.info(f"FAISS index saved (team={self.team_slug}): {index_path}")
        return index_path

    def load_index(self, index_name: str) -> bool:
        """Load FAISS index from disk.

        Args:
            index_name: Name of the index file (without extension).

        Returns:
            True if loaded successfully.
        """
        index_path = os.path.join(self.embeddings_folder, f"{index_name}.faiss")
        if not os.path.isfile(index_path):
            logger.warning(f"Index file not found: {index_path}")
            return False

        self.index = faiss.read_index(index_path)
        logger.info(f"FAISS index loaded (team={self.team_slug}): {self.index.ntotal} vectors")
        return True

    # ------------------------------------------------------------------
    # Central metadata (metadata.json, scoped to this team)
    # ------------------------------------------------------------------

    def _metadata_path(self) -> str:
        return os.path.join(self.embeddings_folder, METADATA_FILE)

    def _load_metadata(self) -> dict[str, Any]:
        """Load the central metadata file.

        Returns:
            Dict keyed by filename with per-file embedding info.
        """
        path = self._metadata_path()
        if not os.path.isfile(path):
            return {}

        with open(path, "r", encoding="utf-8") as f:
            metadata: dict[str, Any] = json.load(f)

        # Defense-in-depth for team isolation (Phase 5): always recompute
        # index_path/mapping_path from this team's own embeddings_folder
        # rather than trusting whatever absolute path was stored at embed
        # time. A record embedded before a project move/copy (or, in a
        # future phase, restored from a backup) could otherwise still
        # resolve to a path outside this team's folder even though the
        # actual .faiss/.json files live right here.
        for filename, record in metadata.items():
            index_name = os.path.splitext(filename)[0]
            record["index_path"] = os.path.join(self.embeddings_folder, f"{index_name}.faiss")
            record["mapping_path"] = os.path.join(
                self.embeddings_folder, f"{index_name}_mapping.json"
            )

        return metadata

    def _save_metadata(self, metadata: dict[str, Any]) -> None:
        """Persist the central metadata file."""
        path = self._metadata_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

    def get_file_metadata(self, filename: str) -> dict[str, Any] | None:
        """Return embedding metadata for a single file.

        Args:
            filename: Excel filename (e.g. ``data.xlsx``).

        Returns:
            Metadata dict or None if not embedded.
        """
        return self._load_metadata().get(filename)

    # ------------------------------------------------------------------
    # Core: process an Excel file
    # ------------------------------------------------------------------

    def process_excel_file(
        self, excel_path: str, column_mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Generate embeddings for an Excel file (all worksheets).

        Workflow:
        1. Convert Excel to nested JSON (Category → Task → Activity)
           with rich ``text`` fields for semantic search.
        2. Extract all ``text`` fields as embedding chunks.
        3. Generate embeddings using SentenceTransformer.
        4. Build and save a FAISS index for this file.
        5. Save the nested JSON as the mapping file.
        6. Update this team's central ``metadata.json``.

        The original Excel file is never modified.

        Args:
            excel_path: Full path to the Excel file.
            column_mapping: Optional per-team column-role mapping
                (Phase 7 — see ``services.excel_parser._map_columns``).
                None (the default) uses the original generic keyword
                matching, unchanged from before Phase 7.

        Returns:
            Dict with keys: ``filename``, ``categories``,
            ``num_vectors``, ``index_path``, ``embedded_at``.
        """
        from services.excel_parser import excel_to_nested_json, extract_texts_from_nested

        if not os.path.isfile(excel_path):
            raise FileNotFoundError(f"Excel file not found: {excel_path}")

        filename = os.path.basename(excel_path)
        index_name = os.path.splitext(filename)[0]

        # 1. Convert Excel to nested JSON
        nested_json = excel_to_nested_json(excel_path, column_mapping=column_mapping)

        if not nested_json:
            raise ValueError(f"No data found in {filename}")

        # 2. Extract text fields for embedding
        texts = extract_texts_from_nested(nested_json)

        if not texts:
            raise ValueError(f"No text chunks generated from {filename}")

        # 3. Generate embeddings
        embeddings = self.generate_embeddings(texts)

        # 4. Build and save FAISS index
        self.build_index(embeddings)
        index_path = self.save_index(index_name)

        # 5. Save nested JSON as mapping
        mapping_path = os.path.join(
            self.embeddings_folder, f"{index_name}_mapping.json"
        )
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(nested_json, f, indent=2, ensure_ascii=False)

        # 6. Update central metadata
        category_names = [c["category"] for c in nested_json]
        embedded_at = datetime.now().isoformat()
        file_meta = {
            "filename": filename,
            "team": self.team_slug,
            "categories": category_names,
            "num_categories": len(nested_json),
            "num_vectors": len(texts),
            "dimension": int(embeddings.shape[1]),
            "index_path": index_path,
            "mapping_path": mapping_path,
            "embedded_at": embedded_at,
        }

        metadata = self._load_metadata()
        metadata[filename] = file_meta
        self._save_metadata(metadata)

        logger.info(
            f"Embeddings generated for '{filename}' (team={self.team_slug}): "
            f"{len(nested_json)} categories, "
            f"{len(texts)} text chunks"
        )
        return file_meta

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def has_index(self, filename: str) -> bool:
        """Check if a FAISS index exists for the given KB filename.

        Args:
            filename: Excel filename (e.g. ``data.xlsx``).

        Returns:
            True if the ``.faiss`` index file exists.
        """
        index_name = os.path.splitext(filename)[0]
        index_path = os.path.join(self.embeddings_folder, f"{index_name}.faiss")
        return os.path.isfile(index_path)

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete_index(self, filename: str) -> None:
        """Delete FAISS index, mapping, and metadata entry for a KB file.

        Args:
            filename: Excel filename (e.g. ``data.xlsx``).
        """
        index_name = os.path.splitext(filename)[0]

        # Remove index and mapping files
        for ext in (".faiss", "_mapping.json"):
            path = os.path.join(self.embeddings_folder, f"{index_name}{ext}")
            if os.path.isfile(path):
                os.remove(path)
                logger.info(f"Deleted: {path}")

        # Remove from central metadata
        metadata = self._load_metadata()
        if filename in metadata:
            del metadata[filename]
            self._save_metadata(metadata)
            logger.info(f"Removed '{filename}' from metadata.json (team={self.team_slug})")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _record_to_text(record: dict[str, Any]) -> str:
    """Convert a record dictionary to a searchable text chunk.

    Concatenates all key-value pairs (skipping internal keys like
    ``_sheet``) into a single string.

    Args:
        record: Dictionary of column name to value.

    Returns:
        Text chunk in ``key: value | key: value`` format.
    """
    parts = []
    for key, value in record.items():
        if key.startswith("_"):
            continue
        if value is not None and str(value).strip():
            parts.append(f"{key}: {value}")
    return " | ".join(parts)
