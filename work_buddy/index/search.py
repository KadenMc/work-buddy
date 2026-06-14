"""HybridSearcher — the single retrieval path for the consolidated index.

FTS5 lexical (bm25) ⊕ per-projection dense (cosine over the resident matrix) fused by
RRF (per-partition ``rrf_k``), with metadata filtering + scope (pushed into the store),
optional recency, candidate-pool widening, single-signal passthrough, and
degrade-to-lexical when the embedding service is unavailable. Generalizes
``vault_index/search.py`` + ``ir/engine.py``.

Metadata filtering lives in ``IndexStore._metadata_where`` (json_extract); the searcher
just forwards ``query.filters``. Dense rankings are intersected with the allowed-id set
when filters/scope are present (filter-then-rank, like IR).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from work_buddy.index.encode import score_dense
from work_buddy.index.fusion import rrf_fuse
from work_buddy.index.model import Hit, PoolStrategy, ProjectionSpec, Query
from work_buddy.index.recency import apply_recency_bias
from work_buddy.index.resident import ResidentCacheRegistry, get_registry
from work_buddy.logging_config import get_logger

if TYPE_CHECKING:
    from work_buddy.index.config import PartitionConfig
    from work_buddy.index.encode import Encoder
    from work_buddy.index.store import IndexStore

logger = get_logger(__name__)


class HybridSearcher:
    """Per-partition hybrid searcher.

    Args:
        store: the shared IndexStore.
        encoder: query encoder (degrades to None when the service is down).
        partition: the partition this searcher serves.
        projection_schema: ``{projection_name: ProjectionSpec}`` — the dense signals.
        cfg: the partition's config (rrf_k, pool sizing, recency).
        residents: resident-cache registry (defaults to the process-global one).
    """

    def __init__(
        self,
        store: "IndexStore",
        encoder: "Encoder",
        *,
        partition: str,
        projection_schema: dict[str, ProjectionSpec],
        cfg: "PartitionConfig",
        residents: ResidentCacheRegistry | None = None,
    ) -> None:
        self._store = store
        self._encoder = encoder
        self._partition = partition
        self._schema = projection_schema or {}
        self._cfg = cfg
        self._residents = residents or get_registry()

    def _resident(self, projection: str):
        key = f"{self._partition}:{projection}"
        return self._residents.get_or_create(
            key,
            loader=lambda: self._store.load_all_vectors(self._partition, projection),
            version_fn=lambda: str(self._store.build_version(self._partition)),
        )

    def prewarm(self) -> int:
        """Eagerly load this partition's resident dense matrices into RAM.

        Goes through the SAME ``_resident`` caches the search path reads, so the
        matrix warmed here is exactly the one the next query serves from — no key
        drift. Removes the first-query cold-load penalty (a large partition's matrix
        can take tens of seconds to build from the float16 blobs, long enough for the
        first post-restart search to exceed its request timeout and degrade to a
        lexical-only / empty result). Idempotent with the idle evictor: a released
        matrix is simply re-loaded on the next call.

        Returns the number of projections whose matrix loaded (a projection with no
        vectors yet, or one that fails to load, contributes 0).
        """
        warmed = 0
        for projection in self._schema:
            try:
                if self._resident(projection).get() is not None:
                    warmed += 1
            except Exception as exc:  # one projection failing must not abort the rest
                logger.warning(
                    "prewarm: %s:%s failed to load: %s", self._partition, projection, exc,
                )
        return warmed

    def search(self, q: Query, *, block_until_warm: bool = True) -> list[Hit]:
        if not (q.text or "").strip():
            return []
        exclude_orphaned = not q.include_orphaned
        allowed = self._allowed_ids(q.filters, q.scope, exclude_orphaned)
        # Non-blocking mode: encode only the warm projections — a cold one's dense vector
        # would be unused, so encoding it would needlessly block on the query model.
        projset = None if block_until_warm else self._warm_projections()
        mats = self._encode_projections([q.text], q.method, projset)
        qvec_by_proj = {
            p: (m[0] if m is not None and len(m) else None) for p, m in mats.items()
        }
        return self._score_one(
            q.text, qvec_by_proj, allowed=allowed, filters=q.filters, scope=q.scope,
            method=q.method, recency=q.recency, rrf_k=q.rrf_k, top_k=q.top_k,
            block_until_warm=block_until_warm, exclude_orphaned=exclude_orphaned,
        )

    def search_many(
        self,
        queries: list[str],
        *,
        top_k: int = 10,
        method: str = "hybrid",
        filters: dict | None = None,
        scope: str | None = None,
        recency: bool = False,
        rrf_k: int | None = None,
        include_orphaned: bool = True,
        block_until_warm: bool = True,
    ) -> list[list[Hit]]:
        """Batched search: ONE query-encode round-trip per projection for ALL queries,
        then per-query lexical + score + fuse against the (shared, resident) matrices.

        Equivalent to ``[search(Query(text=q, ...)) for q in queries]`` but collapses N
        embedding round-trips to 1/projection (the budget-preserving property the
        dev-document scan relies on). Returns one ``list[Hit]`` per input query, in order.
        """
        texts = [str(t or "") for t in queries]
        exclude_orphaned = not include_orphaned
        allowed = self._allowed_ids(filters, scope, exclude_orphaned)
        projset = None if block_until_warm else self._warm_projections()
        mats = self._encode_projections(texts, method, projset)  # batch-encode (warm-only if non-blocking)
        out: list[list[Hit]] = []
        for i, text in enumerate(texts):
            if not text.strip():
                out.append([])
                continue
            qvec_by_proj = {
                p: (m[i] if m is not None and i < len(m) else None)
                for p, m in mats.items()
            }
            out.append(self._score_one(
                text, qvec_by_proj, allowed=allowed, filters=filters, scope=scope,
                method=method, recency=recency, rrf_k=rrf_k, top_k=top_k,
                block_until_warm=block_until_warm, exclude_orphaned=exclude_orphaned,
            ))
        return out

    # -- internals shared by search + search_many ------------------------------

    def _allowed_ids(self, filters, scope, exclude_orphaned=False) -> "set[str] | None":
        """Allowed-id set for filter-then-rank on the dense side (lexical filters in SQL).
        Independent of query text, so it's computed once per (filters, scope, orphan-mode).
        Returns a set whenever ANY restriction applies — including orphan exclusion alone,
        so the dense path honors a live-only view even with no other filters."""
        if filters or scope or exclude_orphaned:
            return set(self._store.load_documents(
                partition=self._partition, filters=filters, scope=scope,
                exclude_orphaned=exclude_orphaned,
            ).keys())
        return None

    def _warm_projections(self) -> "set[str]":
        """Projection names whose resident matrix is loaded RIGHT NOW (non-blocking peek).

        The serving path encodes only these in non-blocking mode: a cold projection's dense
        vector would be unused (its matrix is skipped), so encoding it would block on the
        query model for nothing — exactly the cost the warming path exists to avoid."""
        warm: set[str] = set()
        for proj in self._schema:
            try:
                if self._resident(proj).get_if_cached() is not None:
                    warm.add(proj)
            except Exception:  # pragma: no cover — peek is best-effort
                pass
        return warm

    def _encode_projections(
        self, texts: list[str], method: str, projections: "set[str] | None" = None,
    ) -> "dict[str, object | None]":
        """Batch query-encode ``texts`` per projection → ``{proj: (N,D) | None}``.
        A projection degrades to ``None`` (skipped for every query) when the encoder is
        unavailable OR returns the wrong row count (mirrors the knowledge batch guard).
        ``projections`` restricts encoding to a subset (the warm ones, in non-blocking
        mode); ``None`` encodes the full schema."""
        mats: dict[str, object | None] = {}
        if method not in ("hybrid", "dense"):
            return mats
        n = len(texts)
        for proj_name, spec in self._schema.items():
            if projections is not None and proj_name not in projections:
                continue  # cold projection in non-blocking mode → no encode (lexical-only)
            qvecs = self._encoder.encode_query(texts, spec.kind, model_key=spec.model_key)
            mats[proj_name] = qvecs if (qvecs is not None and len(qvecs) == n) else None
        return mats

    def _score_one(
        self, text: str, qvec_by_proj: "dict[str, object | None]", *,
        allowed: "set[str] | None", filters, scope, method: str, recency: bool,
        rrf_k: int | None, top_k: int, block_until_warm: bool = True,
        exclude_orphaned: bool = False,
    ) -> list[Hit]:
        """Score ONE query given its pre-encoded per-projection vectors. The retrieval
        body shared by ``search`` (1 query) and ``search_many`` (N) — lexical + dense
        fuse + hydrate + recency, identical to the original single-query path.

        ``block_until_warm`` controls the dense side's cold behavior: True (default) loads
        a not-yet-resident matrix inline (the original blocking semantics); False peeks
        without loading, so a cold projection degrades to lexical-only for this query and
        the matrix is left to a background warm — the non-blocking serving mode."""
        if not (text or "").strip():
            return []
        pool = max(top_k * self._cfg.pool_multiplier, self._cfg.pool_floor)
        rrf_k_val = rrf_k if rrf_k is not None else self._cfg.rrf_k

        rankings: list[dict[str, float]] = []
        signal_scores: dict[str, dict[str, float]] = {}

        # --- Lexical (FTS5 bm25) ---
        if method in ("hybrid", "lexical"):
            lex = self._store.search_lexical(
                text, partition=self._partition, filters=filters, scope=scope, top_k=pool,
                exclude_orphaned=exclude_orphaned,
            )
            if lex:
                rankings.append(lex)
                signal_scores["lexical"] = lex

        # --- Dense (per projection; pre-encoded vectors) ---
        if method in ("hybrid", "dense"):
            for proj_name, spec in self._schema.items():
                qvec = qvec_by_proj.get(proj_name)
                if qvec is None:
                    continue  # service down for this signal, or no encode → degrade
                resident = self._resident(proj_name)
                loaded = resident.get() if block_until_warm else resident.get_if_cached()
                if loaded is None:
                    continue  # no vectors, or cold matrix in non-blocking mode → lexical-only
                matrix, doc_ids = loaded
                scores = score_dense(qvec, matrix, doc_ids, pool=spec.pool)
                if allowed is not None:
                    scores = {d: s for d, s in scores.items() if d in allowed}
                if scores:
                    rankings.append(scores)
                    signal_scores[proj_name] = scores

        if not rankings:
            return []

        # --- Fuse (single-signal passthrough when only one) ---
        if len(rankings) == 1:
            fused = rankings[0]
        else:
            fused = rrf_fuse(rankings, k=rrf_k_val)

        top_ids = sorted(fused, key=fused.get, reverse=True)[: max(top_k, 1)]
        if not top_ids:
            return []

        # --- Hydrate (display + metadata + timestamp) ---
        docs = self._store.load_documents(partition=self._partition, doc_ids=top_ids)
        hits: list[Hit] = []
        timestamps: dict[str, float | None] = {}
        for did in top_ids:
            d = docs.get(did, {})
            sig = {"fused": round(float(fused[did]), 6)}
            for name, sc in signal_scores.items():
                if did in sc:
                    sig[name] = round(float(sc[did]), 6)
            hits.append(Hit(
                doc_id=did,
                score=round(float(fused[did]), 6),
                signals=sig,
                display_text=d.get("display_text", ""),
                metadata=d.get("metadata", {}),
            ))
            timestamps[did] = d.get("timestamp")

        # --- Recency (optional) ---
        if recency and self._cfg.recency:
            apply_recency_bias(
                hits, timestamps,
                half_life_days=self._cfg.recency_half_life_days,
                floor=self._cfg.recency_floor,
            )

        return hits


class MultiQueryFuser:
    """Outer RRF across several queries' result lists (the knowledge scan fan-out).

    Preserves ``knowledge/search.rrf_combine`` semantics: a doc appearing in multiple
    queries' top lists ranks higher. Returns a single fused, hydrated Hit list.
    """

    @staticmethod
    def fuse(per_query_hits: list[list[Hit]], *, k: int = 60, top_k: int = 20) -> list[Hit]:
        rankings: list[dict[str, float]] = []
        by_id: dict[str, Hit] = {}
        for hits in per_query_hits:
            ranking: dict[str, float] = {}
            for h in hits:
                ranking[h.doc_id] = h.score
                by_id.setdefault(h.doc_id, h)
            if ranking:
                rankings.append(ranking)
        if not rankings:
            return []
        fused = rrf_fuse(rankings, k=k)
        out: list[Hit] = []
        for did in sorted(fused, key=fused.get, reverse=True)[:top_k]:
            base = by_id[did]
            out.append(Hit(
                doc_id=did, score=round(float(fused[did]), 6),
                signals={**base.signals, "multiquery_rrf": round(float(fused[did]), 6)},
                display_text=base.display_text, metadata=base.metadata,
            ))
        return out
