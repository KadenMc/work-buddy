"""Consolidated index package.

One substrate (SQLite + FTS5 + float16 blob vectors) holding IR-model ``Document``s
across partitions (knowledge, vault chunks, conversations, …), served warm in-service
and scheduled by the inference broker. Subsumes ``knowledge/index.py``, ``vault_index/``,
and ``ir/``.

**Inert until ``index.enabled`` is true** (default OFF). See
``.data/designs/index-consolidation/{DESIGN.md, CLASS-ARCHITECTURE.md}`` for the full
design and ``config.py`` for the feature flag.

This package imports NO domain package — partitions are registered into it by their
domains (``domain → index``), never the reverse.
"""

from __future__ import annotations

from work_buddy.index.config import (
    DEFAULT_RRF_K,
    IndexConfig,
    PartitionConfig,
    load_index_config,
)
from work_buddy.index.model import (
    Document,
    Hit,
    ItemRef,
    PoolStrategy,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    Query,
    content_hash,
    make_doc_id,
)

__all__ = [
    # model
    "Document",
    "Hit",
    "ItemRef",
    "PoolStrategy",
    "Projection",
    "ProjectionKind",
    "ProjectionSpec",
    "Query",
    "content_hash",
    "make_doc_id",
    # config
    "DEFAULT_RRF_K",
    "IndexConfig",
    "PartitionConfig",
    "load_index_config",
]
