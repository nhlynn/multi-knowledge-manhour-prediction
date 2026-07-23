"""Semantic search service.

Handles AI-powered search using FAISS across one team's indexed knowledge files.

Returns results grouped by Category → Task → Activity, matching the
"Chat Result Single Category" Excel layout.

Team isolation (Phase 5 of multi-team support) is entirely inherited from
the ``EmbeddingService`` this class is constructed with: that instance is
already scoped to one team's ``embeddings_folder``, so
``self.emb_svc._load_metadata()`` below can only ever see that team's
indexed files — there is no other team's data anywhere in this class's
reach. The search algorithm itself (exact-match phase, FAISS fallback,
grouping) is unchanged from Phase 3.
"""

import json
import logging
import os
from collections import OrderedDict
from typing import Any

import faiss
import numpy as np

from services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)


MAX_L2_DISTANCE = 1.4

class SearchService:
    """Service for semantic search across one team's FAISS-indexed knowledge files."""

    def __init__(self, embedding_service: EmbeddingService) -> None:
        self.emb_svc = embedding_service
        # Carried only for logging/traceability — isolation itself comes
        # from emb_svc.embeddings_folder already being this team's folder.
        self.team_slug = embedding_service.team_slug

    def semantic_search(self, query: str, top_k: int = 10) -> dict[str, Any]:
        """Search this team's FAISS indices and return grouped results.

        Scope rules (checked via exact name matching first):
          - Query matches a Category name → output all Tasks of that Category
          - Query matches a Task name → output all Task Details of that Task
          - Query matches a Task Detail → output only that one Task Detail
          - No exact match → fall back to FAISS semantic search

        Returns a dict with:
          - categories: list of category groups, each containing tasks
            and activities with merged-cell spans for the UI.
          - totals: overall totals across all matched results.
          - source: source filename.
        """
        logger.debug(f"semantic_search(team={self.team_slug!r}, query={query!r})")

        metadata = self.emb_svc._load_metadata()
        if not metadata:
            return {"categories": [], "totals": {}}

        # --- Phase 1: Try exact name matching first -----------------------
        exact_result = self._exact_match_search(query, metadata)
        if exact_result is not None:
            return exact_result

        # --- Phase 2: Fall back to FAISS semantic search ------------------
        query_vec = self.emb_svc.generate_embeddings([query])

        hits: list[dict[str, Any]] = []

        for filename, file_meta in metadata.items():
            index_path = file_meta.get("index_path", "")
            mapping_path = file_meta.get("mapping_path", "")

            if not os.path.isfile(index_path) or not os.path.isfile(mapping_path):
                continue

            index = faiss.read_index(index_path)
            with open(mapping_path, "r", encoding="utf-8") as f:
                nested_json = json.load(f)

            from services.excel_parser import extract_texts_from_nested
            texts = extract_texts_from_nested(nested_json)

            k = min(top_k, index.ntotal)
            distances, indices = index.search(query_vec, k)

            id_lookup = _build_id_lookup(nested_json, filename)
            text_to_id = _build_text_to_id(nested_json)

            for dist, idx in zip(distances[0], indices[0]):
                if idx < 0 or idx >= len(texts):
                    continue
                matched_text = texts[idx]
                entry_id = text_to_id.get(matched_text)
                if entry_id and entry_id in id_lookup:
                    hit = dict(id_lookup[entry_id])
                    hit["score"] = float(dist)
                    hits.append(hit)

        if not hits:
            return {"categories": [], "totals": {}}

        # Sort by score (lower L2 = better)
        hits.sort(key=lambda x: x["score"])

        # Reject results that are too far (no meaningful match)
        hits = [h for h in hits if h["score"] <= MAX_L2_DISTANCE]
        if not hits:
            return {"categories": [], "totals": {}}

        # Scope to the best hit's source file, so results never mix
        # content from unrelated KB files.
        best_source = hits[0]["source"]
        hits = [h for h in hits if h["source"] == best_source]

        # Scope filtering based on best hit type
        best_type = hits[0]["type"]
        if best_type == "activity":
            hits = [h for h in hits if h["type"] == "activity"]
        elif best_type == "task":
            hits = [h for h in hits if h["type"] in ("task", "activity")]

        # Filter out results that are too far from the best match.
        best_score = hits[0]["score"]
        max_distance = best_score * 1.2 if best_score > 0 else 0.5
        hits = [h for h in hits if h["score"] <= max_distance]

        hits = hits[:top_k]

        return _group_results(hits, metadata)

    def _exact_match_search(
        self, query: str, metadata: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Check if the query matches a category, task, or detail name.

        Matching priority (most specific wins):
        1. Compound scoped: query shares a meaningful word with a category
           name (even a partial one, e.g. "wordpress" for "Wordpress
           Server") — search only within that category for matching
           tasks/details, using the leftover query words.
        2. Global: search across all categories.

        Within each scope, match priority is: detail > task > category.
        Match modes tried in order: exact → contains → contained-by.

        All matching is case-insensitive.
        """
        query_lower = _clean_query(query)
        query_words = query_lower.split()

        # Load all mapping files once
        all_files: list[tuple[str, list[dict[str, Any]]]] = []
        for filename, file_meta in metadata.items():
            mapping_path = file_meta.get("mapping_path", "")
            if not mapping_path or not os.path.isfile(mapping_path):
                continue
            with open(mapping_path, "r", encoding="utf-8") as f:
                all_files.append((filename, json.load(f)))

        # --- Try compound scoped search first ---
        # If the query shares at least one meaningful word with a category
        # name, scope to that category and search the leftover words
        # against its tasks/details.
        for filename, nested_json in all_files:
            for cat in nested_json:
                cat_name = cat.get("category", "")
                cat_lower = cat_name.strip().lower()
                if not cat_lower:
                    continue

                cat_words = [w for w in cat_lower.split() if w not in _FILLER_WORDS]
                matched_words = [w for w in cat_words if w in query_words]
                if not matched_words:
                    continue

                remainder_words = [w for w in query_words if w not in matched_words]
                remainder = " ".join(remainder_words).strip()

                # If no remainder, it's a pure category match — handle below
                if not remainder:
                    continue

                # Search within this category only, most specific first
                hit = self._match_in_category(
                    cat, remainder, cat_name, filename
                )
                if hit is not None:
                    return hit

        # --- Global search (no category scope) ---
        return self._match_globally(all_files, query_lower)

    def _match_in_category(
        self,
        cat: dict[str, Any],
        query_lower: str,
        cat_name: str,
        filename: str,
    ) -> dict[str, Any] | None:
        """Search for a task or detail match within a single category.

        Uses _best_match_level to decide whether to return details or tasks.
        """
        metadata = self.emb_svc._load_metadata()
        tasks = cat.get("tasks", [])

        level = _best_match_level(tasks, query_lower)
        if level == "detail":
            hits = _find_matching_details(tasks, query_lower, cat_name, filename)
            if hits:
                return _group_results(hits, metadata)
        elif level == "task":
            hits = _find_matching_tasks(tasks, query_lower, cat_name, filename)
            if hits:
                return _group_results(hits, metadata)

        return None

    def _match_globally(
        self,
        all_files: list[tuple[str, list[dict[str, Any]]]],
        query_lower: str,
    ) -> dict[str, Any] | None:
        """Search across all categories for matching detail/task/category."""
        metadata = self.emb_svc._load_metadata()

        # Determine best match level across all categories
        all_tasks: list[dict[str, Any]] = []
        for _, nested_json in all_files:
            for cat in nested_json:
                all_tasks.extend(cat.get("tasks", []))

        level = _best_match_level(all_tasks, query_lower)

        if level == "detail":
            hits: list[dict[str, Any]] = []
            for filename, nested_json in all_files:
                for cat in nested_json:
                    cat_name = cat.get("category", "")
                    hits.extend(_find_matching_details(
                        cat.get("tasks", []), query_lower, cat_name, filename
                    ))
            if hits:
                return _group_results(hits, metadata)

        elif level == "task":
            hits = []
            for filename, nested_json in all_files:
                for cat in nested_json:
                    cat_name = cat.get("category", "")
                    hits.extend(_find_matching_tasks(
                        cat.get("tasks", []), query_lower, cat_name, filename
                    ))
            if hits:
                return _group_results(hits, metadata)

        # --- Category level ---
        hits = []
        for filename, nested_json in all_files:
            for cat in nested_json:
                cat_name = cat.get("category", "")
                cat_lower = cat_name.strip().lower()
                if cat_lower and _name_matches(query_lower, cat_lower):
                    for task in cat.get("tasks", []):
                        if task.get("id"):
                            hits.append({
                                "id": task["id"],
                                "type": "category",
                                "category": cat_name,
                                "source": filename,
                                "score": 0.0,
                            })
        if hits:
            return _group_results(hits, metadata)

        return None


# ------------------------------------------------------------------
# Query matching helpers
# ------------------------------------------------------------------

_FILLER_WORDS = {
    "what", "is", "the", "a", "an", "of", "for", "in", "on", "to",
    "how", "much", "many", "about", "tell", "me", "show", "get",
    "find", "search", "look", "up", "please", "can", "you", "i",
    "want", "know", "need", "time", "hour", "hours", "estimate",
    "manhour", "man-hour", "man", "does", "do", "has", "have",
    "this", "that", "it", "my", "your", "give", "list",
}


def _clean_query(query: str) -> str:
    """Remove filler/stop words from the query, keeping meaningful terms."""
    words = query.strip().lower().split()
    cleaned = [w for w in words if w not in _FILLER_WORDS]
    return " ".join(cleaned) if cleaned else query.strip().lower()


def _name_matches(query_lower: str, name_lower: str) -> bool:
    """Check if query and name match via exact, contains, or contained-by."""
    if query_lower == name_lower:
        return True
    if name_lower in query_lower:
        return True
    if query_lower in name_lower:
        return True
    return False


def _best_match_level(
    tasks: list[dict[str, Any]], query_lower: str
) -> str | None:
    """Determine which level (detail, task, or None) the query best matches.

    Match quality tiers (higher = better):
      3 - exact match (query == name)
      2 - query contains the full name (name is substring of query)
      1 - name contains the query (query is substring of name)

    When both levels match at the same tier, prefer task (broader scope).
    Detail only wins when it has a strictly better tier than task.
    """
    best_detail_tier = 0
    best_task_tier = 0

    for task in tasks:
        task_name = task.get("task", "")
        task_lower = task_name.strip().lower()
        if task_lower:
            tier = _match_tier(query_lower, task_lower)
            best_task_tier = max(best_task_tier, tier)

        for detail in task.get("task_details", []):
            detail_name = detail.get("task_detail", "")
            detail_lower = detail_name.strip().lower()
            if detail_lower:
                tier = _match_tier(query_lower, detail_lower)
                best_detail_tier = max(best_detail_tier, tier)

    if best_detail_tier == 0 and best_task_tier == 0:
        return None
    # Detail wins only if it has a strictly better tier
    if best_detail_tier > best_task_tier:
        return "detail"
    if best_task_tier > 0:
        return "task"
    return None


def _match_tier(query_lower: str, name_lower: str) -> int:
    """Return match quality tier: 3=exact, 2=query contains name, 1=name contains query, 0=no match."""
    if query_lower == name_lower:
        return 3
    if name_lower in query_lower:
        return 2
    if query_lower in name_lower:
        return 1
    return 0


def _find_matching_details(
    tasks: list[dict[str, Any]],
    query_lower: str,
    cat_name: str,
    filename: str,
) -> list[dict[str, Any]]:
    """Find detail-level matches within a list of tasks."""
    hits: list[dict[str, Any]] = []
    for task in tasks:
        task_name = task.get("task", "")
        for detail in task.get("task_details", []):
            detail_name = detail.get("task_detail", "")
            detail_lower = detail_name.strip().lower()
            if detail_lower and _name_matches(query_lower, detail_lower) and detail.get("id"):
                hits.append({
                    "id": detail["id"],
                    "type": "activity",
                    "category": cat_name,
                    "task": task_name,
                    "task_id": task.get("id", ""),
                    "task_detail": detail_name,
                    "estimate_hours": detail.get("estimate_hours", 0),
                    "task_estimate_hours": task.get("estimate_hours", 0),
                    "task_buffer_hours": task.get("buffer_hours", 0),
                    "task_total_hours": task.get("total_hours", 0),
                    "source": filename,
                    "score": 0.0,
                })
    return hits


def _find_matching_tasks(
    tasks: list[dict[str, Any]],
    query_lower: str,
    cat_name: str,
    filename: str,
) -> list[dict[str, Any]]:
    """Find task-level matches within a list of tasks."""
    hits: list[dict[str, Any]] = []
    for task in tasks:
        task_name = task.get("task", "")
        task_lower = task_name.strip().lower()
        if task_lower and _name_matches(query_lower, task_lower) and task.get("id"):
            hits.append({
                "id": task["id"],
                "type": "task",
                "category": cat_name,
                "task": task_name,
                "estimate_hours": task.get("estimate_hours", 0),
                "buffer_hours": task.get("buffer_hours", 0),
                "total_hours": task.get("total_hours", 0),
                "source": filename,
                "activities": task.get("task_details", []),
                "score": 0.0,
            })
    return hits


def _build_id_lookup(
    nested_json: list[dict[str, Any]], filename: str
) -> dict[str, dict[str, Any]]:
    """Build id → structured entry for every level."""
    lookup: dict[str, dict[str, Any]] = {}

    for category in nested_json:
        cat_name = category.get("category", "")

        if category.get("id"):
            lookup[category["id"]] = {
                "id": category["id"],
                "type": "category",
                "category": cat_name,
                "source": filename,
            }

        for task in category.get("tasks", []):
            task_name = task.get("task", "")
            task_estimate = task.get("estimate_hours", 0)
            task_buffer = task.get("buffer_hours", 0)

            if task.get("id"):
                lookup[task["id"]] = {
                    "id": task["id"],
                    "type": "task",
                    "category": cat_name,
                    "task": task_name,
                    "estimate_hours": task_estimate,
                    "buffer_hours": task_buffer,
                    "total_hours": task.get("total_hours", 0),
                    "source": filename,
                    "activities": task.get("task_details", []),
                }

            for detail in task.get("task_details", []):
                if detail.get("id"):
                    lookup[detail["id"]] = {
                        "id": detail["id"],
                        "type": "activity",
                        "category": cat_name,
                        "task": task_name,
                        "task_id": task.get("id", ""),
                        "task_detail": detail.get("task_detail", ""),
                        "estimate_hours": detail.get("estimate_hours", 0),
                        "task_estimate_hours": task_estimate,
                        "task_buffer_hours": task_buffer,
                        "task_total_hours": task.get("total_hours", 0),
                        "source": filename,
                    }

    return lookup


def _build_text_to_id(nested_json: list[dict[str, Any]]) -> dict[str, str]:
    """Map each text field to its entry id."""
    mapping: dict[str, str] = {}
    for category in nested_json:
        if category.get("text") and category.get("id"):
            mapping[category["text"]] = category["id"]
        for task in category.get("tasks", []):
            if task.get("text") and task.get("id"):
                mapping[task["text"]] = task["id"]
            for detail in task.get("task_details", []):
                if detail.get("text") and detail.get("id"):
                    mapping[detail["text"]] = detail["id"]
    return mapping


def _group_results(
    hits: list[dict[str, Any]], metadata: dict[str, Any]
) -> dict[str, Any]:
    """Group hits into Category → Task → Activity structure.

    Scope rules:
      - activity hit  → show only that single activity within its task
      - task hit       → show the full task with all its activities
      - category hit   → show all tasks in the category

    Multiple hits within the same task are merged together so each
    matched activity appears once.  Task-level totals (estimate, buffer,
    final) always reflect the *displayed* activities, not the original
    full-task totals, keeping the numbers consistent with what the user
    sees.
    """
    # ---- pass 1: collect per-task data --------------------------------
    # key = (source, category, task_name)
    # value = {"mode": "full"|"partial", "matched_details": set, ...}
    seen_tasks: OrderedDict[tuple, dict[str, Any]] = OrderedDict()

    for hit in hits:
        source = hit.get("source", "")
        cat_name = hit.get("category", "")

        if hit["type"] == "activity":
            task_key = (source, cat_name, hit.get("task", ""))
            if task_key not in seen_tasks:
                seen_tasks[task_key] = {
                    "source": source,
                    "category": cat_name,
                    "task": hit.get("task", ""),
                    "mode": "partial",
                    "matched_details": set(),
                }
            entry = seen_tasks[task_key]
            # Only add activity if task hasn't already been promoted to full
            entry["matched_details"].add(hit.get("task_detail", ""))

        elif hit["type"] == "task":
            task_key = (source, cat_name, hit.get("task", ""))
            if task_key not in seen_tasks:
                seen_tasks[task_key] = {
                    "source": source,
                    "category": cat_name,
                    "task": hit.get("task", ""),
                    "mode": "full",
                    "matched_details": set(),
                }
            else:
                # Promote partial → full
                seen_tasks[task_key]["mode"] = "full"

        elif hit["type"] == "category":
            # Expand to all tasks in the category
            file_meta = metadata.get(source, {})
            mapping_path = file_meta.get("mapping_path", "")
            if os.path.isfile(mapping_path):
                with open(mapping_path, "r", encoding="utf-8") as f:
                    nested_json = json.load(f)
                for cat in nested_json:
                    if cat.get("category") == cat_name:
                        for task in cat.get("tasks", []):
                            tk = (source, cat_name, task.get("task", ""))
                            if tk not in seen_tasks:
                                seen_tasks[tk] = {
                                    "source": source,
                                    "category": cat_name,
                                    "task": task.get("task", ""),
                                    "mode": "full",
                                    "matched_details": set(),
                                }
                            else:
                                seen_tasks[tk]["mode"] = "full"

    # ---- pass 2: build grouped output ---------------------------------
    cat_order: OrderedDict[tuple, dict[str, Any]] = OrderedDict()

    for task_key, task_info in seen_tasks.items():
        source, cat_name, task_name = task_key
        cat_key = (source, cat_name)
        if cat_key not in cat_order:
            cat_order[cat_key] = {
                "category": cat_name,
                "source": source,
                "tasks": [],
            }

        # Load the full task from mapping
        file_meta = metadata.get(source, {})
        full_task = _load_full_task(file_meta, cat_name, task_name)
        if not full_task:
            continue

        all_activities = full_task.get("activities", [])

        if task_info["mode"] == "full":
            # Show all activities
            activities = [
                {"task_detail": a.get("task_detail", ""), "estimate_hours": a.get("estimate_hours", 0)}
                for a in all_activities
            ]
        else:
            # Show only matched activities
            matched = task_info["matched_details"]
            activities = [
                {"task_detail": a.get("task_detail", ""), "estimate_hours": a.get("estimate_hours", 0)}
                for a in all_activities
                if a.get("task_detail", "") in matched
            ]

        # Compute totals based on displayed activities
        shown_estimate = sum(a["estimate_hours"] for a in activities)
        task_buffer = full_task.get("buffer_hours", 0)

        if task_info["mode"] == "full" or len(activities) == len(all_activities):
            # Full task or all activities matched → use task-level buffer
            buffer_hours = task_buffer
        else:
            # Partial: use standalone buffer (0.5h per activity)
            buffer_hours = sum(0.5 for _ in activities)

        cat_order[cat_key]["tasks"].append({
            "task": task_name,
            "activities": activities,
            "estimate_hours": shown_estimate,
            "buffer_hours": buffer_hours,
            "total_hours": shown_estimate + buffer_hours,
        })

    categories = list(cat_order.values())

    # ---- compute grand totals -----------------------------------------
    total_task_estimate = 0
    total_estimate = 0
    total_buffer = 0
    total_final = 0
    for cat in categories:
        for task in cat["tasks"]:
            total_estimate += task["estimate_hours"]
            total_buffer += task["buffer_hours"]
            total_final += task["total_hours"]
            for act in task["activities"]:
                total_task_estimate += act["estimate_hours"]

    return {
        "categories": categories,
        "totals": {
            "task_estimate": total_task_estimate,
            "estimate": total_estimate,
            "buffer": total_buffer,
            "final": total_final,
        },
    }


def _load_full_task(
    file_meta: dict[str, Any], cat_name: str, task_name: str
) -> dict[str, Any] | None:
    """Load a full task (with all activities) from the mapping file."""
    mapping_path = file_meta.get("mapping_path", "")
    if not mapping_path or not os.path.isfile(mapping_path):
        return None

    with open(mapping_path, "r", encoding="utf-8") as f:
        nested_json = json.load(f)

    for cat in nested_json:
        if cat.get("category") == cat_name:
            for task in cat.get("tasks", []):
                if task.get("task") == task_name:
                    return {
                        "task": task_name,
                        "activities": task.get("task_details", []),
                        "estimate_hours": task.get("estimate_hours", 0),
                        "buffer_hours": task.get("buffer_hours", 0),
                        "total_hours": task.get("total_hours", 0),
                    }
    return None
