"""IndexPartition + UnifiedIndex — the facade callers use.

``UnifiedIndex`` is the single entry point: it owns the shared store + encoder +
resident registry, lazily wires an ``IndexPartition`` per partition (search + build),
federates cross-partition search via RRF (fork F-CROSS), and reports status through the
``indexing`` seam. Everything is inert until ``index.enabled`` (the caller checks it).
"""

from __future__ import annotations

import threading
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
        self._residents = residents
        self._searcher = HybridSearcher(
            store, encoder, partition=partition.name,
            projection_schema=get_projection_schema(partition), cfg=cfg,
            residents=residents,
        )
        self._builder = IndexBuilder(store, encoder, partition, residents=residents)

    @property
    def name(self) -> str:
        return self._partition.name

    def search(self, q: Query, *, block_until_warm: bool = True) -> list[Hit]:
        return self._searcher.search(q, block_until_warm=block_until_warm)

    def search_many(
        self, queries: list[str], *, top_k: int = 10, method: str = "hybrid",
        filters: dict | None = None, scope: str | None = None,
        recency: bool = False, rrf_k: int | None = None,
        block_until_warm: bool = True,
    ) -> list[list[Hit]]:
        return self._searcher.search_many(
            queries, top_k=top_k, method=method, filters=filters,
            scope=scope, recency=recency, rrf_k=rrf_k,
            block_until_warm=block_until_warm,
        )

    def is_warm(self) -> bool:
        """True iff every dense projection's resident matrix is loaded. Non-blocking —
        the readiness predicate behind the warming signal, on the serving hot path, so it
        must stay O(projections) in RAM: ``ResidentCache.is_cached()`` is a pure in-memory
        check (no DB). A lexical-only partition (no projections) is always warm.

        A projection that legitimately has no vectors never loads, so it reads as "cold"
        forever — a query against it costs one redundant warm-retry, then degrades
        gracefully. That benign edge is deliberately accepted over probing vector counts
        here: the count is a ``COUNT(DISTINCT) JOIN`` across the whole (all-partition)
        ``doc_vectors`` table — far too heavy to run per query on the readiness path."""
        schema = get_projection_schema(self._partition)
        if not schema:
            return True
        for proj in schema:
            cache = self._residents.get(f"{self.name}:{proj}")
            if cache is None or not cache.is_cached():
                return False
        return True

    def hydrate(self, hits: list[Hit], **opts) -> list[Any]:
        return hydrate(self._partition, hits, **opts)

    def prewarm(self) -> int:
        """Eagerly load this partition's resident dense matrices (see
        :meth:`HybridSearcher.prewarm`). Returns the number of projections warmed."""
        return self._searcher.prewarm()

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

    @property
    def config(self) -> IndexConfig:
        return self._config

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

    def search(
        self, q: Query, partitions: list[str] | None = None, *,
        block_until_warm: bool = True,
    ) -> list[Hit]:
        # Default: search the partitions that actually have docs in the store.
        names = partitions if partitions is not None else (
            self._store.partitions() or self.available()
        )
        results: list[list[Hit]] = []
        for name in names:
            try:
                hits = self.partition(name).search(q, block_until_warm=block_until_warm)
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
        block_until_warm: bool = True,
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
                    block_until_warm=block_until_warm,
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

    def cold_partitions(self, partitions: list[str] | None = None) -> list[str]:
        """The requested (or all built) partitions whose dense matrices aren't resident
        yet — the ``warming`` set. Non-blocking. An unknown/failing partition is treated
        as warm: we never signal warming for one we can't introspect."""
        names = partitions if partitions is not None else (
            self._store.partitions() or self.available()
        )
        cold: list[str] = []
        for name in names:
            try:
                if not self.partition(name).is_warm():
                    cold.append(name)
            except Exception as exc:  # unknown/unregistered partition → not "warming"
                logger.debug("cold_partitions: %r introspection failed: %s", name, exc)
        return cold

    def warm_eta_s(self, partitions: list[str]) -> float:
        """Rough seconds-to-warm estimate for ``partitions`` from their document counts
        (the matrix load is ~linear in row count). Feeds the warming signal's
        ``retry_after_s`` so the client's one-shot retry waits a sensible interval. Uses
        ``doc_count`` (a partition-indexed ``COUNT`` on ``documents``), NOT ``vector_count``
        (a ``COUNT(DISTINCT) JOIN`` over the whole ``doc_vectors`` table) — the cheap proxy
        is plenty for an ETA and keeps the warming response fast."""
        total = 0
        for name in partitions:
            try:
                total += self._store.doc_count(name)
            except Exception:  # pragma: no cover — defensive
                continue
        eta = total / _WARM_LOAD_ROWS_PER_S
        return float(min(max(eta, _WARM_ETA_FLOOR_S), _WARM_ETA_CAP_S))

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


# Resident-matrix load throughput used to estimate warm ETA (rows/sec). Derived from the
# observed vault load (~88k rows in ~27s ≈ 3300/s); rounded down so retry_after_s does not
# under-shoot. Floor/cap keep the client's one-shot wait sane on tiny / huge partitions.
_WARM_LOAD_ROWS_PER_S = 3000.0
_WARM_ETA_FLOOR_S = 2.0
_WARM_ETA_CAP_S = 60.0

