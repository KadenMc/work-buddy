"""Generic resident cache — version-keyed, idle-evicted, RLock-guarded.

Generalizes ``vault_index/dense_cache.py`` into a reusable ``ResidentCache[T]`` whose
behavior is **injected** (a ``loader`` + a ``version_fn``), NOT subclassed — per the
inheritance-free design (CLASS-ARCHITECTURE §6). Lives in the long-lived
embedding-service process: load-once, serve-from-RAM, reload when the on-disk version
changes (a stale value must never outlive a rebuild), free after an idle TTL.

A single ``ResidentCacheRegistry`` + one evictor daemon sweeps every registered cache —
collapsing what are today three bespoke evictors (model registry, vault matrix, /search
candidate cache). The consolidated index registers one ``ResidentCache`` per
(partition, projection) for its vector matrices.

Additive: this does NOT touch the existing live evictors; consolidating them onto this
is a later (flag-flip-time) step.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

DEFAULT_IDLE_TTL_S = 600.0
DEFAULT_EVICT_INTERVAL_S = 60.0


@dataclass
class _Cached(Generic[T]):
    # ``None`` is a meaningful, cacheable result: this generation currently has
    # no vectors.  Remembering that settled-empty state prevents every query from
    # starting another background warm and entering the client's retry window.
    value: T | None
    version: str
    loaded_at: float


class ResidentCache(Generic[T]):
    """A resident object reloaded on version change, freed after idle TTL.

    Args:
        loader: ``() -> T | None`` — produces the resident value (e.g. load the
            vector matrix from SQLite blobs). ``None`` records a settled-empty
            result for the current generation.
        version_fn: ``() -> str`` — the current on-disk version; when it differs
            from the cached copy, ``get()`` reloads.
        name: label for logs.
        clock: monotonic clock (injectable for tests).
    """

    def __init__(
        self,
        loader: Callable[[], T | None],
        version_fn: Callable[[], str],
        *,
        name: str = "resident-cache",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._version_fn = version_fn
        self._name = name
        self._clock = clock
        self._cached: _Cached[T] | None = None
        self._lock = threading.RLock()
        self._load_done = threading.Condition(self._lock)
        self._load_in_progress = False

    def get(self) -> T | None:
        """Return the resident value, (re)loading on a version change. ``None`` if empty."""
        while True:
            try:
                version = self._version_fn()
            except Exception as exc:  # version source unavailable → no cache
                logger.debug("%s: version_fn failed (%s); not caching", self._name, exc)
                return None

            with self._lock:
                if self._cached is not None and self._cached.version == version:
                    self._cached.loaded_at = self._clock()
                    return self._cached.value
                if self._load_in_progress:
                    # Join the elected loader. Re-read the durable generation after
                    # it finishes: the leader may have discarded a raced snapshot,
                    # or it may have loaded an older generation than this waiter saw.
                    self._load_done.wait()
                    continue
                self._load_in_progress = True

            try:
                # Load OUTSIDE the lock. Other caches remain independent, while
                # callers for this exact cache join through ``_load_done`` above.
                value = self._loader()
                # Optimistic, cross-process stability check: a writer may have
                # installed its dirty fence while the matrix loader scanned SQLite.
                try:
                    loaded_version = self._version_fn()
                except Exception as exc:
                    logger.debug(
                        "%s: version changed to unavailable during load (%s); discarding",
                        self._name,
                        exc,
                    )
                    return None
                if loaded_version != version:
                    logger.debug(
                        "%s: version changed during load (%s -> %s); discarding",
                        self._name,
                        version,
                        loaded_version,
                    )
                    return None
                with self._lock:
                    self._cached = _Cached(
                        value=value,
                        version=loaded_version,
                        loaded_at=self._clock(),
                    )
                    logger.info(
                        "%s: loaded resident state (version=%s, empty=%s)",
                        self._name,
                        loaded_version,
                        value is None,
                    )
                    return self._cached.value
            finally:
                with self._lock:
                    self._load_in_progress = False
                    self._load_done.notify_all()

    def get_if_cached(self) -> T | None:
        """Return the resident value ONLY if already loaded for the current version.

        Never triggers a load — the non-blocking peek the serving path uses to decide
        "dense now (warm) vs lexical-only + warm-in-background (cold)" without paying the
        cold-load latency inline. A cached value whose version is stale counts as absent
        (same correctness rule as :meth:`get`: a stale matrix must never be served)."""
        try:
            version = self._version_fn()
        except Exception:
            return None
        with self._lock:
            if self._cached is not None and self._cached.version == version:
                self._cached.loaded_at = self._clock()
                return self._cached.value
        return None

    def is_current(self) -> bool:
        """Whether this cache has settled the current on-disk generation.

        Unlike :meth:`get_if_cached`, this distinguishes a cold cache from a
        successfully loaded generation that contains zero vectors.  It never
        invokes the loader.
        """
        try:
            version = self._version_fn()
        except Exception:
            return False
        with self._lock:
            if self._cached is not None and self._cached.version == version:
                self._cached.loaded_at = self._clock()
                return True
        return False

    def invalidate(self) -> None:
        """Drop the cached value (call after a rebuild bumps the version)."""
        with self._lock:
            self._cached = None

    def release_if_idle(self, ttl_s: float = DEFAULT_IDLE_TTL_S) -> bool:
        """Free the value if untouched for ``ttl_s``. Returns True if released."""
        with self._lock:
            if self._cached is not None and (self._clock() - self._cached.loaded_at) > ttl_s:
                self._cached = None
                return True
        return False

    def is_cached(self) -> bool:
        """Whether a value or settled-empty result is retained in memory."""
        with self._lock:
            return self._cached is not None


class ResidentCacheRegistry:
    """Holds named resident caches so one evictor can sweep them all."""

    def __init__(self) -> None:
        self._caches: dict[str, ResidentCache] = {}
        self._lock = threading.RLock()

    def register(self, key: str, cache: ResidentCache) -> ResidentCache:
        with self._lock:
            self._caches[key] = cache
        return cache

    def get_or_create(
        self,
        key: str,
        loader: Callable[[], object | None],
        version_fn: Callable[[], str],
    ) -> ResidentCache:
        with self._lock:
            existing = self._caches.get(key)
            if existing is not None:
                return existing
            cache = ResidentCache(loader, version_fn, name=key)
            self._caches[key] = cache
            return cache

    def get(self, key: str) -> ResidentCache | None:
        with self._lock:
            return self._caches.get(key)

    def invalidate(self, key: str) -> None:
        with self._lock:
            c = self._caches.get(key)
        if c is not None:
            c.invalidate()

    def invalidate_all(self) -> None:
        with self._lock:
            caches = list(self._caches.values())
        for c in caches:
            c.invalidate()

    def sweep_idle(self, ttl_s: float = DEFAULT_IDLE_TTL_S) -> list[str]:
        """Release every cache idle past ``ttl_s``; return the released keys."""
        with self._lock:
            items = list(self._caches.items())
        released: list[str] = []
        for key, cache in items:
            try:
                if cache.release_if_idle(ttl_s):
                    released.append(key)
            except Exception as exc:  # one bad cache must not stall the sweep
                logger.debug("resident sweep: %s release failed: %s", key, exc)
        return released


_REGISTRY = ResidentCacheRegistry()


def get_registry() -> ResidentCacheRegistry:
    """Module-global resident-cache registry (one per process)."""
    return _REGISTRY


def start_idle_evictor(
    registry: ResidentCacheRegistry | None = None,
    *,
    ttl_s: float = DEFAULT_IDLE_TTL_S,
    interval_s: float = DEFAULT_EVICT_INTERVAL_S,
    name: str = "index-resident-evictor",
) -> threading.Thread:
    """Start a daemon thread that periodically sweeps idle resident caches.

    Started by the embedding-service ``main()`` (step 10); additive — does not replace
    the existing model/vault evictors yet.
    """
    reg = registry or _REGISTRY

    def _loop() -> None:
        while True:
            time.sleep(interval_s)
            try:
                released = reg.sweep_idle(ttl_s)
                if released:
                    logger.info("index resident evictor released: %s", released)
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("index resident evictor error: %s", exc)

    t = threading.Thread(target=_loop, name=name, daemon=True)
    t.start()
    return t
