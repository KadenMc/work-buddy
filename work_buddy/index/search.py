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

    def search(self, q: Query) -> list[Hit]:
        if not (q.text or "").strip():
            return []

        pool = max(q.top_k * self._cfg.pool_multiplier, self._cfg.pool_floor)
        rrf_k = q.rrf_k if q.rrf_k is not None else self._cfg.rrf_k

        # Allowed-id set for filter-then-rank on the dense side (lexical filters in SQL).
        allowed: set[str] | None = None
        if q.filters or q.scope:
            allowed = set(self._store.load_documents(
                partition=self._partition, filters=q.filters, scope=q.scope,
            ).keys())

        rankings: list[dict[str, float]] = []
        signal_scores: dict[str, dict[str, float]] = {}

        # --- Lexical (FTS5 bm25) ---
        if q.method in ("hybrid", "lexical"):
            lex = self._store.search_lexical(
                q.text, partition=self._partition, filters=q.filters,
                scope=q.scope, top_k=pool,
            )
            if lex:
                rankings.append(lex)
                signal_scores["lexical"] = lex

        # --- Dense (per projection) ---
        if q.method in ("hybrid", "dense"):
            for proj_name, spec in self._schema.items():
                qvecs = self._encoder.encode_query(
                    [q.text], spec.kind, model_key=spec.model_key,
                )
                if qvecs is None or len(qvecs) == 0:
                    continue  # service down for this signal → degrade
                loaded = self._resident(proj_name).get()
                if loaded is None:
                    continue  # no vectors for this projection yet
                matrix, doc_ids = loaded
                scores = score_dense(qvecs[0], matrix, doc_ids, pool=spec.pool)
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
            fused = rrf_fuse(rankings, k=rrf_k)

        top_ids = sorted(fused, key=fused.get, reverse=True)[: max(q.top_k, 1)]
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
        if q.recency and self._cfg.recency:
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
