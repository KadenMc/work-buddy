"""IR engine → :class:`Index`. One partition per IR source.

Wraps ``ir.store.index_status`` (verified shape: ``sources[src]`` with
``doc_count``/``dense_eligible_docs``/``last_build`` and, when a ``.npz`` exists,
``vectors[src]`` with ``vector_count``/``pending_eligible``/``vector_file_mb``).
This is the cheapest adapter and proves the protocol against a live, populated
index — wire it first.
"""
from __future__ import annotations

from typing import Callable

from work_buddy.indexing.protocol import (
    BuildProgress,
    BuildResult,
    IndexStatus,
    PartitionStatus,
)

# Source names the IR factory (``ir/store.py::_get_source``) knows. Used by
# bulk_build; status discovers sources from the DB instead.
_IR_SOURCES = ["conversation", "chrome", "projects", "docs", "task_note", "summary"]


class IRIndexAdapter:
    name = "ir"

    def status(self) -> IndexStatus:
        from work_buddy.ir.store import index_status

        raw = index_status()
        if raw.get("status") != "ok":
            return IndexStatus(name=self.name, partitions=[])

        sources = raw.get("sources", {})
        vectors = raw.get("vectors", {})
        parts: list[PartitionStatus] = []
        total_size = 0.0
        for src, info in sorted(sources.items()):
            vec = vectors.get(src, {})
            eligible = info.get("dense_eligible_docs", 0)
            vcount = vec.get("vector_count", 0)
            size = vec.get("vector_file_mb")
            if size:
                total_size += size
            parts.append(
                PartitionStatus(
                    key=src,
                    total_items=info.get("doc_count", 0),
                    dense_eligible=eligible,
                    vector_count=vcount,
                    pending=vec.get("pending_eligible", max(0, eligible - vcount)),
                    last_build=info.get("last_build"),
                    size_on_disk_mb=size,
                    health="ok",
                )
            )
        return IndexStatus(
            name=self.name,
            partitions=parts,
            size_on_disk_mb=round(total_size, 1) if total_size else None,
        )

    def lock_key(self) -> str:
        return "ir"

    def bulk_build(
        self,
        *,
        full_history: bool = False,
        on_progress: Callable[[BuildProgress], None] | None = None,
    ) -> BuildResult:
        from work_buddy.ir.dense import build_vectors
        from work_buddy.ir.store import build_index

        days = 36500 if full_history else 30
        stats: dict = {}
        try:
            for src in _IR_SOURCES:
                if on_progress:
                    on_progress(BuildProgress(phase="scanning"))
                stats[src] = {
                    "index": build_index(source=src, days=days),
                    "dense": build_vectors(source=src),
                }
            return BuildResult(name=self.name, ok=True, stats=stats)
        except Exception as exc:
            return BuildResult(
                name=self.name, ok=False, stats=stats,
                error=f"{type(exc).__name__}: {exc}",
            )
