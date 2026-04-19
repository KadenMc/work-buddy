"""Core retrieval engine — BM25 fielded scoring + RRF fusion.

Source-agnostic: operates on document dicts loaded from the store.
Dense scoring is handled by dense.py and fused here via RRF.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
from rank_bm25 import BM25Okapi

from work_buddy.ir.store import get_connection, load_documents, load_vectors, rrf_fuse
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Chunk-to-document score aggregation
# ---------------------------------------------------------------------------

def top_k_weighted_score(
    scores: list[float],
    weights: tuple[float, ...] = (0.6, 0.3, 0.1),
) -> float:
    """Aggregate chunk-level scores into a document score.

    Takes the top-k chunk scores (sorted descending) and computes a
    weighted mean. Emphasizes the best chunk while rewarding supporting
    evidence from secondary chunks.

    Args:
        scores: Chunk-level scores for a single document.
        weights: Weights for the top-ranked chunks in descending order.
            Need not sum to 1 — normalized internally. Length determines k.

    Returns:
        Aggregated document score.
    """
    x = np.asarray(scores, dtype=float)
    w = np.asarray(weights, dtype=float)

    if x.size == 0 or w.size == 0 or np.all(w == 0):
        return 0.0

    k_eff = min(w.size, x.size)
    topk = np.sort(x)[-k_eff:][::-1]
    w_eff = w[:k_eff]
    w_eff = w_eff / w_eff.sum()

    return float(np.dot(topk, w_eff))


# ---------------------------------------------------------------------------
# Tokenizer (shared with embedding/service.py)
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, filter short tokens."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1]


# ---------------------------------------------------------------------------
# BM25 fielded scoring
# ---------------------------------------------------------------------------

def bm25_score(
    query: str,
    docs: list[dict[str, Any]],
    field_weights: dict[str, float],
) -> dict[str, float]:
    """Score documents using per-field BM25 with weighted fusion.

    Args:
        query: Search query.
        docs: List of document dicts (must have 'doc_id' and 'fields' keys).
        field_weights: Mapping of field_name -> weight (e.g. {"user_text": 1.5}).

    Returns:
        {doc_id: normalized_score} dict, scores in [0, 1].
    """
    q_tokens = tokenize(query)
    if not q_tokens or not docs:
        return {}

    scores = np.zeros(len(docs))
    doc_ids = [d["doc_id"] for d in docs]

    for field_name, weight in field_weights.items():
        corpus = [tokenize(d["fields"].get(field_name, "")) for d in docs]

        # BM25Okapi needs non-empty corpus entries
        if not any(c for c in corpus):
            continue

        bm25 = BM25Okapi(corpus)
        raw = bm25.get_scores(q_tokens)
        scores += raw * weight

    # Normalize to [0, 1]
    max_score = scores.max()
    if max_score > 0:
        scores = scores / max_score

    return {doc_id: float(score) for doc_id, score in zip(doc_ids, scores) if score > 0}


# rrf_fuse is imported from store.py (pure Python, no numpy — safe for MCP server)


# ---------------------------------------------------------------------------
# Top-level search
# ---------------------------------------------------------------------------

def _get_field_weights(source: str | None) -> dict[str, float]:
    """Load BM25 field weights from config for a source."""
    from work_buddy.config import load_config
    cfg = load_config()
    ir_cfg = cfg.get("ir", {})

    if source:
        source_cfg = ir_cfg.get("sources", {}).get(source, {})
        weights = source_cfg.get("bm25_weights")
        if weights:
            return weights

    # Fallback: equal weight on all fields
    return {}


def search(
    query: str,
    *,
    source: str | None = None,
    scope: str | None = None,
    metadata_filter: dict[str, str] | None = None,
    top_k: int = 10,
    bm25_only: bool = False,
    dense_only: bool = False,
) -> list[dict[str, Any]]:
    """Query the IR index with hybrid BM25 + dense retrieval.

    Args:
        query: Search query text.
        source: Filter to a specific source (e.g. "conversation"). None = all.
        scope: Narrow to a specific item within a source. For conversations
            this is a session_id (or prefix); for Chrome tabs a tab_id, etc.
            Uses doc_id prefix matching (doc_ids are "{item_key}:{span_index}").
        metadata_filter: Filter by metadata JSON fields (e.g.
            ``{"project_name": "work-buddy"}``). Applied at the SQLite level
            via ``json_extract`` so BM25 only scores matching docs. Dense
            scoring still runs globally but non-matching docs are dropped
            during fusion (``docs_by_id`` gate).
        top_k: Maximum results to return.
        bm25_only: Skip dense retrieval (BM25 only).
        dense_only: Skip BM25 scoring (dense retrieval only).

    Returns:
        List of result dicts with doc_id, score, source, display_text, metadata.
    """
    if bm25_only and dense_only:
        raise ValueError("bm25_only and dense_only are mutually exclusive")

    from work_buddy.config import load_config
    cfg = load_config()

    conn = get_connection(cfg)
    docs = load_documents(conn, source=source, doc_id_prefix=scope,
                          metadata_filter=metadata_filter)
    conn.close()

    if not docs:
        return []

    # --- BM25 ---
    bm25_ranking: dict[str, float] = {}
    if not dense_only:
        field_weights = _get_field_weights(source)
        if not field_weights:
            # Auto-detect: equal weight for all fields present
            all_fields = set()
            for d in docs:
                all_fields.update(d["fields"].keys())
            field_weights = {f: 1.0 for f in all_fields}

        bm25_ranking = bm25_score(query, docs, field_weights)

    # --- Dense: one ranking per declared projection, or one legacy ranking. ---
    # Multi-projection sources (e.g. task_note) contribute one dense ranking
    # per projection; the legacy single-projection path contributes one
    # ranking from the unkeyed .npz. All dense rankings participate in RRF
    # alongside BM25 as independent signals.
    projection_rankings: dict[str, dict[str, float]] = {}
    legacy_dense_ranking: dict[str, float] = {}
    if not bm25_only:
        try:
            from work_buddy.ir.dense import score_query
            from work_buddy.ir.sources.base import get_projection_schema
            from work_buddy.ir.store import _get_source

            schema: dict[str, Any] = {}
            if source:
                try:
                    schema = get_projection_schema(_get_source(source))
                except Exception:
                    schema = {}

            if schema:
                for proj_key, spec in schema.items():
                    ranking = score_query(
                        query, cfg=cfg, source=source,
                        projection=proj_key, kind=spec.kind, pool=spec.pool,
                    )
                    if ranking:
                        projection_rankings[proj_key] = ranking
            else:
                legacy_dense_ranking = score_query(query, cfg=cfg, source=source)
        except Exception as exc:
            logger.debug("Dense scoring unavailable: %s", exc)

    # --- Fuse BM25 + every dense ranking (legacy or per-projection). ---
    rankings = [r for r in [bm25_ranking, legacy_dense_ranking] if r]
    rankings.extend(projection_rankings.values())
    if not rankings:
        return []

    if len(rankings) == 1:
        fused = rankings[0]
    else:
        rrf_k = cfg.get("ir", {}).get("rrf_k", 60)
        fused = rrf_fuse(rankings, k=rrf_k)

    # --- Rank and format ---
    docs_by_id = {d["doc_id"]: d for d in docs}
    sorted_ids = sorted(fused, key=fused.get, reverse=True)[:top_k]

    # Per-result diagnostics: keep the legacy ``dense_score`` meaning intact
    # (best dense signal the doc received — max across all dense rankings)
    # and surface per-projection scores when present.
    def _best_dense(did: str) -> float:
        scores = [legacy_dense_ranking.get(did, 0.0)]
        scores.extend(r.get(did, 0.0) for r in projection_rankings.values())
        return max(scores) if scores else 0.0

    results = []
    for doc_id in sorted_ids:
        doc = docs_by_id.get(doc_id)
        if not doc:
            continue
        entry = {
            "doc_id": doc_id,
            "score": round(fused[doc_id], 4),
            "bm25_score": round(bm25_ranking.get(doc_id, 0.0), 4),
            "dense_score": round(_best_dense(doc_id), 4),
            "source": doc["source"],
            "display_text": doc["display_text"],
            "metadata": doc["metadata"],
        }
        if projection_rankings:
            entry["projection_scores"] = {
                key: round(ranking.get(doc_id, 0.0), 4)
                for key, ranking in projection_rankings.items()
            }
        results.append(entry)

    return results


# ---------------------------------------------------------------------------
# Ad-hoc hybrid search (no index needed)
# ---------------------------------------------------------------------------

def search_against(
    query: str,
    candidates: list[str],
    *,
    top_k: int | None = None,
    threshold: float = 0.0,
    bm25_only: bool = False,
) -> list[dict]:
    """Hybrid BM25 + dense search over an ad-hoc list of strings.

    No index or pre-built store needed — scores the query against the
    candidate list inline. Useful for small candidate sets (commands,
    short lists) where building an index would be overkill.

    Args:
        query: Search query.
        candidates: List of candidate strings to rank.
        top_k: Max results to return. None = all above threshold.
        threshold: Minimum fused score to include (0-1).
        bm25_only: Skip dense scoring (faster, no embedding service needed).

    Returns:
        List of ``{"index": int, "text": str, "score": float}`` dicts,
        sorted by descending score.
    """
    if not query.strip() or not candidates:
        return []

    # Build pseudo-documents (single field per candidate)
    docs = [
        {"doc_id": str(i), "fields": {"text": c}}
        for i, c in enumerate(candidates)
    ]

    # BM25
    bm25_ranking = bm25_score(query, docs, {"text": 1.0})

    # Dense (optional)
    dense_ranking: dict[str, float] = {}
    if not bm25_only:
        try:
            from work_buddy.embedding.client import embed_for_ir
            q_vecs = embed_for_ir([query], role="query")
            c_vecs = embed_for_ir(candidates, role="document")
            if q_vecs and c_vecs:
                q_vec = np.array(q_vecs[0], dtype=np.float32)
                c_mat = np.array(c_vecs, dtype=np.float32)
                # Cosine similarity (vectors are typically normalized)
                norms = np.linalg.norm(c_mat, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                c_mat_normed = c_mat / norms
                q_norm = q_vec / (np.linalg.norm(q_vec) or 1.0)
                sims = c_mat_normed @ q_norm
                # Normalize to [0, 1]
                max_sim = sims.max()
                if max_sim > 0:
                    sims = sims / max_sim
                dense_ranking = {
                    str(i): float(s) for i, s in enumerate(sims) if s > 0
                }
        except Exception as exc:
            logger.debug("Dense scoring unavailable for search_against: %s", exc)

    # Fuse
    rankings = [r for r in [bm25_ranking, dense_ranking] if r]
    if not rankings:
        return []

    if len(rankings) == 1:
        fused = rankings[0]
    else:
        fused = rrf_fuse(rankings)

    # Rank, filter, return
    sorted_ids = sorted(fused, key=fused.get, reverse=True)
    if top_k:
        sorted_ids = sorted_ids[:top_k]

    results = []
    for doc_id in sorted_ids:
        score = fused[doc_id]
        if score < threshold:
            break  # Sorted descending, so all subsequent are below threshold
        idx = int(doc_id)
        results.append({
            "index": idx,
            "text": candidates[idx],
            "score": round(score, 4),
        })

    return results
