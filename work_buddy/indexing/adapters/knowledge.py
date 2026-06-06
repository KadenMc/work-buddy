"""Knowledge index → :class:`Index`. One partition (content projection).

The knowledge dense index is a *per-process* singleton (built lazily where it is
queried), so its in-memory ``status()`` is unreliable from the dashboard process.
This adapter therefore reads **on-disk** truth instead: the unit count from the
store, and the dense vector count from the persisted content cache
(``persistence.load_content_cache``). Counts-only is acceptable for v1 (the
knowledge index is the lowest-urgency partition).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from work_buddy.indexing.protocol import (
    BuildProgress,
    BuildResult,
    IndexStatus,
    PartitionStatus,
)


def _unit_count() -> int:
    from work_buddy.knowledge.store import load_store
    return len(load_store(scope="all"))


def _cached_vector_count() -> int:
    """How many units have a persisted content vector on disk (model-keyed cache)."""
    from work_buddy.knowledge.index import get_index
    from work_buddy.knowledge.persistence import load_content_cache

    model_key = getattr(get_index(), "_CONTENT_MODEL_KEY", None)
    if not model_key:
        return 0
    return len(load_content_cache(model_key))


def _content_cache_mtime_iso() -> str | None:
    from pathlib import Path

    from work_buddy.knowledge.persistence import cache_status

    info = cache_status().get("content", {})
    path = info.get("path")
    if not path or info.get("missing"):
        return None
    try:
        ts = Path(path).stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return None


class KnowledgeIndexAdapter:
    name = "knowledge"

    def status(self) -> IndexStatus:
        total = _unit_count()
        try:
            vcount = _cached_vector_count()
        except Exception:
            vcount = 0  # cache unreadable → report 0, don't blank the panel
        part = PartitionStatus(
            key="content",
            total_items=total,
            dense_eligible=total,
            vector_count=min(vcount, total),
            pending=max(0, total - vcount),
            last_build=_content_cache_mtime_iso(),
            health="ok",
            detail="dense vectors build lazily in the serving process; counts from on-disk cache",
        )
        return IndexStatus(name=self.name, partitions=[part])

    def lock_key(self) -> str:
        return "knowledge"

    def bulk_build(
        self,
        *,
        full_history: bool = False,
        on_progress: Callable[[BuildProgress], None] | None = None,
    ) -> BuildResult:
        from work_buddy.knowledge.index import rebuild_index

        try:
            stats = rebuild_index(force=full_history)
            return BuildResult(name=self.name, ok=True, stats=stats)
        except Exception as exc:
            return BuildResult(
                name=self.name, ok=False, stats={},
                error=f"{type(exc).__name__}: {exc}",
            )
