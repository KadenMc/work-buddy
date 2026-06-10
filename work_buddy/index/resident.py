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
    value: T
    version: str
    loaded_at: float


class ResidentCache(Generic[T]):
    """A resident object reloaded on version change, freed after idle TTL.

    Args:
        loader: ``() -> T | None`` — produces the resident value (e.g. load the
            vector matrix from SQLite blobs). ``None`` means "nothing to cache".
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

    def get(self) -> T | None:
        """Return the resident value, (re)loading on a version change. ``None`` if empty."""
        try:
            version = self._version_fn()
        except Exception as exc:  # version source unavailable → no cache
            logger.debug("%s: version_fn failed (%s); not caching", self._name, exc)
            return None

        with self._lock:
            if self._cached is not None and self._cached.version == version:
                self._cached.loaded_at = self._clock()
                return self._cached.value

        # Load OUTSIDE the lock so a slow load doesn't block readers of other caches.
        value = self._loader()
        with self._lock:
            if value is None:
                self._cached = None
                return None
            self._cached = _Cached(value=value, version=version, loaded_at=self._clock())
            logger.info("%s: loaded resident value (version=%s)", self._name, version)
            return self._cached.value

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