# Singleflight guard for on-demand warming: concurrent cold queries for the same partition
# must spawn ONE background warm, not N (the thundering-herd mitigation).
_warming_in_flight: set[str] = set()
_warming_lock = threading.Lock()


def prewarm_resident_matrices(
    config: IndexConfig | None = None,
    *,
    only: list[str] | None = None,
    index_factory: Callable[[IndexConfig], UnifiedIndex] | None = None,
) -> dict[str, int]:
    """Load BUILT partitions' resident dense matrices into RAM up front.

    The cold-start fix. Dense matrices are lazy-loaded on a partition's first search,
    so after the embedding service restarts the first search of a large partition pays
    the full load (e.g. vault, ~88k×768 vectors) — long enough to exceed the request
    timeout, at which point the client sees ``None`` and the first post-restart search
    silently misses the consolidated index. Warming the matrices up front (the service
    calls this in a background thread at startup) removes that first-query penalty.

    Gated: returns ``{}`` immediately when ``index.enabled`` is false or no partition is
    built — never builds or loads a disabled/empty index. Loading is a SQLite read plus
    numpy reshape (no model encode), so it does not contend for the inference broker /
    GPU. Idempotent with the idle evictor. Never raises — a failing partition is logged
    and skipped.

    ``only`` restricts warming to the named subset (still intersected with what's actually
    built) — used by the on-demand warm a cold query triggers; ``None`` warms every built
    partition (the startup path). ``index_factory`` is an injection seam for tests;
    production passes the resolved config and lets it construct a :class:`UnifiedIndex`
    bound to the process-global resident registry (the one the serving path reads).

    Returns ``{partition: n_projections_warmed}``.
    """
    cfg = config or load_index_config()
    if not cfg.enabled:
        logger.debug("index prewarm: index.enabled is false; skipping")
        return {}
    ui = index_factory(cfg) if index_factory is not None else UnifiedIndex(config=cfg)
    built = ui.store.partitions()
    if only is not None:
        wanted = set(only)
        built = [p for p in built if p in wanted]
    if not built:
        logger.debug("index prewarm: no built partitions to warm; skipping")
        return {}
    # Warm the largest partitions first. Their matrices take longest to load (vault is
    # ~88k×768) and are exactly the ones whose cold-load penalty motivates prewarm, so a
    # query that races the warm-up is least likely to find a slow partition still cold;
    # the small partitions (~1-2s each) trail harmlessly. Ordering is best-effort — a
    # count failure falls back to the store's order rather than aborting the warm-up.
    try:
        built = sorted(built, key=lambda n: ui.store.doc_count(n), reverse=True)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("index prewarm: size ordering failed (%s); using store order", exc)
    logger.info(
        "index prewarm: warming resident matrices for %d built partition(s): %s",
        len(built), built,
    )
    warmed: dict[str, int] = {}
    for name in built:
        try:
            n = ui.partition(name).prewarm()
        except Exception as exc:  # one partition failing must not abort the rest
            logger.warning("index prewarm: partition %r failed: %s", name, exc)
            continue
        warmed[name] = n
        logger.info("index prewarm: %s warmed (%d projection matrix/matrices)", name, n)
    return warmed


def start_prewarm(
    config: IndexConfig | None = None,
    *,
    only: list[str] | None = None,
    index_factory: Callable[[IndexConfig], UnifiedIndex] | None = None,
    name: str = "index-prewarm",
) -> threading.Thread:
    """Spawn a daemon thread that runs :func:`prewarm_resident_matrices`.

    Non-blocking: the service must keep serving ``/health`` and queries while the
    matrices warm. Started by the embedding-service ``main()`` (additive — pairs with
    the idle evictor, which releases what this warms after an idle TTL).
    """
    def _run() -> None:
        try:
            prewarm_resident_matrices(config, only=only, index_factory=index_factory)
        except Exception as exc:  # pragma: no cover — prewarm already guards per-partition
            logger.warning("index prewarm thread failed: %s", exc)

    t = threading.Thread(target=_run, name=name, daemon=True)
    t.start()
    return t


def warm_partitions_async(
    cold: list[str],
    *,
    config: IndexConfig | None = None,
    index_factory: Callable[[IndexConfig], UnifiedIndex] | None = None,
) -> threading.Thread | None:
    """Warm ``cold`` partitions in a background daemon, singleflighted per partition.

    The on-demand counterpart to startup prewarm: a query that finds a partition cold
    triggers this so the matrix is resident by the caller's one-shot retry. The
    singleflight guard (``_warming_in_flight``) ensures concurrent cold queries for the
    same partition spawn ONE warm, not one each. Returns the thread, or ``None`` when
    every requested partition already has a warm in flight (nothing to do)."""
    with _warming_lock:
        todo = [p for p in cold if p not in _warming_in_flight]
        _warming_in_flight.update(todo)
    if not todo:
        return None

    def _run() -> None:
        try:
            prewarm_resident_matrices(config, only=todo, index_factory=index_factory)
        except Exception as exc:  # pragma: no cover — prewarm already guards per-partition
            logger.warning("index on-demand warm failed for %s: %s", todo, exc)
        finally:
            with _warming_lock:
                _warming_in_flight.difference_update(todo)

    t = threading.Thread(target=_run, name="index-warm-on-demand", daemon=True)
    t.start()
    return t
