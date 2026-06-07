"""Dense vector encoding for the vault semantic index.

Encodes stored chunks' ``embed_input`` into 768-d vectors stored as float16 blobs
in the ``chunk_vectors`` table (one row per chunk). Incremental: only chunks
without a vector are encoded; vectors for deleted chunks are removed automatically
by the ``chunk_vectors -> chunks`` FK cascade (see ``store.py``). ``force=True``
drops all vectors and re-encodes.

Per-chunk blob storage means an incremental build writes only the new rows (O(1)
per chunk) — no monolithic vector file to load-and-rewrite.
"""
from __future__ import annotations

import time
from typing import Callable

# Reuse the IR engine's bulk encoder. This cross-package private import is an
# intentional, scoped coupling — it gives the vault build the standalone model-load
# and LM Studio offload paths for free (the offload dispatch lives only in that
# function). A provider-agnostic ``encode_bulk`` could later move to a shared module.
from work_buddy.ir.dense import _encode_bulk_direct
from work_buddy.logging_config import get_logger
from work_buddy.vault_index import store

logger = get_logger(__name__)

CHECKPOINT_ROWS = 500


def build_vectors(
    cfg: dict | None = None,
    *,
    force: bool = False,
    on_checkpoint: Callable[[], None] | None = None,
) -> dict:
    """Encode chunks lacking a vector and store them as blobs (incremental).

    Args:
        cfg: Config dict (defaults to ``load_config()``).
        force: Drop all existing vectors and re-encode every chunk.
        on_checkpoint: Called after each committed batch (the build wraps this to
            refresh the advisory-lock heartbeat during a long encode).

    Returns:
        Stats: vectors_total, vectors_new, dims (when encoding), encode_time_s,
        status.
    """
    if cfg is None:
        from work_buddy.config import load_config
        cfg = load_config()

    conn = store.get_connection(cfg)
    try:
        if force:
            store.delete_all_vectors(conn)

        pending = store.chunks_to_encode(conn)  # [(doc_id, embed_input)]
        if not pending:
            return {
                "vectors_total": store.vector_count(conn),
                "vectors_new": 0,
                "status": "up_to_date",
            }

        logger.info("vault_index: encoding %d new chunk vectors", len(pending))
        t0 = time.time()
        new = 0
        dims = 0
        for start in range(0, len(pending), CHECKPOINT_ROWS):
            batch = pending[start:start + CHECKPOINT_ROWS]
            doc_ids = [doc_id for doc_id, _ in batch]
            texts = [text for _, text in batch]
            from work_buddy.inference.call_context import inference_detail
            with inference_detail("vault index"):
                vecs = _encode_bulk_direct(texts, kind="passage")
            store.upsert_vectors(conn, doc_ids, vecs)   # per-batch commit (resumable)
            new += len(doc_ids)
            dims = int(vecs.shape[1])
            if on_checkpoint is not None:
                on_checkpoint()

        return {
            "vectors_total": store.vector_count(conn),
            "vectors_new": new,
            "dims": dims,
            "encode_time_s": round(time.time() - t0, 1),
            "status": "ok",
        }
    finally:
        conn.close()
