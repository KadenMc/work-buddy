"""The ``Index`` protocol and its status/progress value types.

Every index the seam drives implements :class:`Index` — the seam never knows
about conversations, vaults, or knowledge units; it drives ``Index``es made of
``partition``s (a partition = a vault id, an IR source name, a knowledge slice).

These dataclasses are the wire shape the dashboard renders. They are frozen and
``dataclasses.asdict``-friendly so a route can ``jsonify`` them directly.

``IndexStatus`` carries an optional index-level ``size_on_disk_mb`` for stores whose
partitions share one DB (the vault index is one SQLite file across all vaults, so a
per-partition size is not separable).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class PartitionStatus:
    """One slice of an index (a vault id, an IR source, a knowledge slice)."""

    key: str
    total_items: int          # rows / docs / chunks in this partition
    dense_eligible: int       # items that should carry a vector
    vector_count: int         # items that do
    pending: int              # dense_eligible - vector_count (the real backlog)
    last_build: str | None = None        # ISO timestamp
    size_on_disk_mb: float | None = None
    health: str = "ok"        # "ok" | "unreachable" | "error"
    detail: str | None = None  # error / unreachability message — surfaced, not swallowed


@dataclass(frozen=True)
class IndexStatus:
    """A whole index's status: a name and its partitions."""

    name: str                 # "ir" | "vault_index" | "knowledge"
    partitions: list[PartitionStatus]
    last_build_duration_s: float | None = None  # drives the progress-affordance threshold
    size_on_disk_mb: float | None = None         # index-level size (shared-DB stores)


@dataclass(frozen=True)
class BuildProgress:
    """Emitted per checkpoint during a bulk build; consumed by the panel."""

    phase: str                # "scanning" | "chunking" | "embedding" | "idle"
    items_done: int = 0
    items_total: int = 0
    throughput: float | None = None
    eta_s: float | None = None


@dataclass(frozen=True)
class BuildResult:
    """The outcome of a bulk build."""

    name: str
    ok: bool
    stats: dict
    error: str | None = None


@runtime_checkable
class Index(Protocol):
    """The one small contract every index reports through."""

    @property
    def name(self) -> str: ...

    def status(self) -> IndexStatus: ...

    def lock_key(self) -> str:
        """A per-index advisory-lock identity (used to show a 'building…' state)."""
        ...

    def bulk_build(
        self,
        *,
        full_history: bool = False,
        on_progress: Callable[[BuildProgress], None] | None = None,
    ) -> BuildResult: ...
