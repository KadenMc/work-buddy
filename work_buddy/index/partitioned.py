"""IndexPartition + UnifiedIndex — the facade callers use.

``UnifiedIndex`` is the single entry point: it owns the shared store + encoder +
resident registry, lazily wires an ``IndexPartition`` per partition (search + build),
federates cross-partition search via RRF (fork F-CROSS), and reports status through the
``indexing`` seam. Everything is inert until ``index.enabled`` (the caller checks it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from work_buddy.index.config import IndexConfig, load_index_config
from work_buddy.index.model import Hit, Query
from work_buddy.index.partition import (
    configure_partition,
    get_partition_registry,
    get_projection_schema,
    hydrate,
)
from work_buddy.index.search import HybridSearcher, MultiQueryFuser
from work_buddy.logging_config import get_logger

if TYPE_CHECKING:
    from work_buddy.index.encode import Encoder
    from work_buddy.index.partition import Partition, PartitionRegistry
    from work_buddy.index.resident import ResidentCacheRegistry
    from work_buddy.index.store import IndexStore

logger = get_logger(__name__)


class IndexPartition:
    """One partition end-to-end: search (HybridSearcher) + build (IndexBuilder)."""

    def __init__(
        self,
        partition: "Partition",
        store: "IndexStore",
        encoder: "Encoder",
        cfg,
        residents: "ResidentCacheRegistry",
    ) -> None:
        from work_buddy.index.build import IndexBuilder

        self._partition = partition
        configure_partition(partition, cfg)  # apply coverage etc. before build/search
        self._store = store
        self._cfg = cfg
        self._searcher = HybridSearcher(
            store, encoder, partition=partition.name,
            projection_schema=get_projection_schema(partition), cfg=cfg,
            residents=residents,
        )
        self._builder = IndexBuilder(store, encoder, partition, residents=residents)

    @property
    def name(self) -> str:
        return self._partition.name

    def search(self, q: Query) -> list[Hit]:
        return self._searcher.search(q)

    def search_many(
        self, queries: list[str], *, top_k: int = 10, method: str = "hybrid",
        filters: dict | None = None, scope: str | None = None,
        recency: bool = False, rrf_k: int | None = None,
    ) -> list[list[Hit]]:
        return self._searcher.search_many(
            queries, top_k=top_k, method=method, filters=filters,
            scope=scope, recency=recency, rrf_k=rrf_k,
        )

    def hydrate(self, hits: list[Hit], **opts) -> list[Any]:
        return hydrate(self._partition, hits, **opts)

    def build(self, *, force: bool = False, on_progress=None) -> dict[str, Any]:
        return self._builder.build(force=force, on_progress=on_progress)

    def status(self):
        from work_buddy.indexing.protocol import PartitionStatus
        total = self._store.doc_count(self.name)
        vcount = self._store.vector_count(self.name)
        return PartitionStatus(
            key=self.name,
            total_items=total,
            dense_eligible=total,
            vector_count=min(vcount, total),
            pending=max(0, total - vcount),
            last_build=self._store.get_meta(f"last_build:{self.name}"),
            health="ok",
        )


class UnifiedIndex:
    """The consolidated index facade."""

    NAME = "consolidated"

    def __init__(
        self,
        store: "IndexStore | None" = None,
        encoder: "Encoder | None" = None,
        config: IndexConfig | None = None,
        residents: "ResidentCacheRegistry | None" = None,
        registry: "PartitionRegistry | None" = None,
    ) -> None:
        self._config = config or load_index_config()
        if store is None:
            from work_buddy.index.store import IndexStore
            store = IndexStore(self._config.resolved_db_path())
        self._store = store
        if encoder is None:
            from work_buddy.index.encode import default_encoder
            encoder = default_encoder()
        self._encoder = encoder
        if residents is None:
            from work_buddy.index.resident import get_registry
            residents = get_registry()
        self._residents = residents
        if registry is None:
            from work_buddy.index.partitions.bootstrap import ensure_partitions_registered
            ensure_partitions_registered()
            registry = get_partition_registry()
        self._registry = registry
        self._partitions: dict[str, IndexPartition] = {}

    @property
    def store(self) -> "IndexStore":
        return self._store

    def available(self) -> list[str]:
        return self._registry.names()

    def partition(self, name: str) -> IndexPartition:
        if name not in self._partitions:
            part = self._registry.get(name)
            self._partitions[name] = IndexPartition(
                part, self._store, self._encoder, self._config.partition(name),
                self._residents,
            )
        return self._partitions[name]

    def search(self, q: Query, partitions: list[str] | None = None) -> list[Hit]:
        # Default: search the partitions that actually have docs in the store.
        names = partitions if partitions is not None else (
            self._store.partitions() or self.available()
        )
        results: list[list[Hit]] = []
        for name in names:
            try:
                hits = self.partition(name).search(q)
            except Exception as exc:  # one partition failing must not kill the query
                logger.warning("partition %r search failed: %s", name, exc)
                continue
            if hits:
                results.append(hits)
        if not results:
            return []
        if len(results) == 1:
            return results[0][: q.top_k]
        # Cross-partition federation via RRF (fork F-CROSS).
        return MultiQueryFuser.fuse(results, k=(q.rrf_k or 60), top_k=q.top_k)

    def search_many(
        self, queries: list[str], partitions: list[str] | None = None, *,
        top_k: int = 10, method: str = "hybrid", filters: dict | None = None,
        scope: str | None = None, recency: bool = False, rrf_k: int | None = None,
    ) -> list[list[Hit]]:
        """Batched federated search — one ``list[Hit]`` per query, in order. Each
        partition is searched once (batched); per query, results federate across
        partitions via RRF, exactly like :meth:`search` does for a single query."""
        names = partitions if partitions is not None else (
            self._store.partitions() or self.available()
        )
        per_partition: list[list[list[Hit]]] = []
        for name in names:
            try:
                res = self.partition(name).search_many(
                    queries, top_k=top_k, method=method, filters=filters,
                    scope=scope, recency=recency, rrf_k=rrf_k,
                )
            except Exception as exc:  # one partition failing must not kill the batch
                logger.warning("partition %r search_many failed: %s", name, exc)
                continue
            per_partition.append(res)
        out: list[list[Hit]] = []
        fk = rrf_k or 60
        for i in range(len(queries)):
            per_q = [pp[i] for pp in per_partition if i < len(pp) and pp[i]]
            if not per_q:
                out.append([])
            elif len(per_q) == 1:
                out.append(per_q[0][:top_k])
            else:
                out.append(MultiQueryFuser.fuse(per_q, k=fk, top_k=top_k))
        return out

    def hydrate(self, partition: str, hits: list[Hit], **opts) -> list[Any]:
        return self.partition(partition).hydrate(hits, **opts)

    def build(self, name: str, *, force: bool = False, on_progress=None) -> dict[str, Any]:
        return self.partition(name).build(force=force, on_progress=on_progress)

    def build_all(self, *, force: bool = False) -> list[dict[str, Any]]:
        out = []
        for name in self.available():
            try:
                out.append(self.build(name, force=force))
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("build_all: %r failed: %s", name, exc)
                out.append({"partition": name, "error": str(exc)})
        return out

    def status(self):
        from work_buddy.indexing.protocol import IndexStatus
        # Report partitions present in the store (built); fall back to registered names.
        names = self._store.partitions() or self.available()
        parts = []
        for name in names:
            try:
                parts.append(self.partition(name).status())
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("status for %r failed: %s", name, exc)
        size_mb = None
        try:
            size_mb = round(self._store.db_path.stat().st_size / 1024 / 1024, 2)
        except OSError:
            pass
        return IndexStatus(name=self.NAME, partitions=parts, size_on_disk_mb=size_mb)
