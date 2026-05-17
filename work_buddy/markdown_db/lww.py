"""Last-write-wins metadata log — the ``lww_meta`` abstraction.

The :class:`LwwLog` protocol is the storage interface the
:class:`~work_buddy.markdown_db.base.MarkdownDB` orchestration depends
on. It being a protocol keeps the orchestration decoupled from storage,
so a :class:`MarkdownDB` can run against an in-memory log, a no-op log,
or the durable SQLite log without code changes. Three implementations:

- :class:`NullLwwLog` — records nothing, knows nothing. The default. A
  :class:`MarkdownDB` wired with it falls back to pure markdown-canonical
  conflict resolution (markdown always wins), which is exactly the
  behaviour of the legacy ``obsidian/tasks/sync.py`` reconciler.
- :class:`InMemoryLwwLog` — dict-backed, append-only. For unit tests and
  for exercising the LWW resolution paths without a database.
- :class:`~work_buddy.markdown_db.sqlite_lww.SqliteLwwLog` — backed by
  the per-DB ``lww_meta`` table; the durable production implementation.

The log is **append-only** by contract: :meth:`LwwLog.record` adds a
write event, it never overwrites. :meth:`LwwLog.latest` returns the most
recent event for a ``(table, pk, field, surface)`` tuple. The append-only
shape serves LWW today (read the latest row) and is replayable as an op
log if a CRDT resolver is ever introduced.

See ``architecture/markdown-db`` for the ``lww_meta`` schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from work_buddy.markdown_db.types import Surface, WriteProvenance


@dataclass(frozen=True)
class LwwEntry:
    """One recorded write event for a single field on a single surface."""

    ts: datetime
    provenance: WriteProvenance
    to_surface: Surface


class LwwLog(Protocol):
    """Append-only per-field write-event log.

    Implementations must treat :meth:`record` as append-only — every
    call adds an event; none replace a prior one. :meth:`latest` reads
    back the most recent event for a coordinate.
    """

    def record(
        self,
        *,
        table: str,
        pk: str,
        field: str,
        ts: datetime,
        provenance: WriteProvenance,
        to_surface: Surface,
    ) -> None:
        """Append a write event for ``table.pk.field`` landing on ``to_surface``."""
        ...

    def latest(
        self, *, table: str, pk: str, field: str, surface: Surface,
    ) -> LwwEntry | None:
        """Most recent event for ``table.pk.field`` on ``surface``, or ``None``."""
        ...


class NullLwwLog:
    """A :class:`LwwLog` that records nothing and remembers nothing.

    The default log. With it, :meth:`MarkdownDB.resolve` has no
    timestamps to compare and falls back to its markdown-canonical
    default (markdown wins). This is intentional: the abstraction is
    fully usable — and behaviourally identical to the legacy tasks
    reconciler — before the SQLite ``lww_meta`` backend exists.
    """

    def record(
        self,
        *,
        table: str,
        pk: str,
        field: str,
        ts: datetime,
        provenance: WriteProvenance,
        to_surface: Surface,
    ) -> None:
        return None

    def latest(
        self, *, table: str, pk: str, field: str, surface: Surface,
    ) -> LwwEntry | None:
        return None


class InMemoryLwwLog:
    """Dict-backed append-only :class:`LwwLog` for tests and dry runs.

    Stores every event in insertion order. :meth:`latest` scans for the
    newest event matching the coordinate. Not thread-safe; not durable.
    """

    def __init__(self) -> None:
        # (table, pk, field, surface) → list[LwwEntry], append-only.
        self._events: dict[tuple[str, str, str, Surface], list[LwwEntry]] = {}

    def record(
        self,
        *,
        table: str,
        pk: str,
        field: str,
        ts: datetime,
        provenance: WriteProvenance,
        to_surface: Surface,
    ) -> None:
        key = (table, pk, field, to_surface)
        self._events.setdefault(key, []).append(
            LwwEntry(ts=ts, provenance=provenance, to_surface=to_surface)
        )

    def latest(
        self, *, table: str, pk: str, field: str, surface: Surface,
    ) -> LwwEntry | None:
        events = self._events.get((table, pk, field, surface))
        if not events:
            return None
        return max(events, key=lambda e: e.ts)

    # ── Test / introspection helpers ────────────────────────────────

    def all_events(self) -> list[tuple[str, str, str, LwwEntry]]:
        """Every recorded event as ``(table, pk, field, entry)`` tuples."""
        out: list[tuple[str, str, str, LwwEntry]] = []
        for (table, pk, field, _surface), entries in self._events.items():
            for e in entries:
                out.append((table, pk, field, e))
        return out

    def event_count(self) -> int:
        """Total number of recorded events across all coordinates."""
        return sum(len(v) for v in self._events.values())
