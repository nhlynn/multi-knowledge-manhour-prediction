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
import threading
from datetime import datetime
from typing import Any

import faiss
import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

METADATA_FILE = "metadata.json"

# Process-wide cache of loaded SentenceTransformer models, keyed by model
# name. Every route constructs a fresh EmbeddingService per request (see
# routes/upload.py, routes/chatbot.py) — without this cache, the ~90MB
# model would be reloaded from disk on every single upload/search
# request instead of once per process. In practice there is exactly one
# model name across the whole app (``Config.EMBEDDING_MODEL``), so this
# cache holds at most one entry for the lifetime of the process — not an
# unbounded/growing cache.
_MODEL_CACHE: dict[str, SentenceTransformer] = {}
_MODEL_CACHE_LOCK = threading.Lock()

# Process-wide cache of loaded FAISS indices, keyed by absolute index
# path and self-invalidating on the file's mtime — so a re-embed (which
# rewrites the .faiss file, changing its mtime) is picked up on the next
# load without any explicit cache-eviction call. Bounded by the number
# of distinct KB files ever searched/embedded in this process's
# lifetime, i.e. by real Knowledge Base size — not per-request growth.
_FAISS_INDEX_CACHE: dict[str, tuple[float, "faiss.Index"]] = {}


def load_faiss_index_cached(index_path: str) -> "faiss.Index | None":
    """Read a FAISS index from disk, reusing a cached instance if the
    file hasn't changed since it was last loaded.

    Shared by ``EmbeddingService.load_index`` and
    ``services.search_service`` (the actual per-request search hot
    path), so a search request doesn't re-read and re-deserialize the
    same unchanged index file every time.

    Returns:
        The loaded index, or None if the file doesn't exist or fails to
        load (caller decides how to handle that — e.g. skip this file).
    """
    try:
        mtime = os.path.getmtime(index_path)
    except OSError:
        return None

    cached = _FAISS_INDEX_CACHE.get(index_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        index = faiss.read_index(index_path)
    except Exception:
        logger.exception("Failed to read FAISS index %s.", index_path)
        return None

    _FAISS_INDEX_CACHE[index_path] = (mtime, index)
    return index


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
        """Load the sentence transformer model (lazy, once per process).

        Reuses a process-wide cached model instance keyed by
        ``model_name`` if another ``EmbeddingService`` (any team, any
        earlier request) has already loaded it — the model itself has no
        team-specific state, so sharing it across instances/requests is
        safe and produces identical embeddings.
        """
        if self.model is not None:
            return

        cached = _MODEL_CACHE.get(self.model_name)
        if cached is not None:
            self.model = cached
            return

        with _MODEL_CACHE_LOCK:
            # Re-check inside the lock: another thread may have finished
            # loading it while this thread was waiting to acquire.
            cached = _MODEL_CACHE.get(self.model_name)
            if cached is not None:
                self.model = cached
                return
            logger.info("Loading embedding model: %s", self.model_name)
            self.model = SentenceTransformer(self.model_name)
            _MODEL_CACHE[self.model_name] = self.model
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
        logger.info("Generating embeddings for %d texts...", len(texts))
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
        logger.info("FAISS index built: %d vectors, dim=%d", self.index.ntotal, dimension)

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
        # Populate the cache with the index already in memory (keyed by
        # its freshly-written mtime), so the very next load of this same
        # path — e.g. this same request's search, or a re-embed followed
        # immediately by a search — doesn't re-read what's already here.
        _FAISS_INDEX_CACHE[index_path] = (os.path.getmtime(index_path), self.index)
        logger.info("FAISS index saved (team=%s): %s", self.team_slug, index_path)
        return index_path

    def load_index(self, index_name: str) -> bool:
        """Load FAISS index from disk (cached — see ``load_faiss_index_cached``).

        Args:
            index_name: Name of the index file (without extension).

        Returns:
            True if loaded successfully, False if the file is missing or
            unreadable.
        """
        index_path = os.path.join(self.embeddings_folder, f"{index_name}.faiss")
        index = load_faiss_index_cached(index_path)
        if index is None:
            logger.warning("Index file not found or unreadable: %s", index_path)
            return False

        self.index = index
        logger.info("FAISS index loaded (team=%s): %d vectors", self.team_slug, self.index.ntotal)
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
        team_name: str | None = None,
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
            team_name: The current team's name, used only to look up a
                dedicated nested-JSON parser in
                ``services.import_strategies.CUSTOM_IMPORT_PARSERS``
                for a team whose worksheet layout the generic
                ``column_mapping``-driven parser can't express (e.g.
                SGL Team's two-row header). Omitting this (the
                default, ``None``) is completely unaffected — the
                lookup simply misses and step 1 falls back to the
                exact same generic ``excel_to_nested_json`` call as
                before this parameter existed.

        Returns:
            Dict with keys: ``filename``, ``categories``,
            ``num_vectors``, ``index_path``, ``embedded_at``.
        """
        from services.excel_parser import excel_to_nested_json, extract_texts_from_nested
        from services.import_strategies import get_custom_import_parser

        if not os.path.isfile(excel_path):
            raise FileNotFoundError(f"Excel file not found: {excel_path}")

        filename = os.path.basename(excel_path)
        index_name = os.path.splitext(filename)[0]

        # 1. Convert Excel to nested JSON
        custom_parser = get_custom_import_parser(team_name)
        if custom_parser:
            nested_json = custom_parser(excel_path)
        else:
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
        mapping_path = self._save_mapping_file(index_name, nested_json)

        # 6. Update central metadata
        file_meta = self._build_file_metadata_record(
            filename=filename, nested_json=nested_json, texts=texts,
            embeddings=embeddings, index_path=index_path, mapping_path=mapping_path,
        )
        self._remember_file_metadata(filename, file_meta)

        logger.info(
            f"Embeddings generated for '{filename}' (team={self.team_slug}): "
            f"{len(nested_json)} categories, "
            f"{len(texts)} text chunks"
        )
        return file_meta

    def _save_mapping_file(self, index_name: str, nested_json: list[dict[str, Any]]) -> str:
        """Persist the nested Category → Task → Activity JSON as this
        file's mapping file, returning its path.
        """
        mapping_path = os.path.join(self.embeddings_folder, f"{index_name}_mapping.json")
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(nested_json, f, indent=2, ensure_ascii=False)
        return mapping_path

    def _build_file_metadata_record(
        self,
        *,
        filename: str,
        nested_json: list[dict[str, Any]],
        texts: list[str],
        embeddings: np.ndarray,
        index_path: str,
        mapping_path: str,
    ) -> dict[str, Any]:
        """Build this file's entry for the central ``metadata.json`` registry."""
        return {
            "filename": filename,
            "team": self.team_slug,
            "categories": [c["category"] for c in nested_json],
            "num_categories": len(nested_json),
            "num_vectors": len(texts),
            "dimension": int(embeddings.shape[1]),
            "index_path": index_path,
            "mapping_path": mapping_path,
            "embedded_at": datetime.now().isoformat(),
        }

    def _remember_file_metadata(self, filename: str, file_meta: dict[str, Any]) -> None:
        """Add/replace one file's entry in the central metadata registry."""
        metadata = self._load_metadata()
        metadata[filename] = file_meta
        self._save_metadata(metadata)

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

    def annotate_files_with_embedding_status(self, kb_files: list[dict[str, Any]]) -> None:
        """Enrich a Knowledge Base file listing with this team's embedding status.

        Mutates each dict in ``kb_files`` in place, adding
        ``has_embeddings``, ``num_categories``, and ``num_vectors`` —
        the exact fields the Upload Files page displays per file.

        Args:
            kb_files: File listing from ``ExcelService.list_knowledge_files()``.
        """
        # Loaded once and reused for every file below — calling
        # get_file_metadata() per file would re-read and re-parse the
        # same metadata.json from disk once per KB file in the list.
        metadata = self._load_metadata()
        for f in kb_files:
            f["has_embeddings"] = self.has_index(f["filename"])
            emb_meta = metadata.get(f["filename"])
            if emb_meta:
                f["num_categories"] = emb_meta.get("num_categories", 0)
                f["num_vectors"] = emb_meta.get("num_vectors", 0)
            else:
                f["num_categories"] = 0
                f["num_vectors"] = 0

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
                logger.info("Deleted: %s", path)

        # Remove from central metadata
        metadata = self._load_metadata()
        if filename in metadata:
            del metadata[filename]
            self._save_metadata(metadata)
            logger.info("Removed '%s' from metadata.json (team=%s)", filename, self.team_slug)
