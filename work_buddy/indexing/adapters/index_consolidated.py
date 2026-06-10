"""``Index`` adapter for the consolidated index (flag-gated).

Surfaces the consolidated index in the same dashboard/status/build seam as the IR,
vault, and knowledge indexes. ``bulk_build`` is a no-op unless ``index.enabled`` — the
consolidated index never builds (or touches its DB) on the live path until the flag is
flipped. ``status`` is always safe (pure store reads; empty before first build).
"""

from __future__ import annotations

from typing import Callable

from work_buddy.indexing.protocol import (
    BuildProgress,
    BuildResult,
    IndexStatus,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class ConsolidatedIndexAdapter:
    name = "consolidated"

    def status(self) -> IndexStatus:
        try:
            from work_buddy.index.partitioned import UnifiedIndex
            return UnifiedIndex().status()
        except Exception as exc:  # never blank the panel on one bad index
            logger.debug("consolidated status failed: %s", exc)
            return IndexStatus(name=self.name, partitions=[])

    def lock_key(self) -> str:
        from work_buddy.index.config import load_index_config
        return str(load_index_config().resolved_db_path())

    def bulk_build(
        self, *, full_history: bool = False,
        on_progress: Callable[[BuildProgress], None] | None = None,
    ) -> BuildResult:
        from work_buddy.index.config import load_index_config
        cfg = load_index_config()
        if not cfg.enabled:
            return BuildResult(
                name=self.name, ok=True,
                stats={"skipped": "index.enabled is false (flag-gated; off by default)"},
            )
        try:
            from work_buddy.index.partitioned import UnifiedIndex
            stats = UnifiedIndex(config=cfg).build_all(force=full_history)
            return BuildResult(name=self.name, ok=True, stats={"partitions": stats})
        except Exception as exc:
            return BuildResult(
                name=self.name, ok=False, stats={},
                error=f"{type(exc).__name__}: {exc}",
            )
