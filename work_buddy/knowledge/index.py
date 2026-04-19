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

from work_buddy.knowledge.model import KnowledgeUnit, _resolve_placeholders
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Batch size for embedding requests (matches embedding service batch_size)
_EMBED_BATCH_SIZE = 32

# Default RRF constant (Cormack/Clarke/Buettcher 2009). Higher = flatter fusion.
_RRF_K = 60


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, filter short tokens."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1]


def _rrf_fuse(
    score_arrays: "list[np.ndarray]",
    n_docs: int,
    rrf_k: int = _RRF_K,
) -> "np.ndarray":
    """Reciprocal Rank Fusion across multiple ranked score arrays.

    Each score array ranks the same ``n_docs`` candidates (higher score = better).
    Only candidates with positive score in a given array contribute to that
    array's ranking; zero/negative-score docs are skipped (e.g. masked out).

    Fusion is rank-based — the absolute score scale of each input array does not
    matter. This is what makes it safe to fuse rankings from different vector
    spaces (e.g. 768-d content similarity vs 1024-d alias similarity) alongside
    BM25 scores without any normalization tricks.

    Args:
        score_arrays: List of score arrays, one per ranking signal. Arrays
            with all-zero (or all-None) content contribute nothing and are
            skipped silently — callers can pass unavailable signals as zero
            arrays without special-casing.
        n_docs: Length each score array is expected to have.
        rrf_k: RRF constant. Higher values flatten rank differences.

    Returns:
        Fused score array of shape (n_docs,). Higher is better.
    """
    import numpy as np

    fused = np.zeros(n_docs)
    for scores in score_arrays:
        if scores is None:
            continue
        # Skip arrays with no positive entries (e.g. masked-out or unavailable)
        if not np.any(scores > 0):
            continue
        order = np.argsort(-scores)
        for rank, idx in enumerate(order):
            if scores[idx] > 0:
                fused[idx] += 1.0 / (rrf_k + rank + 1)
    return fused


# ---------------------------------------------------------------------------
# Per-unit document for indexing
# ---------------------------------------------------------------------------

@dataclass
class IndexDoc:
    """A single indexed knowledge unit.

    Three textual views of the unit serve three ranking signals:

    - ``full_text`` — every searchable surface glued together (name, desc,
      aliases, tags, summary, full body). Tokenized for BM25. Aliases are
      INCLUDED here because aliases carry real lexical signal for BM25 and
      were always indexed this way; keeping them preserves BM25 behavior.
    - ``content_text`` — content-only text (name, desc, tags, summary, full
      body). Used for asymmetric DENSE content retrieval. Aliases are
      EXCLUDED here so they don't dilute the passage-shaped embedding. The
      alias signal lives in its own symmetric index (see ``alias_texts``).
    - ``alias_texts`` — list of individual alias strings. Each alias is
      embedded separately with the symmetric ``leaf-mt`` model and compared
      as a query↔query signal (max-pooled per doc at search time).
    """

    path: str
    # Concatenated searchable text (metadata + all content) — BM25 only
    full_text: str
    # Metadata-only text (name, description, aliases, tags) — BM25 only
    meta_text: str
    # Content-only text (excludes aliases) — used for asymmetric dense embedding
    content_text: str
    # Individual alias strings — each embedded separately with symmetric model
    alias_texts: list[str] = field(default_factory=list)
    # Tokenized versions (pre-computed for BM25)
    full_tokens: list[str] = field(default_factory=list)
    meta_tokens: list[str] = field(default_factory=list)


