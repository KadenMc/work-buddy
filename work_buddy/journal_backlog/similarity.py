"""Embedding-fused similarity merge for journal segments.

Runs after the line-range LLM partition (``build_threads_from_line_ranges``)
and before sub-thread spawning. The line-range LLM occasionally over-splits
— a topic that should be one segment ends up as two — and the previous
mitigation (``merge_orphaned_continuations`` pinned to indented bullets)
relied on note-hygiene discipline that real users won't keep. This module
replaces that with a robust signal-fusion approach:

    fused = w_emb · embedding_cosine
          + w_tag · tag_jaccard
          + w_prox · position_proximity(sigma)

Pairs above ``threshold`` get merged greedily (highest score first; each
segment consumes into at most one merge). Survives a graceful degradation
path: if the embedding service is unavailable the embedding signal
collapses to zero and the merger falls back to tag + proximity only —
never an exception, never a blocked spawn.

## History

The original ``analyze_threads`` was deleted in commit 92d0473 with the
message "already migrated to work_buddy/ml/clustering.py" — but only the
math primitives migrated; the journal-specific orchestrator was lost.
That orchestrator embedded via Smart Connections (cold-start hazard,
384-d generic encoder). This rebuild uses the HTTP embedding service
(``work_buddy.embedding.client.embed_for_ir``, port 5124, 768-d
``mdbr-leaf-ir-asym``, retrieval-tuned, no cold-start) which Sonnet's
2026-05-03 sanity-check confirmed is the better fit for short-segment
similarity.

## Tuning

The fusion weights ``{embedding: 0.55, tag: 0.35, proximity: 0.10}`` and
``sigma=0.2`` are inherited from the recovered file's docstring (claimed
6/6 accuracy on a 35-thread eval that no longer exists in the repo). They
are **starting hypotheses only** — the original eval set is gone, and
they were tuned against Smart Connections' 384-d space. If real-world
mis-merges or fail-to-merges show up on this user's vault, re-tune.

## Caching

Repeat scans on the same journal day will largely embed the same text.
A small content-hash → vector cache lives in ``cache/journal-similarity/``
(npz format, mirrors ``work_buddy.knowledge.persistence``'s pattern).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from work_buddy.logging_config import get_logger
from work_buddy.ml.clustering import (
    compute_pairwise_similarity,
    suggest_merges,
)
from work_buddy.paths import resolve

logger = get_logger(__name__)


# Tuned defaults inherited from the recovered (deleted) file. These are
# starting hypotheses against the new 768-d retrieval-tuned encoder, NOT
# re-validated weights — see module docstring.
DEFAULT_WEIGHTS: dict[str, float] = {
    "embedding": 0.55,
    "tag": 0.35,
    "proximity": 0.10,
}
DEFAULT_SIGMA: float = 0.2
DEFAULT_THRESHOLD: float = 0.55

# Encoder identity. If this changes, bump CACHE_VERSION too — vectors from
# different models live in different spaces and must not mix.
_EMBED_MODEL_KEY = "leaf-ir-document"
_EMBED_DIM = 768

# Cache file format version. Bump when the hash inputs or vector
# normalisation scheme change.
CACHE_VERSION = 1
_HASH_LEN = 16


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------


_INLINE_TAG_PREFIXES = ("#wb/", "#paper/", "#projects/", "#admin/", "#")


def extract_inline_tags(raw_text: str) -> list[str]:
    """Pull ``#tag`` tokens from a journal segment's raw text.

    The v5 spawn pipeline runs BEFORE the manifest step that would otherwise
    produce LLM-generated tags, so we don't have a tag list pre-merge. Inline
    Obsidian-style tags (``#wb/TODO``, ``#paper/foo``, ``#projects/bar``,
    plus the bare ``#tag`` form) are a cheap categorical signal that makes
    "two segments mention the same project / TODO topic" a strong fusion
    contributor when the embedding signal alone is ambiguous.

    Returns deduped, lowercase tags. Strips the leading ``#`` so the caller
    sees ``["wb/todo", "paper/ecg-classifier"]``.
    """
    if not raw_text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in raw_text.split():
        if not token.startswith("#") or len(token) < 2:
            continue
        # Trim trailing punctuation a markdown line might attach
        # (e.g. ``#wb/TODO,`` or ``#wb/TODO.``).
        tag = token.lstrip("#").rstrip(",.;:!?)\"'")
        if not tag:
            continue
        tag = tag.lower()
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


# ---------------------------------------------------------------------------
# Cache I/O — tiny content-hash → vector store
# ---------------------------------------------------------------------------


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LEN]


def _cache_path() -> Path:
    return resolve("cache/journal-similarity-embeddings")


def _load_cache() -> dict[str, list[float]]:
    """Load ``{content_hash: vector}`` from disk.

    Returns an empty dict on any failure path — the caller's contract is
    that a missing/corrupt cache means "embed everything fresh," never an
    exception. Header mismatches (different model_key or version) also
    treat the cache as cold.
    """
    try:
        import numpy as np
    except ImportError:
        return {}

    path = _cache_path().with_suffix(".npz")
    if not path.exists():
        return {}

    try:
        with np.load(path, allow_pickle=True) as data:
            cache_model = (
                str(data["model_key"]) if "model_key" in data else ""
            )
            cache_version = (
                int(data["version"]) if "version" in data else 0
            )
            if cache_model != _EMBED_MODEL_KEY or cache_version != CACHE_VERSION:
                logger.info(
                    "Journal-similarity cache header mismatch "
                    "(model=%r vs %r, version=%d vs %d). Treating as empty.",
                    cache_model, _EMBED_MODEL_KEY,
                    cache_version, CACHE_VERSION,
                )
                return {}
            hashes = data["hashes"].tolist()
            vectors = data["vectors"].astype(np.float32)
        if len(hashes) != vectors.shape[0]:
            logger.warning(
                "Journal-similarity cache shape mismatch (hashes=%d, "
                "vectors=%d). Treating as empty.",
                len(hashes), vectors.shape[0],
            )
            return {}
        return {h: vectors[i].tolist() for i, h in enumerate(hashes)}
    except Exception as e:
        logger.warning(
            "Failed to load journal-similarity cache (%s). Treating as empty.",
            e,
        )
        return {}


def _save_cache(cache: dict[str, list[float]]) -> None:
    """Persist ``{content_hash: vector}`` atomically.

    Best-effort: failures are logged and swallowed. The merge path runs
    inline with thread-spawn; we do not let cache I/O crash the spawn.
    """
    try:
        import numpy as np
    except ImportError:
        return

    path = _cache_path().with_suffix(".npz")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        hashes_arr = np.array(list(cache.keys()), dtype=object)
        if cache:
            vectors_arr = np.stack(
                [np.asarray(v, dtype=np.float16) for v in cache.values()]
            )
        else:
            vectors_arr = np.zeros((0, _EMBED_DIM), dtype=np.float16)
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(
            tmp,
            hashes=hashes_arr,
            vectors=vectors_arr,
            model_key=np.array(_EMBED_MODEL_KEY),
            version=np.array(CACHE_VERSION),
        )
        tmp.replace(path)
    except Exception as e:
        logger.warning(
            "Failed to save journal-similarity cache (%s).", e,
        )


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def embed_segments(
    raw_texts: list[str],
    *,
    cache: Optional[dict[str, list[float]]] = None,
) -> list[Optional[list[float]]]:
    """Embed each segment's raw text via the HTTP embedding service.

    Returns a parallel list of vectors, with ``None`` for any segment whose
    embedding could not be produced (e.g. service down for the whole batch).
    The merge path treats ``None`` as "no embedding signal for this segment"
    — the segment still participates via the tag and proximity signals.

    Honours and updates ``cache`` if provided: pre-embedded segments hit the
    cache directly; new segments are batched into a single service call.

    Args:
        raw_texts: parallel to the caller's segment list.
        cache: ``{content_hash: vector}`` from ``_load_cache()``. Mutated
            in place when new vectors are added; the caller is responsible
            for persisting via ``_save_cache(cache)`` after the run.
    """
    if not raw_texts:
        return []

    if cache is None:
        cache = {}

    n = len(raw_texts)
    out: list[Optional[list[float]]] = [None] * n
    miss_indices: list[int] = []
    miss_hashes: list[str] = []
    miss_texts: list[str] = []

    for i, text in enumerate(raw_texts):
        h = _content_hash(text)
        cached = cache.get(h)
        if cached is not None:
            out[i] = cached
            continue
        miss_indices.append(i)
        miss_hashes.append(h)
        miss_texts.append(text)

    if not miss_texts:
        return out

    # Single batch call. The HTTP client returns None on service-unavailable
    # (graceful — no exception). We mirror the clarify/cluster fallback:
    # leave the missing entries as None and let the caller blend tags +
    # proximity without the embedding signal.
    try:
        from work_buddy.embedding.client import embed_for_ir
        vectors = embed_for_ir(miss_texts, role="document")
    except Exception as e:  # defensive — embed_for_ir already swallows most
        logger.warning(
            "embed_for_ir raised on journal-similarity batch (%s); "
            "merger will fall back to tag + proximity.", e,
        )
        vectors = None

    if vectors is None:
        logger.info(
            "Embedding service unavailable for %d journal segments; "
            "similarity merge will use tag + proximity only.",
            len(miss_texts),
        )
        return out

    if len(vectors) != len(miss_texts):
        logger.warning(
            "Embedding service returned %d vectors for %d inputs; "
            "discarding mismatched batch and falling back to tag + proximity.",
            len(vectors), len(miss_texts),
        )
        return out

    for idx, h, vec in zip(miss_indices, miss_hashes, vectors):
        out[idx] = vec
        cache[h] = vec

    return out


# ---------------------------------------------------------------------------
# Merge planning
# ---------------------------------------------------------------------------


def _segment_to_item(
    segment: dict[str, Any],
) -> dict[str, Any]:
    """Adapt a journal-segment dict into the ``ml.clustering`` item shape.

    ``ml.clustering.compute_pairwise_similarity`` reads ``id`` and
    ``tags``. Journal segments out of ``build_threads_from_line_ranges``
    carry ``id`` already; we extract inline tags from ``raw_text`` since
    the manifest step (which would produce LLM tags) hasn't run yet.
    """
    return {
        "id": segment["id"],
        "tags": extract_inline_tags(segment.get("raw_text", "")),
        # ``summary`` is referenced by ml.clustering's reporters when they
        # exist; we pass through raw_text as a stand-in so any future
        # logging surface has something readable.
        "summary": segment.get("raw_text", "")[:200],
    }


def plan_merges(
    segments: list[dict[str, Any]],
    *,
    weights: Optional[dict[str, float]] = None,
    sigma: float = DEFAULT_SIGMA,
    threshold: float = DEFAULT_THRESHOLD,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Compute pairwise similarity and produce a greedy merge plan.

    This is a **plan**, not an apply. The caller decides whether to apply
    the merges (e.g. only when confidence is high, or surface as
    suggestions for user review).

    Args:
        segments: list of dicts from ``build_threads_from_line_ranges``.
            Each must have ``id`` and ``raw_text``.
        weights: override the default fusion weights.
        sigma: positional decay for the proximity signal.
        threshold: minimum fused score for a pair to be a merge candidate.
        use_cache: if False, skip the disk cache (useful in tests).

    Returns:
        ``{
            "merges": [{"ids": [a, b], "fused_score": float, ...}, ...],
            "pair_count": int,
            "embed_status": "ok" | "service_unavailable" | "partial",
            "embedded": int,
            "skipped": int,
        }``
    """
    if len(segments) < 2:
        return {
            "merges": [],
            "pair_count": 0,
            "embed_status": "ok",
            "embedded": 0,
            "skipped": 0,
        }

    cache = _load_cache() if use_cache else {}
    raw_texts = [s.get("raw_text", "") for s in segments]
    embeddings = embed_segments(raw_texts, cache=cache)

    # Normalise: ml.clustering.cosine_similarity expects parallel lists.
    # For segments where embedding failed, substitute a zero vector — its
    # cosine similarity to every other vector is 0, so the embedding
    # signal collapses to zero for those pairs and the merger falls back
    # to tag + proximity. Matches clarify/cluster.py:58-60.
    embedded = sum(1 for v in embeddings if v is not None)
    skipped = len(embeddings) - embedded
    if embedded == 0:
        embed_status = "service_unavailable"
    elif skipped == 0:
        embed_status = "ok"
    else:
        embed_status = "partial"

    safe_embeddings: list[list[float]] = [
        v if v is not None else [0.0] * _EMBED_DIM
        for v in embeddings
    ]

    items = [_segment_to_item(s) for s in segments]

    pairs = compute_pairwise_similarity(
        items,
        safe_embeddings,
        weights=weights or DEFAULT_WEIGHTS,
        sigma=sigma,
    )
    merges = suggest_merges(pairs, items, threshold=threshold)

    if use_cache and embedded > 0:
        _save_cache(cache)

    logger.info(
        "Journal similarity: %d segments -> %d pairs, %d merges "
        "(embed_status=%s, embedded=%d, skipped=%d, threshold=%.2f)",
        len(segments), len(pairs), len(merges),
        embed_status, embedded, skipped, threshold,
    )

    return {
        "merges": merges,
        "pair_count": len(pairs),
        "embed_status": embed_status,
        "embedded": embedded,
        "skipped": skipped,
    }


def apply_merges(
    segments: list[dict[str, Any]],
    merges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply a merge plan to a segment list.

    Each merge collapses two segments into one whose ``raw_text`` is the
    concatenation of the originals (separated by a newline, in original
    list order to preserve reading flow), ``line_count`` is the sum, and
    ``has_multi_flag`` becomes True if either input had it set. The merged
    segment keeps the **first** input's ``id`` so any downstream caller
    that already keyed off the segment id (e.g. cleanup adapter line_text
    matching) keeps working without a remap.

    Segments not referenced by any merge pass through untouched.

    Args:
        segments: original segment list (from
            ``build_threads_from_line_ranges``).
        merges: output of :func:`plan_merges`'s ``merges`` field. Each
            entry has ``ids: [str, str]``.

    Returns:
        New list with merges applied. Order preserved by the position of
        the first segment in each merged group.
    """
    if not merges:
        return list(segments)

    by_id = {s["id"]: s for s in segments}
    consumed: set[str] = set()
    output: list[dict[str, Any]] = []

    # Map id → merge entry (each id appears in at most one merge by
    # ml.clustering.suggest_merges' greedy contract).
    merge_of: dict[str, dict[str, Any]] = {}
    for m in merges:
        for tid in m.get("ids", []):
            merge_of[tid] = m

    for segment in segments:
        sid = segment["id"]
        if sid in consumed:
            continue
        merge = merge_of.get(sid)
        if merge is None:
            output.append(segment)
            continue
        ids = merge["ids"]
        # Preserve original list order when concatenating raw_text:
        # walk the segment list in order, pull the ones in this merge.
        members = [by_id[i] for i in ids if i in by_id]
        members.sort(key=lambda s: segments.index(s))
        if not members:
            continue
        merged_raw = "\n".join(m.get("raw_text", "") for m in members)
        merged_lines = sum(int(m.get("line_count", 0) or 0) for m in members)
        merged_multi = any(bool(m.get("has_multi_flag")) for m in members)
        # Source dates: union, preserving order of first appearance.
        seen_dates: set[str] = set()
        merged_dates: list[str] = []
        for m in members:
            for d in m.get("source_dates", []) or []:
                if d not in seen_dates:
                    seen_dates.add(d)
                    merged_dates.append(d)
        # Keep the first member's id so any external key references stay valid.
        merged = dict(members[0])
        merged["raw_text"] = merged_raw
        merged["line_count"] = merged_lines
        merged["has_multi_flag"] = merged_multi
        merged["source_dates"] = merged_dates
        # Annotate so downstream consumers can audit what happened.
        merged["merged_from"] = list(ids)
        merged["merge_score"] = float(merge.get("fused_score", 0.0))
        output.append(merged)
        for i in ids:
            consumed.add(i)

    return output


def merge_segments(
    segments: list[dict[str, Any]],
    *,
    weights: Optional[dict[str, float]] = None,
    sigma: float = DEFAULT_SIGMA,
    threshold: float = DEFAULT_THRESHOLD,
    use_cache: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convenience: plan + apply in one call. Returns (merged_segments, plan_meta).

    The plan_meta is the dict from :func:`plan_merges` (without the
    ``merges`` field, which has been applied) — useful for audit logging.
    """
    plan = plan_merges(
        segments,
        weights=weights,
        sigma=sigma,
        threshold=threshold,
        use_cache=use_cache,
    )
    merged = apply_merges(segments, plan["merges"])
    meta = {k: v for k, v in plan.items() if k != "merges"}
    meta["applied_merges"] = len(plan["merges"])
    meta["before_count"] = len(segments)
    meta["after_count"] = len(merged)
    return merged, meta
