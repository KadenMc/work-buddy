"""Dense retrieval scoring via the embedding service.

Query encoding uses the lightweight ``leaf-ir-query`` model
(MongoDB/mdbr-leaf-ir, 90 MB, eager-loaded at startup).  Document encoding
uses the full asymmetric bundle ``leaf-ir`` (MongoDB/mdbr-leaf-ir-asym,
526 MB, lazy-loaded only during indexing).  Both produce compatible 768-d
vectors per the model card's documented split-usage pattern.

When running inside the embedding service process (``_IN_SERVICE = True``),
encoding calls the model directly instead of making an HTTP round-trip to
itself.  The flag is set by ``service.main()`` at startup.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from work_buddy.embedding.client import embed_for_ir, is_available
from work_buddy.ir.store import load_vectors, save_vectors, load_documents, get_connection
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Set to True by embedding/service.py when this module runs in-process.
# Skips HTTP self-calls and uses the model registry directly.
_IN_SERVICE: bool = False


def encode_query(query: str, kind: str = "passage") -> np.ndarray | None:
    """Encode a search query for the given projection kind.

    - ``kind="passage"`` (default): asymmetric query encoder ``leaf-ir-query``
      (768-d). Pairs with passage-side vectors encoded via ``leaf-ir``.
    - ``kind="label"``: symmetric encoder ``leaf-mt`` (1024-d). Same model
      on both sides, for peer-shaped short-text matching.

    Returns (1, D) float32 array, or None if service unavailable.
    """
    if _IN_SERVICE:
        return _encode_query_direct(query, kind=kind)
    if kind == "label":
        from work_buddy.embedding.client import embed
        vectors = embed([query], model="leaf-mt")
    else:
        vectors = embed_for_ir([query], role="query")
    if vectors is None:
        return None
    return np.array(vectors, dtype=np.float32)


def _encode_query_direct(query: str, kind: str = "passage") -> np.ndarray | None:
    """Encode query in-process using the service's loaded model."""
    try:
        from work_buddy.embedding.service import _get_model
        if kind == "label":
            model = _get_model("leaf-mt")
            vec = model.encode([query], show_progress_bar=False)
        else:
            model = _get_model("leaf-ir-query")
            vec = model.encode([query], prompt_name="query", show_progress_bar=False)
        return np.array(vec, dtype=np.float32)
    except Exception as exc:
        logger.warning("In-service query encoding failed (kind=%s): %s", kind, exc)
        return None


