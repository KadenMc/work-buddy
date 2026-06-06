"""Aggregate every index's status into a uniform list for the dashboard panel.

The load-bearing invariant: **one failing index never blanks the panel.** A
raising adapter is caught and rendered as a single ``health="error"`` partition
carrying its message, so the other indexes still report.
"""
from __future__ import annotations

from work_buddy.indexing import registry
from work_buddy.indexing.protocol import IndexStatus, PartitionStatus
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def aggregate_status() -> list[IndexStatus]:
    """Return one :class:`IndexStatus` per registered index (errors degraded, not raised)."""
    out: list[IndexStatus] = []
    for name in registry.index_names():
        try:
            out.append(registry.get_index(name).status())
        except Exception as exc:  # one bad index must not blank the whole panel
            logger.warning("indexing: status for %r failed: %s", name, exc)
            out.append(
                IndexStatus(
                    name=name,
                    partitions=[
                        PartitionStatus(
                            key=name,
                            total_items=0,
                            dense_eligible=0,
                            vector_count=0,
                            pending=0,
                            health="error",
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    ],
                )
            )
    return out
