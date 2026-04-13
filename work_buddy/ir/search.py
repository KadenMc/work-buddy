"""Reusable IR search orchestration.

Encapsulates the full search pipeline: method parsing, routing to
substring/keyword/semantic backends, multi-method RRF fusion, and
recency bias. Returns **structured result dicts** — formatting is
the caller's responsibility.

This module is safe to import in the MCP server process:
- embedding.client.ir_search uses HTTP (safe)
- ir.recency is pure Python (safe)
- ir.store.substring_search imports sqlite3 (tolerated, see note below)
- RRF fusion is inlined to avoid importing ir.store for other functions
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Method parsing
# ---------------------------------------------------------------------------

_VALID_METHODS = {"substring", "keyword", "semantic"}
_SOLO_ONLY = {"substring"}


def _parse_methods(method_str: str) -> list[str]:
    """Parse and validate a comma-delimited method string."""
    methods = [m.strip().lower() for m in method_str.split(",") if m.strip()]
    if not methods:
        return ["keyword", "semantic"]

    invalid = [m for m in methods if m not in _VALID_METHODS]
    if invalid:
        raise ValueError(
            f"Unknown method(s): {', '.join(invalid)}. "
            f"Valid: {', '.join(sorted(_VALID_METHODS))}"
        )

    if len(methods) > 1:
        solo = [m for m in methods if m in _SOLO_ONLY]
        if solo:
            raise ValueError(
                f"'{solo[0]}' cannot be combined with other methods; "
                "use it alone or use 'keyword', 'semantic', or 'keyword,semantic'."
            )

    return methods


# ---------------------------------------------------------------------------
# Main search entry point
# ---------------------------------------------------------------------------


def search(
    query: str,
    *,
    top_k: int = 10,
    source: str | None = None,
    scope: str | None = None,
    metadata_filter: dict[str, str] | None = None,
    method: str = "keyword,semantic",
    recency: bool | None = None,
) -> list[dict[str, Any]] | str:
    """Execute an IR search and return structured results.

    Returns a list of result dicts on success, or an error string on failure.

    Each result dict contains:
        doc_id, score, source, display_text, metadata
        bm25_score?  (if method includes keyword)
        dense_score? (if method includes semantic)
        recency_weight?, raw_score? (if recency applied)
    """
    try:
        methods = _parse_methods(method)
    except ValueError as exc:
        return str(exc)

    # --- Recency config ---
    from work_buddy.config import load_config

    ir_cfg = load_config().get("ir", {})
    rec_cfg = ir_cfg.get("recency", {})
    do_recency = recency if recency is not None else rec_cfg.get("enabled", True)

    rec_hl = rec_cfg.get("half_life_days", 14)
    rec_floor = rec_cfg.get("floor", 0.15)

    log.debug("search: query=%r, method=%s, source=%s, scope=%s", query, method, source, scope)

    # --- Single method: substring ---
    if methods == ["substring"]:
        # !! DEADLOCK NOTE: ir.store imports sqlite3 (C extension).
        # This CAN deadlock via asyncio.to_thread(). It survives in
        # practice because sqlite3 is small and loads fast, but this
        # should be moved to the embedding service HTTP API eventually.
        from work_buddy.ir.store import substring_search

        results = substring_search(query, source=source, scope=scope,
                                   metadata_filter=metadata_filter, top_k=top_k)
        if do_recency:
            from work_buddy.ir.recency import apply_recency_bias

            results = apply_recency_bias(results, half_life_days=rec_hl, floor=rec_floor)
        return results

    # --- Single method: keyword or semantic ---
    from work_buddy.embedding.client import ir_search as _ir_search_client

    if methods == ["keyword"]:
        results = _ir_search_client(
            query, source=source, scope=scope, metadata_filter=metadata_filter,
            top_k=top_k, bm25_only=True,
        )
        if results is None:
            return "Error: embedding service unavailable. Start it or use method='substring'."
        if do_recency:
            from work_buddy.ir.recency import apply_recency_bias

            results = apply_recency_bias(results, half_life_days=rec_hl, floor=rec_floor)
        return results

    if methods == ["semantic"]:
        results = _ir_search_client(
            query, source=source, scope=scope, metadata_filter=metadata_filter,
            top_k=top_k, dense_only=True,
        )
        if results is None:
            return "Error: embedding service unavailable. Start it or use method='substring'."
        if do_recency:
            from work_buddy.ir.recency import apply_recency_bias

            results = apply_recency_bias(results, half_life_days=rec_hl, floor=rec_floor)
        return results

    # --- Multi-method: keyword,semantic (default) ---
    # NOTE: RRF fusion is inlined here to avoid importing ir.store,
    # which pulls in sqlite3 and can cause an import-lock deadlock
    # when dispatched via asyncio.to_thread() in the MCP server.

    all_results: dict[str, dict] = {}  # doc_id -> best result dict
    rankings: list[dict[str, float]] = []

    for m in methods:
        t0 = time.time()
        r = _ir_search_client(
            query,
            source=source,
            scope=scope,
            metadata_filter=metadata_filter,
            top_k=top_k,
            bm25_only=(m == "keyword"),
            dense_only=(m == "semantic"),
        )
        log.debug("  %s returned in %.2fs: %s results", m, time.time() - t0, len(r) if r else "None")
        if r is None:
            return (
                f"Error: embedding service unavailable for method '{m}'. "
                "Start it or use method='substring'."
            )
        ranking = {}
        for doc in r:
            doc_id = doc["doc_id"]
            ranking[doc_id] = doc["score"]
            if doc_id not in all_results:
                all_results[doc_id] = doc
        rankings.append(ranking)

    # Inline RRF fusion (avoids importing ir.store -> sqlite3)
    rrf_k = 60
    fused: dict[str, float] = {}
    for ranking in rankings:
        if not ranking:
            continue
        sorted_ids = sorted(ranking, key=ranking.get, reverse=True)
        for rank, doc_id in enumerate(sorted_ids, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)

    sorted_ids = sorted(fused, key=fused.get, reverse=True)[:top_k]

    results = []
    for doc_id in sorted_ids:
        doc = all_results[doc_id]
        results.append({**doc, "score": round(fused[doc_id], 4)})

    if do_recency:
        from work_buddy.ir.recency import apply_recency_bias

        results = apply_recency_bias(results, half_life_days=rec_hl, floor=rec_floor)

    return results
