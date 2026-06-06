"""Index-agnostic seam — a uniform view over work-buddy's several indexes.

work-buddy has more than one thing that "indexes content and serves search":
the IR engine (``work_buddy/ir/`` — conversation, docs, summaries, …), the vault
semantic index (``work_buddy/vault_index/``), and the knowledge index
(``work_buddy/knowledge/``). Each grew its own store, status, and build path.

This package is the thin **observability seam** over all of them: a single
``Index`` protocol every backend reports through (``protocol.py``), a name→index
``registry``, and a ``status.aggregate_status()`` that yields one uniform
``IndexStatus`` per index for the dashboard's index panel. Backends keep their
internals; they gain a small adapter (``adapters/``).

This package is the *status/observability* surface. The advisory lock and matrix cache
it relies on live in ``utils/index_lock.py`` and ``vault_index/dense_cache.py``; this
layer reads them rather than owning them.
"""
from __future__ import annotations

from work_buddy.indexing.protocol import (
    BuildProgress,
    BuildResult,
    Index,
    IndexStatus,
    PartitionStatus,
)

__all__ = [
    "BuildProgress",
    "BuildResult",
    "Index",
    "IndexStatus",
    "PartitionStatus",
]