def _build_doc(
    path: str,
    unit: KnowledgeUnit,
    store: dict[str, KnowledgeUnit] | None = None,
) -> IndexDoc:
    """Build an IndexDoc from a knowledge unit.

    Includes the unit's OWN content (not chained context_before/after).
    When *store* is provided, inline ``<<wb:...>>`` placeholders in the
    content are resolved before indexing — so referenced content is
    searchable from the referencing unit.
    """
    meta_text = " ".join(unit.search_phrases())

    summary = unit.content.get("summary", "")
    full = unit.content.get("full", "")

    # Resolve placeholders so referenced content is indexed
    if store is not None and full and "<<wb:" in full:
        full = _resolve_placeholders(full, store)

    # --- full_text: BM25 corpus (all searchable surfaces, incl. aliases) ---
    full_parts = [meta_text]
    if summary:
        full_parts.append(summary)
    if full and full != summary:
        full_parts.append(full)
    full_text = "\n".join(full_parts)

    # --- content_text: dense content embedding (NO aliases) ---
    # Asymmetric passage encoder sees the written prose, not query-shaped
    # alias phrases. Aliases get their own index.
    name_text = unit.name.replace("-", " ").replace("_", " ")
    content_parts: list[str] = [name_text, unit.description]
    if unit.tags:
        content_parts.append(" ".join(unit.tags))
    if summary:
        content_parts.append(summary)
    if full and full != summary:
        content_parts.append(full)
    content_text = "\n".join(p for p in content_parts if p)

    # --- alias_texts: one embedding per alias, max-pooled at query time ---
    alias_texts = [a for a in (unit.aliases or []) if a and a.strip()]

    return IndexDoc(
        path=path,
        full_text=full_text,
        meta_text=meta_text,
        content_text=content_text,
        alias_texts=alias_texts,
        full_tokens=_tokenize(full_text),
        meta_tokens=_tokenize(meta_text),
    )


# ---------------------------------------------------------------------------
# Knowledge index
# ---------------------------------------------------------------------------

