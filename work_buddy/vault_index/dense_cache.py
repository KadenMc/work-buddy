"""Resident dense-vector matrix cache for vault search.

Lazy-loads the ``(N, dim)`` float32 matrix from the ``chunk_vectors`` blobs on first
query, serves it from RAM, **invalidates** when the index is rebuilt (an ``index_meta``
version counter, bumped by the build), and **releases** it after an idle TTL (frees the
~hundreds of MB). Mirrors the embedding service's ``_candidate_cache`` + model
idle-evictor patterns.

The cache is a module global usable from any caller. It lives in the long-lived
embedding-service process, where a background idle-evictor thread calls
:func:`release_if_idle`; version-invalidation is the load-bearing correctness piece —
a stale matrix must never outlive a rebuild.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from work_buddy.logging_config import get_logger
from work_buddy.vault_index import store

logger = get_logger(__name__)

VERSION_KEY = "build_version:vault"
DEFAULT_IDLE_TTL_S = 600.0


@dataclass
class _Cached:
    vectors: np.ndarray
    doc_ids: list[str]
    version: str
    loaded_at: float


_cache: _Cached | None = None
_lock = threading.RLock()


def current_version(conn) -> str:
    return store.get_meta(conn, VERSION_KEY) or "0"


def get_matrix(cfg: dict | None = None) -> tuple[np.ndarray, list[str]] | None:
    """Return the cached ``(matrix, doc_ids)``, loading/reloading as needed.

    Reloads when the index's ``build_version`` has changed since the cached copy.
    Returns ``None`` when no vectors exist.
    """
    global _cache
    conn = store.get_connection(cfg)
    try:
        version = current_version(conn)
        with _lock:
            if _cache is not None and _cache.version == version:
                _cache.loaded_at = time.monotonic()
                return _cache.vectors, _cache.doc_ids
        loaded = store.load_all_vectors(conn)
    finally:
        conn.close()

    if loaded is None:
        with _lock:
            _cache = None
        return None
    vectors, doc_ids = loaded
    with _lock:
        _cache = _Cached(vectors, doc_ids, version, time.monotonic())
        logger.info("vault_index: loaded %d vectors into the resident matrix", len(doc_ids))
        return _cache.vectors, _cache.doc_ids


def invalidate() -> None:
    """Drop the cached matrix (call after a rebuild)."""
    global _cache
    with _lock:
        _cache = None


def release_if_idle(ttl_s: float = DEFAULT_IDLE_TTL_S) -> bool:
    """Free the matrix if it hasn't been used within ``ttl_s``. Returns True if released."""
    global _cache
    with _lock:
        if _cache is not None and (time.monotonic() - _cache.loaded_at) > ttl_s:
            _cache = None
            return True
    return False


def is_cached() -> bool:
    with _lock:
        return _cache is not None
