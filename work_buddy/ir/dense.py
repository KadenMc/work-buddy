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


def encode_query(query: str) -> np.ndarray | None:
    """Encode a search query via the embedding service (leaf-ir, query prompt).

    Returns (1, 768) float32 array, or None if service unavailable.
    """
    if _IN_SERVICE:
        return _encode_query_direct(query)
    vectors = embed_for_ir([query], role="query")
    if vectors is None:
        return None
    return np.array(vectors, dtype=np.float32)


def _encode_query_direct(query: str) -> np.ndarray | None:
    """Encode query in-process using the service's loaded model."""
    try:
        from work_buddy.embedding.service import _get_model
        model = _get_model("leaf-ir-query")
        vec = model.encode([query], prompt_name="query", show_progress_bar=False)
        return np.array(vec, dtype=np.float32)
    except Exception as exc:
        logger.warning("In-service query encoding failed: %s", exc)
        return None


def encode_documents(
    texts: list[str],
    batch_size: int = 32,
    progress: bool = True,
) -> np.ndarray | None:
    """Encode document texts via the embedding service (leaf-ir, document prompt).

    Batches requests to avoid HTTP timeouts on large corpora.
    Returns (N, 768) float32 array, or None if service unavailable.
    """
    import sys

    all_vectors: list[list[float]] = []
    total = len(texts)
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        # Retry once on failure (first batch may trigger lazy model load)
        vectors = embed_for_ir(batch, role="document")
        if vectors is None:
            import time
            logger.warning("Batch %d failed, retrying in 5s (may be lazy loading)...",
                           i // batch_size + 1)
            time.sleep(5)
            vectors = embed_for_ir(batch, role="document")
        if vectors is None:
            logger.error("Encoding failed at batch %d/%d", i // batch_size + 1,
                         (total + batch_size - 1) // batch_size)
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
) -> dict[str, float]:
    """Cosine similarity between query vector and document vectors.

    Args:
        query_vec: (1, D) or (D,) query vector.
        doc_vectors: (N, D) document vectors.
        doc_ids: Parallel list of doc IDs.

    Returns:
        {doc_id: score} dict, scores in [0, 1].
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

    # Normalize to [0, 1]
    max_sim = sims.max()
    if max_sim > 0:
        sims = sims / max_sim

    return {
        doc_id: float(score)
        for doc_id, score in zip(doc_ids, sims)
        if score > 0
    }


def score_query(
    query: str,
    cfg: dict | None = None,
    source: str | None = None,
) -> dict[str, float]:
    """Full dense scoring pipeline: encode query, load vectors, compute sims.

    Called by engine.search() when dense retrieval is enabled.
    Returns empty dict if embedding service is unavailable or no vectors exist.
    """
    if not _IN_SERVICE and not is_available():
        logger.debug("Embedding service not available for dense scoring")
        return {}

    query_vec = encode_query(query)
    if query_vec is None:
        return {}

    # Load per-source vectors, or all if source is None
    if source:
        vdata = load_vectors(cfg, source=source)
    else:
        # Merge vectors across all sources
        vdata = _load_all_vectors(cfg)

    if vdata is None:
        logger.debug("No vector file found for dense scoring")
        return {}

    doc_vectors, doc_ids = vdata
    return score_dense(query_vec, doc_vectors, doc_ids)


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
) -> np.ndarray:
    """Encode documents in-process (no HTTP).

    When running inside the embedding service (``_IN_SERVICE``), reuses the
    already-loaded ``leaf-ir`` model from the service registry.  Otherwise
    loads a fresh SentenceTransformer (standalone / CLI usage).
    """
    import sys

    if _IN_SERVICE:
        from work_buddy.embedding.service import _get_model
        model = _get_model("leaf-ir")  # triggers lazy load on first call
        logger.info("Using in-service leaf-ir model for bulk encoding")
    else:
        from work_buddy.config import load_config
        cfg = load_config()
        model_name = cfg.get("embedding", {}).get("models", {}).get(
            "leaf-ir", {}
        ).get("name", "MongoDB/mdbr-leaf-ir-asym")
        from sentence_transformers import SentenceTransformer
        logger.info("Loading %s for bulk encoding...", model_name)
        model = SentenceTransformer(model_name)

    all_vecs = []
    total = len(texts)
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        vecs = model.encode(
            batch, batch_size=batch_size,
            show_progress_bar=False, prompt_name="document",
        )
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

    Loads existing vectors, identifies which doc_ids are new (not yet
    encoded), encodes only those, appends to the existing vectors, and
    saves. Pass force=True to re-encode everything from scratch.

    Returns stats dict.
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

    all_doc_ids = [d["doc_id"] for d in docs]
    docs_by_id = {d["doc_id"]: d for d in docs}

    # Load existing vectors (if any) and find what's new
    existing_vectors = None
    existing_ids: list[str] = []
    if not force:
        vdata = load_vectors(cfg, source=source)
        if vdata is not None:
            existing_vectors, existing_ids = vdata

    existing_id_set = set(existing_ids)
    new_doc_ids = [did for did in all_doc_ids if did not in existing_id_set]

    if not new_doc_ids:
        return {
            "doc_count": len(all_doc_ids),
            "docs_new": 0,
            "status": "up_to_date",
        }

    # Encode only new documents
    new_texts = [docs_by_id[did]["dense_text"] for did in new_doc_ids]
    logger.info("Encoding %d new documents (%d existing)...",
                len(new_texts), len(existing_ids))
    t0 = time.time()
    new_vectors = _encode_bulk_direct(new_texts)
    encode_time = time.time() - t0

    # Merge with existing
    if existing_vectors is not None and len(existing_ids) > 0:
        # Filter existing to only keep docs still in the index
        # (handles deletions if force rebuild dropped some)
        keep_mask = [i for i, eid in enumerate(existing_ids) if eid in docs_by_id]
        if len(keep_mask) < len(existing_ids):
            existing_vectors = existing_vectors[keep_mask]
            existing_ids = [existing_ids[i] for i in keep_mask]

        merged_vectors = np.vstack([existing_vectors, new_vectors])
        merged_ids = existing_ids + new_doc_ids
    else:
        merged_vectors = new_vectors
        merged_ids = new_doc_ids

    path = save_vectors(merged_vectors, merged_ids, cfg, source=source)

    return {
        "doc_count": len(merged_ids),
        "docs_new": len(new_doc_ids),
        "dims": int(merged_vectors.shape[1]),
        "encode_time_s": round(encode_time, 1),
        "vector_file": path.as_posix(),
        "vector_file_mb": round(path.stat().st_size / 1024 / 1024, 1),
    }
