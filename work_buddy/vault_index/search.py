"""Hybrid (lexical ⊕ dense, RRF-fused) search over the vault chunk index.

Mirrors ``ir/search.py``: a lexical signal (FTS5 bm25) and a dense signal (cosine over
the resident vector matrix), fused via Reciprocal Rank Fusion. Degrades to lexical-only
when the embedding service is unavailable. Returns IR-compatible result dicts so the
surface formatters render them unchanged.
"""
from __future__ import annotations

import json
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.vault_index import dense_cache, store

logger = get_logger(__name__)

# Pull a wider candidate pool than top_k into the fusion so a doc strong on one
# signal but absent from the other's truncated list still competes.
_POOL_MULTIPLIER = 5
_POOL_FLOOR = 50


def search(
    query: str,
    *,
    top_k: int = 10,
    method: str = "hybrid",
    vault_id: str | None = None,
    recency: bool = False,
    cfg: dict | None = None,
) -> list[dict[str, Any]]:
    """Search the vault index. ``method`` ∈ {"hybrid", "lexical", "dense"}.

    Returns IR-compatible result dicts: ``doc_id, score, bm25_score, dense_score,
    source="vault_index", display_text, metadata``. Degrades to lexical-only if the
    embedding service is down; returns ``[]`` for an empty query or empty index.
    """
    query = (query or "").strip()
    if not query:
        return []

    pool = max(top_k * _POOL_MULTIPLIER, _POOL_FLOOR)
    lexical: dict[str, float] = {}
    dense: dict[str, float] = {}

    if method in ("hybrid", "lexical"):
        conn = store.get_connection(cfg)
        try:
            lexical = store.search_lexical(conn, query, vault_id=vault_id, top_k=pool)
        finally:
            conn.close()

    if method in ("hybrid", "dense"):
        dense = _dense_scores(query, cfg=cfg, vault_id=vault_id, top_k=pool)

    rankings = [r for r in (lexical, dense) if r]
    if not rankings:
        return []

    if len(rankings) == 1:
        fused = rankings[0]
    else:
        from work_buddy.ir.store import rrf_fuse
        fused = rrf_fuse(rankings)

    top_ids = sorted(fused, key=fused.get, reverse=True)[:top_k]
    results = _hydrate(top_ids, fused, lexical, dense, cfg=cfg)

    if recency and results:
        from work_buddy.ir.recency import apply_recency_bias
        apply_recency_bias(results)

    return results


def _dense_scores(
    query: str, *, cfg: dict | None, vault_id: str | None, top_k: int
) -> dict[str, float]:
    """Dense cosine ranking, or ``{}`` if the embedding service / index is unavailable."""
    # ``encode_query`` is ``_IN_SERVICE``-aware: in the embedding-service process it
    # encodes against the loaded model directly (no HTTP self-call); elsewhere it
    # round-trips to the service. Returns a (1, D) float32 array, or None when the
    # service is down — in which case the caller degrades to lexical-only.
    from work_buddy.ir.dense import encode_query, score_dense

    q = encode_query(query)
    if q is None:
        return {}
    loaded = dense_cache.get_matrix(cfg)
    if loaded is None:
        return {}
    matrix, doc_ids = loaded

    scores = score_dense(q, matrix, doc_ids)

    if vault_id is not None:
        allowed = _vault_doc_ids(cfg, vault_id)
        scores = {d: s for d, s in scores.items() if d in allowed}

    if len(scores) > top_k:
        keep = sorted(scores, key=scores.get, reverse=True)[:top_k]
        scores = {d: scores[d] for d in keep}
    return scores


def _vault_doc_ids(cfg: dict | None, vault_id: str) -> set[str]:
    conn = store.get_connection(cfg)
    try:
        return {
            r["doc_id"]
            for r in conn.execute(
                "SELECT doc_id FROM chunks WHERE vault_id = ?", (vault_id,)
            )
        }
    finally:
        conn.close()


def _hydrate(
    top_ids: list[str],
    fused: dict[str, float],
    lexical: dict[str, float],
    dense: dict[str, float],
    *,
    cfg: dict | None,
) -> list[dict[str, Any]]:
    if not top_ids:
        return []
    conn = store.get_connection(cfg)
    try:
        placeholders = ",".join("?" * len(top_ids))
        rows = conn.execute(
            f"SELECT doc_id, source_path, heading_path, vault_id, chunk_key, "
            f"text, line_start FROM chunks WHERE doc_id IN ({placeholders})",
            top_ids,
        ).fetchall()
    finally:
        conn.close()

    by_id = {r["doc_id"]: r for r in rows}
    results: list[dict[str, Any]] = []
    for doc_id in top_ids:  # preserve fused rank order
        r = by_id.get(doc_id)
        if r is None:
            continue
        results.append({
            "doc_id": doc_id,
            "score": round(fused.get(doc_id, 0.0), 4),
            "bm25_score": round(lexical.get(doc_id, 0.0), 4),
            "dense_score": round(dense.get(doc_id, 0.0), 4),
            "source": "vault_index",
            "display_text": (r["text"] or "")[:300],
            "metadata": {
                "source_path": r["source_path"],
                "heading_path": json.loads(r["heading_path"]),
                "vault_id": r["vault_id"],
                "chunk_key": r["chunk_key"],
                "line_start": r["line_start"],
            },
        })
    return results