def encode_documents(
    texts: list[str],
    batch_size: int = 32,
    progress: bool = True,
    kind: str = "passage",
) -> np.ndarray | None:
    """Encode document texts via the embedding service.

    - ``kind="passage"`` (default): ``leaf-ir`` asymmetric document encoder.
    - ``kind="label"``: ``leaf-mt`` symmetric encoder.

    Batches requests to avoid HTTP timeouts on large corpora.
    Returns (N, D) float32 array, or None if service unavailable.
    """
    import sys

    def _encode_batch(batch: list[str]):
        if kind == "label":
            from work_buddy.embedding.client import embed
            return embed(batch, model="leaf-mt")
        return embed_for_ir(batch, role="document")

    all_vectors: list[list[float]] = []
    total = len(texts)
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        # Retry once on failure (first batch may trigger lazy model load)
        vectors = _encode_batch(batch)
        if vectors is None:
            import time
            logger.warning("Batch %d failed, retrying in 5s (may be lazy loading)...",
                           i // batch_size + 1)
            time.sleep(5)
            vectors = _encode_batch(batch)
        if vectors is None:
            logger.error("Encoding failed at batch %d/%d (kind=%s)",
                         i // batch_size + 1,
                         (total + batch_size - 1) // batch_size, kind)
            return None
        all_vectors.extend(vectors)
        if progress:
            done = min(i + batch_size, total)
            print(f"\r  Encoded {done}/{total} documents...", end="", file=sys.stderr)

    if progress:
        print(file=sys.stderr)  # newline after progress

    return np.array(all_vectors, dtype=np.float32)


def score_dense(
    query_vec: np.ndarray,
    doc_vectors: np.ndarray,
    doc_ids: list[str],
    pool: str = "none",
) -> dict[str, float]:
    """Cosine similarity between query vector and document vectors.

    Args:
        query_vec: (1, D) or (D,) query vector.
        doc_vectors: (N, D) document vectors.
        doc_ids: Parallel list of doc IDs. For ``pool != "none"`` a single
            doc_id may appear multiple times — one row per sub-vector.
        pool: Aggregation when a doc_id has multiple vectors.
            - ``"none"``: one-to-one with doc_ids (default).
            - ``"max"``: per-doc maximum similarity.
            - ``"mean"``: per-doc mean similarity.

    Returns:
        {doc_id: score} dict, scores in [0, 1] after max-normalization.
    """
    if query_vec.ndim == 2:
        query_vec = query_vec[0]

    q_norm = np.linalg.norm(query_vec)
    if q_norm == 0:
        return {}

    d_norms = np.linalg.norm(doc_vectors, axis=1)
    mask = d_norms > 0
    sims = np.zeros(len(doc_ids))
    sims[mask] = np.dot(doc_vectors[mask], query_vec) / (d_norms[mask] * q_norm)

    # Aggregate across repeated doc_ids for pooled projections. We pool
    # BEFORE the [0, 1] max-normalisation so that normalization reflects
    # the per-doc score space the caller will actually see.
    if pool == "none":
        per_doc = dict(zip(doc_ids, sims))
    else:
        per_doc: dict[str, float] = {}
        counts: dict[str, int] = {}
        for did, s in zip(doc_ids, sims):
            if pool == "max":
                prev = per_doc.get(did)
                if prev is None or s > prev:
                    per_doc[did] = float(s)
            elif pool == "mean":
                per_doc[did] = per_doc.get(did, 0.0) + float(s)
                counts[did] = counts.get(did, 0) + 1
            else:
                raise ValueError(f"Unknown pool strategy: {pool!r}")
        if pool == "mean":
            per_doc = {did: total / counts[did] for did, total in per_doc.items()}

    if not per_doc:
        return {}

    max_sim = max(per_doc.values())
    if max_sim <= 0:
        return {}

    return {
        did: float(score / max_sim)
        for did, score in per_doc.items()
        if score > 0
    }


def score_query(
    query: str,
    cfg: dict | None = None,
    source: str | None = None,
    projection: str | None = None,
    kind: str = "passage",
    pool: str = "none",
) -> dict[str, float]:
    """Full dense scoring pipeline for one projection.

    ``projection=None`` hits the legacy single-projection vectors at
    ``work_buddy_ir.<source>.npz`` (kind is still used to pick the query
    encoder — default "passage" matches the historical asymmetric setup).

    ``projection="<name>"`` loads that projection's vectors from
    ``work_buddy_ir.<source>.<projection>.npz`` and aggregates per ``pool``.

    Returns empty dict if the embedding service is unavailable or no
    vectors exist at the requested path.
    """
    if not _IN_SERVICE and not is_available():
        logger.debug("Embedding service not available for dense scoring")
        return {}

    query_vec = encode_query(query, kind=kind)
    if query_vec is None:
        return {}

    if source:
        vdata = load_vectors(cfg, source=source, projection=projection)
    else:
        # Merge vectors across all sources (legacy single-projection only)
        vdata = _load_all_vectors(cfg)

    if vdata is None:
        logger.debug(
            "No vector file for dense scoring (source=%s, projection=%s)",
            source, projection,
        )
        return {}

    doc_vectors, doc_ids = vdata
    return score_dense(query_vec, doc_vectors, doc_ids, pool=pool)


def _load_all_vectors(cfg: dict | None = None) -> tuple[np.ndarray, list[str]] | None:
    """Load and merge vectors from all per-source .npz files."""
    from work_buddy.ir.store import get_connection, _npz_path

    conn = get_connection(cfg)
    sources = [row["source"] for row in conn.execute(
        "SELECT DISTINCT source FROM documents"
    ).fetchall()]
    conn.close()

    all_vecs = []
    all_ids: list[str] = []
    for src in sources:
        vdata = load_vectors(cfg, source=src)
        if vdata is not None:
            vecs, ids = vdata
            all_vecs.append(vecs)
            all_ids.extend(ids)

    if not all_vecs:
        return None
    return np.vstack(all_vecs), all_ids


def _encode_bulk_direct(
    texts: list[str],
    batch_size: int = 32,
    kind: str = "passage",
) -> np.ndarray:
    """Encode documents in-process (no HTTP).

    When running inside the embedding service (``_IN_SERVICE``), reuses the
    already-loaded models from the service registry. Otherwise loads a
    fresh SentenceTransformer for standalone / CLI usage.

    ``kind="passage"`` uses ``leaf-ir`` (asymmetric document encoder),
    ``kind="label"`` uses ``leaf-mt`` (symmetric).
    """
    import sys

    model_key = "leaf-mt" if kind == "label" else "leaf-ir"
    # leaf-mt encodes without a prompt; leaf-ir uses the "document" prompt.
    prompt = None if kind == "label" else "document"

    if _IN_SERVICE:
        from work_buddy.embedding.service import _get_model
        model = _get_model(model_key)  # may trigger lazy load on first call
        logger.info("Using in-service %s model for bulk encoding (kind=%s)",
                    model_key, kind)
    else:
        from work_buddy.config import load_config
        cfg = load_config()
        default_hf = "MongoDB/mdbr-leaf-mt" if kind == "label" else "MongoDB/mdbr-leaf-ir-asym"
        model_name = cfg.get("embedding", {}).get("models", {}).get(
            model_key, {}
        ).get("name", default_hf)
        from sentence_transformers import SentenceTransformer
        logger.info("Loading %s for bulk encoding (kind=%s)...", model_name, kind)
        model = SentenceTransformer(model_name)

    all_vecs = []
    total = len(texts)
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        encode_kwargs = {"batch_size": batch_size, "show_progress_bar": False}
        if prompt is not None:
            encode_kwargs["prompt_name"] = prompt
        vecs = model.encode(batch, **encode_kwargs)
        all_vecs.append(vecs)
        done = min(i + batch_size, total)
        print(f"\r  Encoded {done}/{total} documents...", end="", file=sys.stderr)

    print(file=sys.stderr)
    return np.vstack(all_vecs)


def build_vectors(
    source: str | None = None,
    cfg: dict | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Encode documents and save vectors to .npz (incremental).

    Dispatches on the source's projection schema:

    - **Legacy single-projection sources** (no schema declared): encode
      ``dense_text`` once as kind=passage, save to
      ``work_buddy_ir.<source>.npz``. This is the historical behaviour
      preserved verbatim — conversation / docs / chrome / projects all
      flow through here with no visible change.

    - **Multi-projection sources** (schema non-empty): for every declared
      projection, pull the per-doc ``projections[key].text`` out of the
      SQLite store, encode with the spec's kind, and save to
      ``work_buddy_ir.<source>.<key>.npz``. Pooled projections (list
      text) are flattened into multi-row storage; aggregation happens at
      query time in ``score_dense``.

    Incremental: unchanged doc_ids are not re-encoded. ``force=True``
    drops the existing vectors and re-encodes everything.

    Returns stats dict — for multi-projection sources this includes a
    per-projection breakdown under ``"projections"``.
    """
    import time

    if cfg is None:
        from work_buddy.config import load_config
        cfg = load_config()

    conn = get_connection(cfg)
    docs = load_documents(conn, source=source)
    conn.close()

    if not docs:
        return {"doc_count": 0, "status": "no_documents"}

    # Discover the source's projection schema (empty → legacy path).
    schema: dict[str, Any] = {}
    if source:
        try:
            from work_buddy.ir.store import _get_source
            from work_buddy.ir.sources.base import get_projection_schema
            schema = get_projection_schema(_get_source(source))
        except Exception as exc:
            logger.debug("No projection schema for source=%s: %s", source, exc)
            schema = {}

    if not schema:
        # Legacy single-projection path. Single .npz, kind=passage, no pooling.
        return _build_vectors_for_projection(
            docs=docs,
            cfg=cfg,
            source=source,
            projection=None,
            kind="passage",
            pool="none",
            force=force,
        )

    # Multi-projection path: one .npz per declared projection.
    t0 = time.time()
    per_proj_stats: dict[str, dict[str, Any]] = {}
    total_new = 0
    for proj_key, spec in schema.items():
        stats = _build_vectors_for_projection(
            docs=docs,
            cfg=cfg,
            source=source,
            projection=proj_key,
            kind=spec.kind,
            pool=spec.pool,
            force=force,
        )
        per_proj_stats[proj_key] = stats
        total_new += stats.get("docs_new", 0)

    return {
        "doc_count": len(docs),
        "docs_new": total_new,
        "projections": per_proj_stats,
        "encode_time_s": round(time.time() - t0, 1),
    }


def _build_vectors_for_projection(
    docs: list[dict[str, Any]],
    cfg: dict,
    source: str | None,
    projection: str | None,
    kind: str,
    pool: str,
    force: bool,
) -> dict[str, Any]:
    """Encode one projection's vectors for a set of loaded docs.

    Shared by the legacy (``projection=None``) and multi-projection
    (``projection="<name>"``) paths — the code diverges only on where it
    gets each doc's dense text.
    """
    import time

    # Per-doc text lookup. Pool="none" yields one string per doc;
    # pool in {"max", "mean"} yields a list (stored multi-row).
    def _text_for(doc: dict[str, Any]) -> str | list[str] | None:
        if projection is None:
            return doc.get("dense_text") or None
        proj = doc.get("projections", {}).get(projection)
        if not proj:
            return None
        text = proj.get("text")
        if text is None:
            return None
        if pool == "none" and not isinstance(text, str):
            # Fell through with a list when the spec says scalar — skip
            # defensively rather than mis-encode.
            logger.warning(
                "Projection %s/%s expected scalar text (pool=none), got list; skipping doc %s",
                source, projection, doc["doc_id"],
            )
            return None
        if pool != "none" and isinstance(text, str):
            # Source author passed a single string for a pooled projection;
            # treat it as a singleton list to keep semantics consistent.
            return [text]
        return text

    # Flatten doc_id → text pairs. For pooled projections a doc_id may
    # appear multiple times — that's intentional, one row per sub-vector.
    all_doc_ids: list[str] = []
    all_texts: list[str] = []
    for d in docs:
        t = _text_for(d)
        if t is None:
            continue
        if isinstance(t, list):
            for sub in t:
                if sub and sub.strip():
                    all_doc_ids.append(d["doc_id"])
                    all_texts.append(sub)
        else:
            if t.strip():
                all_doc_ids.append(d["doc_id"])
                all_texts.append(t)

    if not all_texts:
        return {"doc_count": 0, "status": "no_documents"}

    # Incremental: keep vectors whose doc_id is still present; encode the rest.
    # For pooled projections we re-encode whenever a doc_id isn't fully
    # represented in the existing file — this keeps the code simple at
    # the cost of re-encoding pooled docs more aggressively than needed
    # when a single sub-vector would have sufficed. Acceptable for Phase 1.
    existing_vectors = None
    existing_ids: list[str] = []
    if not force:
        vdata = load_vectors(cfg, source=source, projection=projection)
        if vdata is not None:
            existing_vectors, existing_ids = vdata

    existing_id_set = set(existing_ids)
    keep_mask = [i for i, eid in enumerate(existing_ids) if eid in {d["doc_id"] for d in docs}]
    if existing_vectors is not None and len(keep_mask) < len(existing_ids):
        existing_vectors = existing_vectors[keep_mask]
        existing_ids = [existing_ids[i] for i in keep_mask]
        existing_id_set = set(existing_ids)

    # Identify new rows by doc_id presence (pool="none") or by doc-level
    # absence (pool!="none"; any missing sub-vector triggers re-encode of
    # every sub-vector for that doc — intentional simplification).
    if pool == "none":
        to_encode_indices = [
            i for i, did in enumerate(all_doc_ids) if did not in existing_id_set
        ]
    else:
        # For pooled projections, existing_id_set already contains every
        # doc_id that has at least one sub-vector in the .npz. If a doc
        # isn't there at all, encode all its sub-vectors.
        to_encode_indices = [
            i for i, did in enumerate(all_doc_ids) if did not in existing_id_set
        ]

    if not to_encode_indices:
        return {
            "doc_count": len(set(all_doc_ids)),
            "docs_new": 0,
            "status": "up_to_date",
        }

    new_doc_ids = [all_doc_ids[i] for i in to_encode_indices]
    new_texts = [all_texts[i] for i in to_encode_indices]
    logger.info(
        "Encoding %d new rows for source=%s projection=%s kind=%s pool=%s (%d existing)",
        len(new_texts), source, projection, kind, pool, len(existing_ids),
    )
    t0 = time.time()
    new_vectors = _encode_bulk_direct(new_texts, kind=kind)
    encode_time = time.time() - t0

    if existing_vectors is not None and len(existing_ids) > 0:
        merged_vectors = np.vstack([existing_vectors, new_vectors])
        merged_ids = existing_ids + new_doc_ids
    else:
        merged_vectors = new_vectors
        merged_ids = new_doc_ids

    path = save_vectors(
        merged_vectors, merged_ids, cfg,
        source=source, projection=projection,
    )

    return {
        "doc_count": len(set(merged_ids)),
        "rows_total": len(merged_ids),
        "docs_new": len(set(new_doc_ids)),
        "dims": int(merged_vectors.shape[1]),
        "encode_time_s": round(encode_time, 1),
        "vector_file": path.as_posix(),
        "vector_file_mb": round(path.stat().st_size / 1024 / 1024, 1),
    }