class KnowledgeIndex:
    """In-memory BM25 + dense index over knowledge units.

    Three ranking signals are fused via Reciprocal Rank Fusion:

    - **BM25** over ``full_text`` and ``meta_text`` (unchanged from the
      original design; aliases still boost BM25 lexical matches).
    - **Content dense** (768-d): ``content_text`` encoded asymmetrically via
      ``embed_for_ir(role="document")`` at build time; queries encoded with
      ``embed_for_ir(role="query")``. Matches user queries against passage
      prose without alias dilution.
    - **Alias dense** (1024-d): each alias embedded separately with the
      symmetric ``leaf-mt`` model (via bare ``embed()``); queries encoded
      the same way. Per-doc scores are max-pooled across the doc's aliases
      so one strong alias hit wins over many weak ones.

    Dense vectors are built EAGERLY during build() — these are the most
    important documents in the system and deserve full embedding coverage.
    If either model is unavailable, the corresponding signal is dropped
    from fusion and the remaining signals are used.
    """

    def __init__(self) -> None:
        self._docs: list[IndexDoc] = []
        self._path_to_idx: dict[str, int] = {}
        self._bm25_full: Any = None      # BM25Okapi over full text
        self._bm25_meta: Any = None      # BM25Okapi over metadata
        # Content dense vectors: shape (n_docs, 768), L2-normalized
        self._content_vectors: Any = None
        # Alias dense vectors: flat shape (total_aliases, 1024), L2-normalized.
        # _alias_slices[i] = (start, end) into _alias_flat for doc index i.
        # For docs with no aliases, slice is (s, s) (empty).
        self._alias_flat: Any = None
        self._alias_slices: list[tuple[int, int]] = []
        self._built_at: float = 0.0
        self._unit_count: int = 0
        self._has_content: bool = False
        self._has_aliases: bool = False
        self._generation: int = 0        # incremented on each build/invalidate

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
            doc = _build_doc(path, unit, store=store)
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

        # --- Build dense vectors eagerly (content + aliases) ---
        content_time = 0.0
        alias_time = 0.0
        self._content_vectors = None
        self._alias_flat = None
        self._alias_slices = [(0, 0)] * len(docs)
        self._has_content = False
        self._has_aliases = False

        if not skip_dense and docs:
            t_c = time.time()
            self._build_content_vectors()
            content_time = time.time() - t_c

            t_a = time.time()
            self._build_alias_vectors()
            alias_time = time.time() - t_a

        self._built_at = time.time()
        self._unit_count = len(docs)

        total_time = time.time() - t0
        stats = {
            "units_indexed": self._unit_count,
            "bm25_time_s": round(bm25_time, 3),
            "content_dense_time_s": round(content_time, 3),
            "alias_dense_time_s": round(alias_time, 3),
            "total_time_s": round(total_time, 3),
            "has_content_vectors": self._has_content,
            "has_alias_vectors": self._has_aliases,
        }
        logger.info("Knowledge index built: %s", stats)
        return stats

    def _embed_in_batches(
        self,
        texts: list[str],
        embed_fn,
        label: str,
        expected_generation: int | None = None,
    ) -> "list[list[float]] | None":
        """Batch-embed a list of texts via ``embed_fn``.

        Returns the full list of vectors, or ``None`` if the embedding
        service was unavailable at any point (loudly logged so the caller
        can fall back gracefully).

        ``embed_fn`` is a callable taking a list of texts and returning a
        list of vectors (or None) — typically ``embed`` or a lambda
        wrapping ``embed_for_ir(..., role=...)``.
        """
        if not texts:
            return []
        n_batches = (len(texts) + _EMBED_BATCH_SIZE - 1) // _EMBED_BATCH_SIZE
        all_vectors: list[list[float]] = []
        for batch_start in range(0, len(texts), _EMBED_BATCH_SIZE):
            if expected_generation is not None and self._generation != expected_generation:
                logger.info(
                    "Knowledge %s build aborted: index generation changed "
                    "(%d -> %d)", label, expected_generation, self._generation,
                )
                return None
            batch = texts[batch_start:batch_start + _EMBED_BATCH_SIZE]
            batch_vectors = embed_fn(batch)
            if batch_vectors is None:
                logger.warning(
                    "Embedding service unavailable during knowledge %s build "
                    "(batch %d/%d). Dropping this signal.",
                    label, batch_start // _EMBED_BATCH_SIZE + 1, n_batches,
                )
                return None
            all_vectors.extend(batch_vectors)
        return all_vectors

    @staticmethod
    def _l2_normalize(mat: "np.ndarray") -> "np.ndarray":
        import numpy as np
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms

    # Model identity keys — must match the model used at embed time. If the
    # model choice ever changes, bumping the key here (or CACHE_VERSION) will
    # invalidate the persisted cache on next load.
    _CONTENT_MODEL_KEY = "leaf-ir"
    _ALIAS_MODEL_KEY = "leaf-mt"

    def _build_content_vectors(self, expected_generation: int | None = None) -> None:
        """Embed every doc's ``content_text`` with the asymmetric passage encoder.

        Uses ``embed_for_ir(role="document")`` → 768-d ``leaf-ir`` vectors.
        Queries are encoded with ``embed_for_ir(role="query")`` at search time
        so the two sides live in the SAME 768-d space (dot product is
        meaningful). Aliases are deliberately excluded from ``content_text``
        to keep passage-shaped signal clean.

        Cache-aware: loads per-path hash→vector cache from disk and only
        re-embeds units whose ``content_text`` hash has changed (or who are
        new to the store). Writes an updated cache back after a successful
        build.

        Args:
            expected_generation: If set, abort if the index generation has
                changed (signals the index was invalidated/rebuilt while we
                were working in a background thread).
        """
        if not self._docs:
            return

        # Bail early if the index was invalidated/rebuilt since we were
        # queued (e.g. in a background warmup thread). Otherwise a stale
        # build could overwrite a fresh one. We re-check just before commit
        # too, since we release the GIL during embedding.
        if expected_generation is not None and self._generation != expected_generation:
            logger.info(
                "Knowledge content dense build aborted before start: "
                "generation changed (%d -> %d)",
                expected_generation, self._generation,
            )
            return

        try:
            from work_buddy.embedding.client import embed_for_ir
            from work_buddy.knowledge.persistence import (
                content_hash, load_content_cache, save_content_cache,
            )
            import numpy as np

            cache = load_content_cache(self._CONTENT_MODEL_KEY)

            # Walk docs in order; figure out which need re-embedding.
            n = len(self._docs)
            vectors: list["np.ndarray | None"] = [None] * n
            new_hashes: list[str] = [""] * n
            to_embed: list[tuple[int, str]] = []  # (doc_idx, text)

            for i, doc in enumerate(self._docs):
                h = content_hash(doc.content_text)
                new_hashes[i] = h
                cached = cache.get(doc.path)
                if cached is not None and cached[0] == h:
                    vectors[i] = cached[1]
                else:
                    to_embed.append((i, doc.content_text))

            hits = n - len(to_embed)
            t0 = time.time()

            if to_embed:
                texts = [t for _, t in to_embed]
                new_vecs = self._embed_in_batches(
                    texts,
                    embed_fn=lambda batch: embed_for_ir(batch, role="document"),
                    label="content dense",
                    expected_generation=expected_generation,
                )
                if new_vecs is None:
                    # Service unavailable partway — abandon this build without
                    # touching the persisted cache. The prior cache stays
                    # valid for next time.
                    return

                if expected_generation is not None and self._generation != expected_generation:
                    logger.info("Knowledge content dense build aborted after encoding")
                    return

                for (doc_i, _), vec in zip(to_embed, new_vecs):
                    v = np.array(vec, dtype=np.float32)
                    norm = np.linalg.norm(v)
                    if norm > 0:
                        v = v / norm
                    vectors[doc_i] = v

            # Final check before committing — the generation could have
            # advanced while we were embedding OR reading the cache file.
            if expected_generation is not None and self._generation != expected_generation:
                logger.info(
                    "Knowledge content dense build aborted before commit: "
                    "generation changed",
                )
                return

            # All slots filled — assemble doc-ordered matrix.
            mat = np.stack(vectors).astype(np.float32)
            self._content_vectors = mat
            self._has_content = True

            # Rewrite cache to reflect current store: prune deleted paths,
            # refresh changed vectors, keep untouched ones.
            new_cache = {
                doc.path: (new_hashes[i], vectors[i])
                for i, doc in enumerate(self._docs)
            }
            save_content_cache(new_cache, self._CONTENT_MODEL_KEY)

            logger.info(
                "Knowledge content vectors built: %d units (%d cache hits, "
                "%d embedded), %d dims, %.1fs",
                n, hits, len(to_embed), mat.shape[1], time.time() - t0,
            )

        except Exception as e:
            logger.warning(
                "Failed to build content dense vectors: %s. Content-path dense "
                "signal will be skipped.", e,
            )

    def _build_alias_vectors(self, expected_generation: int | None = None) -> None:
        """Embed every alias string separately with the symmetric ``leaf-mt`` model.

        Aliases are query-shaped phrases (e.g. "tabs I have open") authored
        in ``registry.py``. They're compared against user queries — query↔query
        — so the symmetric 1024-d ``leaf-mt`` model is the right choice
        (``embed_for_ir`` is for query↔passage comparisons).

        Storage: one flat matrix of shape (total_aliases, 1024) with per-doc
        slices ``(start, end)`` into it. At query time, similarity is computed
        against the whole flat matrix in one dot product, then max-pooled per
        doc via the slices. This keeps the query-time cost to one matrix
        multiply regardless of alias count.

        Cache-aware: loads a ``{(path, alias_text): vector}`` cache from disk
        and only re-embeds alias strings that are new or changed. Writes an
        updated cache back after a successful build.
        """
        if not self._docs:
            return

        # Bail early if the index was invalidated/rebuilt since we were
        # queued. Re-check before commit too — see _build_content_vectors
        # for rationale.
        if expected_generation is not None and self._generation != expected_generation:
            logger.info(
                "Knowledge alias dense build aborted before start: "
                "generation changed (%d -> %d)",
                expected_generation, self._generation,
            )
            return

        # Plan the flat layout (doc order → slice order) BEFORE checking the
        # cache so slices are stable regardless of cache state.
        slices: list[tuple[int, int]] = []
        flat_keys: list[tuple[str, str]] = []  # (path, alias_text) per flat row
        cursor = 0
        for doc in self._docs:
            start = cursor
            for alias in doc.alias_texts:
                flat_keys.append((doc.path, alias))
                cursor += 1
            slices.append((start, cursor))

        if not flat_keys:
            # No aliases anywhere — reset signal and be done. Persist the
            # empty cache so a later read doesn't misattribute a cold disk
            # to a cold startup.
            from work_buddy.knowledge.persistence import save_alias_cache
            self._alias_slices = slices
            self._alias_flat = None
            self._has_aliases = False
            save_alias_cache({}, self._ALIAS_MODEL_KEY)
            logger.info("Knowledge alias vectors: no aliases in store, skipping")
            return

        try:
            from work_buddy.embedding.client import embed
            from work_buddy.knowledge.persistence import (
                load_alias_cache, save_alias_cache,
            )
            import numpy as np

            cache = load_alias_cache(self._ALIAS_MODEL_KEY)

            # Fill cached rows; collect missing rows to embed.
            flat_vectors: list["np.ndarray | None"] = [None] * len(flat_keys)
            to_embed: list[tuple[int, str]] = []  # (flat_idx, alias_text)
            for i, key in enumerate(flat_keys):
                cached = cache.get(key)
                if cached is not None:
                    flat_vectors[i] = cached
                else:
                    to_embed.append((i, key[1]))

            hits = len(flat_keys) - len(to_embed)
            t0 = time.time()

            if to_embed:
                texts = [t for _, t in to_embed]
                new_vecs = self._embed_in_batches(
                    texts,
                    embed_fn=embed,  # symmetric leaf-mt (1024-d)
                    label="alias dense",
                    expected_generation=expected_generation,
                )
                if new_vecs is None:
                    return

                if expected_generation is not None and self._generation != expected_generation:
                    logger.info("Knowledge alias dense build aborted after encoding")
                    return

                for (flat_i, _), vec in zip(to_embed, new_vecs):
                    v = np.array(vec, dtype=np.float32)
                    norm = np.linalg.norm(v)
                    if norm > 0:
                        v = v / norm
                    flat_vectors[flat_i] = v

            # Final check before commit — see _build_content_vectors.
            if expected_generation is not None and self._generation != expected_generation:
                logger.info(
                    "Knowledge alias dense build aborted before commit: "
                    "generation changed",
                )
                return

            mat = np.stack(flat_vectors).astype(np.float32)
            self._alias_flat = mat
            self._alias_slices = slices
            self._has_aliases = True

            # Persist current set — dropped aliases fall out naturally.
            new_cache = {
                flat_keys[i]: flat_vectors[i] for i in range(len(flat_keys))
            }
            save_alias_cache(new_cache, self._ALIAS_MODEL_KEY)

            doc_with_aliases = sum(1 for s, e in slices if e > s)
            logger.info(
                "Knowledge alias vectors built: %d aliases across %d/%d units "
                "(%d cache hits, %d embedded), %d dims, %.1fs",
                mat.shape[0], doc_with_aliases, len(self._docs),
                hits, len(to_embed), mat.shape[1], time.time() - t0,
            )

        except Exception as e:
            logger.warning(
                "Failed to build alias dense vectors: %s. Alias-path dense "
                "signal will be skipped.", e,
            )

    def invalidate(self) -> None:
        """Clear the index. Will be rebuilt on next search/ensure call."""
        self._generation += 1  # signals background threads to abort
        self._docs = []
        self._path_to_idx = {}
        self._bm25_full = None
        self._bm25_meta = None
        self._content_vectors = None
        self._alias_flat = None
        self._alias_slices = []
        self._has_content = False
        self._has_aliases = False
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

        # --- Dense scoring: two independent signals, fused by rank ---
        # Either may be None if the respective signal is unavailable (service
        # down, model missing, no aliases in store). _rrf_fuse skips None/zero
        # arrays, so fusion degrades gracefully.
        content_scores, alias_scores = self._score_dense(query, mask)

        signals: list = [bm25_scores]
        if content_scores is not None:
            signals.append(content_scores)
        if alias_scores is not None:
            signals.append(alias_scores)

        if len(signals) > 1:
            scores = _rrf_fuse(signals, n_docs=n_docs)
        else:
            # Only BM25 available — use it directly (no need to rank-fuse a
            # single signal with itself).
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
    ) -> "tuple[np.ndarray | None, np.ndarray | None]":
        """Score query against both dense indices.

        Returns ``(content_scores, alias_scores)`` — either may be ``None``
        if the respective signal is unavailable (service down, model missing,
        no aliases in store). Callers should drop ``None`` arrays from fusion.

        Both arrays are normalized to [0, 1] per-query so that within-array
        ranking is preserved; actual score magnitudes don't matter after RRF
        since fusion is rank-based.
        """
        if self._content_vectors is None and self._alias_flat is None:
            return None, None

        import numpy as np

        content_scores: "np.ndarray | None" = None
        alias_scores: "np.ndarray | None" = None

        # --- Content path: asymmetric query encoder (leaf-ir-query, 768-d) ---
        if self._content_vectors is not None:
            try:
                from work_buddy.embedding.client import embed_for_ir

                q_vecs = embed_for_ir([query], role="query")
                if q_vecs and q_vecs[0]:
                    q_vec = np.array(q_vecs[0], dtype=np.float32)
                    q_norm = np.linalg.norm(q_vec)
                    if q_norm > 0:
                        q_vec = q_vec / q_norm
                        sims = self._content_vectors @ q_vec
                        sims[~mask] = 0.0
                        max_sim = sims.max()
                        if max_sim > 0:
                            sims = sims / max_sim
                        content_scores = sims
            except Exception as e:
                logger.debug("Content dense query scoring failed: %s", e)

        # --- Alias path: symmetric leaf-mt (1024-d), max-pool per doc ---
        if self._alias_flat is not None and self._alias_flat.shape[0] > 0:
            try:
                from work_buddy.embedding.client import embed

                q_vecs = embed([query])
                if q_vecs and q_vecs[0]:
                    q_vec = np.array(q_vecs[0], dtype=np.float32)
                    q_norm = np.linalg.norm(q_vec)
                    if q_norm > 0:
                        q_vec = q_vec / q_norm
                        # One dot product against ALL alias vectors.
                        flat_sims = self._alias_flat @ q_vec
                        # Max-pool per doc via slices.
                        n_docs = len(self._docs)
                        sims = np.zeros(n_docs, dtype=np.float32)
                        for i, (start, end) in enumerate(self._alias_slices):
                            if end > start and mask[i]:
                                sims[i] = flat_sims[start:end].max()
                        max_sim = sims.max()
                        if max_sim > 0:
                            sims = sims / max_sim
                            alias_scores = sims
                        # If no positive alias matches at all, leave as None —
                        # adding an all-zero signal to fusion is a no-op but
                        # explicit None is cleaner.
            except Exception as e:
                logger.debug("Alias dense query scoring failed: %s", e)

        return content_scores, alias_scores

    def status(self) -> dict[str, Any]:
        """Return index status info."""
        result: dict[str, Any] = {
            "built": self.is_built,
            "unit_count": self._unit_count,
            "built_at": self._built_at,
            "has_content_vectors": self._has_content,
            "has_alias_vectors": self._has_aliases,
        }
        if self._has_content and self._content_vectors is not None:
            result["content_dims"] = int(self._content_vectors.shape[1])
            result["content_memory_mb"] = round(
                self._content_vectors.nbytes / 1024 / 1024, 2
            )
        if self._has_aliases and self._alias_flat is not None:
            result["alias_count"] = int(self._alias_flat.shape[0])
            result["alias_dims"] = int(self._alias_flat.shape[1])
            result["alias_memory_mb"] = round(
                self._alias_flat.nbytes / 1024 / 1024, 2
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


def rebuild_index(
    knowledge_scope: str = "all",
    force: bool = False,
) -> dict[str, Any]:
    """Force rebuild the index from the current store.

    Args:
        knowledge_scope: Which store to index ("all", "system", "personal").
        force: If True, wipe the persistent dense-vector cache first so the
            rebuild re-embeds every unit. Use when the cache is corrupted or
            after a model change that didn't bump ``CACHE_VERSION``. Normal
            rebuilds (``force=False``) keep the cache and re-embed only
            changed units — much faster.

    Returns:
        Build stats dict. When ``force=True``, includes a ``cache_cleared``
        field showing which caches were removed.
    """
    idx = get_index()
    idx.invalidate()

    cleared: dict[str, Any] | None = None
    if force:
        from work_buddy.knowledge.persistence import clear_caches
        cleared = clear_caches()

    from work_buddy.knowledge.store import load_store
    store = load_store(scope=knowledge_scope, force=True)
    stats = idx.build(store)
    if cleared is not None:
        stats["cache_cleared"] = cleared
    return stats
