"""Persistent in-memory search index for the knowledge system.

Indexes the FULL CONTENT of all knowledge units (system docs + personal)
so hybrid search can match on body text, not just metadata phrases.

Architecture:
  - BM25 via rank_bm25 (always available, no external service)
  - Dense vectors via embedding service (eagerly built at index time)
  - RRF fusion when both are available, BM25-only fallback
  - Rebuilt on store invalidation or explicit rebuild

These are the most important and most frequently accessed documents in
the entire framework. Every unit gets fully embedded at build time —
no lazy loading, no truncation, no skimming.

Lightweight by design: ~220 units fit comfortably in memory.
No SQLite or disk persistence — rebuilt from the store on demand.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from work_buddy.knowledge.model import KnowledgeUnit
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Batch size for embedding requests (matches embedding service batch_size)
_EMBED_BATCH_SIZE = 32


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, filter short tokens."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1]


# ---------------------------------------------------------------------------
# Per-unit document for indexing
# ---------------------------------------------------------------------------

@dataclass
class IndexDoc:
    """A single indexed knowledge unit."""

    path: str
    # Concatenated searchable text (metadata + all content)
    full_text: str
    # Metadata-only text (name, description, aliases, tags)
    meta_text: str
    # Tokenized versions (pre-computed for BM25)
    full_tokens: list[str] = field(default_factory=list)
    meta_tokens: list[str] = field(default_factory=list)


def _build_doc(path: str, unit: KnowledgeUnit) -> IndexDoc:
    """Build an IndexDoc from a knowledge unit.

    Includes the unit's OWN content (not chained context_before/after).
    """
    meta_text = " ".join(unit.search_phrases())

    # Build full text: metadata + summary + full content body
    content_parts = [meta_text]
    summary = unit.content.get("summary", "")
    full = unit.content.get("full", "")
    if summary:
        content_parts.append(summary)
    if full and full != summary:
        content_parts.append(full)

    full_text = "\n".join(content_parts)

    return IndexDoc(
        path=path,
        full_text=full_text,
        meta_text=meta_text,
        full_tokens=_tokenize(full_text),
        meta_tokens=_tokenize(meta_text),
    )


# ---------------------------------------------------------------------------
# Knowledge index
# ---------------------------------------------------------------------------

class KnowledgeIndex:
    """In-memory BM25 + dense index over knowledge units.

    Dense vectors are built EAGERLY during build() — these are the most
    important documents in the system and deserve full embedding coverage.
    """

    def __init__(self) -> None:
        self._docs: list[IndexDoc] = []
        self._path_to_idx: dict[str, int] = {}
        self._bm25_full: Any = None    # BM25Okapi over full text
        self._bm25_meta: Any = None    # BM25Okapi over metadata
        self._dense_vectors: Any = None  # numpy array (n_docs, dim) or None
        self._built_at: float = 0.0
        self._unit_count: int = 0
        self._has_dense: bool = False
        self._generation: int = 0      # incremented on each build/invalidate

    @property
    def is_built(self) -> bool:
        return self._unit_count > 0

    @property
    def size(self) -> int:
        return self._unit_count

    def build(
        self,
        store: dict[str, KnowledgeUnit],
        skip_dense: bool = False,
    ) -> dict[str, Any]:
        """Build the full index from a knowledge store.

        Builds BM25 indices AND dense vectors eagerly. If the embedding
        service is unavailable, logs a warning and continues with BM25 only.

        Args:
            store: Merged store from load_store(scope="all").
            skip_dense: If True, skip dense vector building (for testing).

        Returns:
            Stats dict with timing and counts.
        """
        from rank_bm25 import BM25Okapi

        t0 = time.time()
        self._generation += 1

        # --- Build documents ---
        docs: list[IndexDoc] = []
        path_to_idx: dict[str, int] = {}

        for path, unit in store.items():
            doc = _build_doc(path, unit)
            path_to_idx[path] = len(docs)
            docs.append(doc)

        self._docs = docs
        self._path_to_idx = path_to_idx

        # --- Build BM25 indices ---
        full_corpus = [d.full_tokens for d in docs]
        meta_corpus = [d.meta_tokens for d in docs]

        if full_corpus and any(full_corpus):
            self._bm25_full = BM25Okapi(full_corpus)
        else:
            self._bm25_full = None

        if meta_corpus and any(meta_corpus):
            self._bm25_meta = BM25Okapi(meta_corpus)
        else:
            self._bm25_meta = None

        bm25_time = time.time() - t0

        # --- Build dense vectors eagerly ---
        dense_time = 0.0
        self._dense_vectors = None
        self._has_dense = False

        if not skip_dense and docs:
            t_dense = time.time()
            self._build_dense_vectors()
            dense_time = time.time() - t_dense

        self._built_at = time.time()
        self._unit_count = len(docs)

        total_time = time.time() - t0
        stats = {
            "units_indexed": self._unit_count,
            "bm25_time_s": round(bm25_time, 3),
            "dense_time_s": round(dense_time, 3),
            "total_time_s": round(total_time, 3),
            "has_dense_vectors": self._has_dense,
        }
        logger.info("Knowledge index built: %s", stats)
        return stats

    def _build_dense_vectors(self, expected_generation: int | None = None) -> None:
        """Embed ALL indexed documents with their full content.

        Sends full_text for each unit to the embedding service in batches.
        These are the most important documents in the framework — they get
        fully embedded, not truncated or skimmed.

        Args:
            expected_generation: If set, abort if the index generation has
                changed (means the index was invalidated/rebuilt while we
                were working in a background thread).
        """
        if not self._docs:
            return

        try:
            from work_buddy.embedding.client import embed

            # Embed full content text for every unit
            texts = [doc.full_text for doc in self._docs]

            t0 = time.time()

            # Batch to avoid overwhelming the service
            all_vectors: list[list[float]] = []
            for batch_start in range(0, len(texts), _EMBED_BATCH_SIZE):
                # Check if index was invalidated/rebuilt during background work
                if expected_generation is not None and self._generation != expected_generation:
                    logger.info(
                        "Knowledge dense build aborted: index generation changed "
                        "(%d -> %d)", expected_generation, self._generation,
                    )
                    return

                batch = texts[batch_start:batch_start + _EMBED_BATCH_SIZE]
                batch_vectors = embed(batch)
                if batch_vectors is None:
                    logger.warning(
                        "Embedding service unavailable during knowledge index build "
                        "(batch %d/%d). Index will use BM25 only.",
                        batch_start // _EMBED_BATCH_SIZE + 1,
                        (len(texts) + _EMBED_BATCH_SIZE - 1) // _EMBED_BATCH_SIZE,
                    )
                    return
                all_vectors.extend(batch_vectors)

            # Final generation check before writing results
            if expected_generation is not None and self._generation != expected_generation:
                logger.info(
                    "Knowledge dense build aborted after encoding: index generation changed"
                )
                return

            import numpy as np

            mat = np.array(all_vectors, dtype=np.float32)

            # L2-normalize rows for cosine similarity via dot product
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            mat = mat / norms

            self._dense_vectors = mat
            self._has_dense = True

            logger.info(
                "Knowledge dense vectors built: %d units, %d dims, %.1fs "
                "(%d batches of %d)",
                mat.shape[0],
                mat.shape[1],
                time.time() - t0,
                (len(texts) + _EMBED_BATCH_SIZE - 1) // _EMBED_BATCH_SIZE,
                _EMBED_BATCH_SIZE,
            )

        except Exception as e:
            logger.warning(
                "Failed to build dense vectors for knowledge index: %s. "
                "Falling back to BM25 only.",
                e,
            )

    def invalidate(self) -> None:
        """Clear the index. Will be rebuilt on next search/ensure call."""
        self._generation += 1  # signals background threads to abort
        self._docs = []
        self._path_to_idx = {}
        self._bm25_full = None
        self._bm25_meta = None
        self._dense_vectors = None
        self._has_dense = False
        self._built_at = 0.0
        self._unit_count = 0
        logger.debug("Knowledge index invalidated (generation=%d)", self._generation)

    def search(
        self,
        query: str,
        candidates: dict[str, KnowledgeUnit] | None = None,
        top_n: int = 8,
        meta_weight: float = 0.3,
        content_weight: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Search the index with hybrid BM25 + dense scoring.

        Args:
            query: Natural language search query.
            candidates: If provided, restrict search to these paths only.
                        If None, search the full index.
            top_n: Maximum results to return.
            meta_weight: Weight for metadata BM25 scores.
            content_weight: Weight for full-text BM25 scores.

        Returns:
            List of {"path": str, "score": float} sorted by score desc.
        """
        if not self.is_built:
            return []

        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        import numpy as np

        n_docs = len(self._docs)

        # Candidate mask (restrict to subset if provided)
        if candidates is not None:
            mask = np.zeros(n_docs, dtype=bool)
            for path in candidates:
                idx = self._path_to_idx.get(path)
                if idx is not None:
                    mask[idx] = True
        else:
            mask = np.ones(n_docs, dtype=bool)

        # --- BM25 scoring (weighted: content + metadata) ---
        bm25_scores = np.zeros(n_docs)

        if self._bm25_full is not None and content_weight > 0:
            full_scores = self._bm25_full.get_scores(q_tokens)
            max_full = full_scores.max()
            if max_full > 0:
                full_scores = full_scores / max_full
            bm25_scores += full_scores * content_weight

        if self._bm25_meta is not None and meta_weight > 0:
            meta_scores = self._bm25_meta.get_scores(q_tokens)
            max_meta = meta_scores.max()
            if max_meta > 0:
                meta_scores = meta_scores / max_meta
            bm25_scores += meta_scores * meta_weight

        bm25_scores[~mask] = 0.0

        # --- Dense scoring (from pre-built vectors) ---
        dense_scores = self._score_dense(query, mask)

        # --- RRF fusion ---
        if dense_scores is not None:
            rrf_k = 60
            fused = np.zeros(n_docs)

            # BM25 ranking contribution
            bm25_order = np.argsort(-bm25_scores)
            for rank, idx in enumerate(bm25_order):
                if bm25_scores[idx] > 0:
                    fused[idx] += 1.0 / (rrf_k + rank + 1)

            # Dense ranking contribution
            dense_order = np.argsort(-dense_scores)
            for rank, idx in enumerate(dense_order):
                if dense_scores[idx] > 0:
                    fused[idx] += 1.0 / (rrf_k + rank + 1)

            scores = fused
        else:
            scores = bm25_scores

        scores[~mask] = 0.0

        # --- Rank and return ---
        top_indices = np.argsort(-scores)[:top_n]

        results = []
        for idx in top_indices:
            if scores[idx] <= 0:
                break
            results.append({
                "path": self._docs[idx].path,
                "score": round(float(scores[idx]), 4),
            })

        return results

    def _score_dense(
        self,
        query: str,
        mask: "np.ndarray",
    ) -> "np.ndarray | None":
        """Score query against pre-built dense vectors.

        Returns normalized similarity scores, or None if dense vectors
        are not available (embedding service was down at build time).
        """
        if self._dense_vectors is None:
            return None

        import numpy as np

        try:
            from work_buddy.embedding.client import embed

            q_vecs = embed([query])
            if q_vecs is None or not q_vecs:
                return None

            q_vec = np.array(q_vecs[0], dtype=np.float32)
            q_norm = np.linalg.norm(q_vec)
            if q_norm == 0:
                return None
            q_vec = q_vec / q_norm

            # Cosine similarity (vectors are pre-normalized)
            sims = self._dense_vectors @ q_vec
            sims[~mask] = 0.0

            # Normalize to [0, 1]
            max_sim = sims.max()
            if max_sim > 0:
                sims = sims / max_sim

            return sims

        except Exception as e:
            logger.debug("Dense query scoring failed: %s", e)
            return None

    def status(self) -> dict[str, Any]:
        """Return index status info."""
        import numpy as np

        result: dict[str, Any] = {
            "built": self.is_built,
            "unit_count": self._unit_count,
            "built_at": self._built_at,
            "has_dense_vectors": self._has_dense,
        }
        if self._has_dense and self._dense_vectors is not None:
            result["dense_dims"] = int(self._dense_vectors.shape[1])
            result["dense_memory_mb"] = round(
                self._dense_vectors.nbytes / 1024 / 1024, 2
            )
        return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_INDEX = KnowledgeIndex()


def get_index() -> KnowledgeIndex:
    """Return the module-level knowledge index singleton."""
    return _INDEX


def ensure_index(knowledge_scope: str = "all") -> KnowledgeIndex:
    """Ensure the index is built, building it if needed.

    Args:
        knowledge_scope: Which store to index ("all", "system", "personal").

    Returns:
        The built index.
    """
    idx = get_index()
    if not idx.is_built:
        from work_buddy.knowledge.store import load_store
        store = load_store(scope=knowledge_scope)
        idx.build(store)
    return idx


def invalidate_index() -> None:
    """Invalidate the singleton index."""
    _INDEX.invalidate()


def rebuild_index(knowledge_scope: str = "all") -> dict[str, Any]:
    """Force rebuild the index from the current store."""
    idx = get_index()
    idx.invalidate()

    from work_buddy.knowledge.store import load_store
    store = load_store(scope=knowledge_scope, force=True)
    return idx.build(store)
