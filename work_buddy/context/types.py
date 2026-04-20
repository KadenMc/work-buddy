"""Core types for the :mod:`work_buddy.context` pipeline.

The shape of a context request, the section each source produces, and
the ``ContextSource`` protocol every adapter implements. Deliberately
small and boring — the value of the refactor is in the discipline of
ONE request shape across all callers, not in fancy abstractions.

``ContextDepth`` is an *ordinal* — per-source renderers map it onto
source-specific meanings ("BRIEF = 5 commits" vs "BRIEF = titles-only
for the top 5 tasks"). Each source documents its own mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable


class ContextDepth(IntEnum):
    """How much detail a source should render.

    Ordinal so ``depth >= ContextDepth.NORMAL`` comparisons are natural.
    Each source maps these onto its own semantics in ``render()``.
    """

    BRIEF = 1
    """Titles-only / top-N counts. Shortest rendering."""

    NORMAL = 2
    """Default. Balanced detail — one-liners + small context."""

    DEEP = 3
    """Full-fidelity. Include body text, longer windows, metadata."""


@dataclass(frozen=True)
class ContextRequest:
    """Caller-facing handle describing what context to gather.

    Only fields that affect *fetch* (sources, target_date, window_days,
    per-source custom params) participate in the cache bucket key.
    ``depth``, ``per_source_depth`` and ``max_chars`` are *curation*
    concerns and don't influence the cached raw JSON.

    Attributes:
        sources: Subset of registered source names. ``None`` = all.
        exclude: Names to drop from the default all-sources set.
            Ignored when ``sources`` is explicit.
        depth: Global default rendering depth.
        per_source_depth: Overrides per source name. Unspecified
            sources use ``depth``.
        target_date: Reference date for time-windowed sources.
            ``None`` = now.
        window_days: Window around ``target_date`` for sources that
            accept one. Defaults to 1 (today only).
        max_chars: Rendering budget. Curator truncates when output
            would exceed. ``None`` = unlimited.
        max_age_seconds: Cache-freshness floor. ``None`` = always
            refetch. ``0`` = use cache if any exists.
        custom: Per-source ad-hoc parameter overrides. Keys are source
            names; values are dicts forwarded into that source's
            ``collect()``.
    """

    sources: list[str] | None = None
    exclude: list[str] | None = None
    depth: ContextDepth = ContextDepth.NORMAL
    per_source_depth: dict[str, ContextDepth] | None = None
    target_date: date | None = None
    window_days: int = 1
    max_chars: int | None = None
    max_age_seconds: int | None = None
    custom: dict[str, dict[str, Any]] | None = None

    def depth_for(self, source: str) -> ContextDepth:
        """Return the effective depth for a given source."""
        if self.per_source_depth and source in self.per_source_depth:
            return self.per_source_depth[source]
        return self.depth

    def custom_for(self, source: str) -> dict[str, Any]:
        """Return the custom param dict for a source (empty if none)."""
        if self.custom and source in self.custom:
            return dict(self.custom[source])
        return {}


@dataclass
class ContextSection:
    """A source's raw contribution to a :class:`Context`.

    The ``items`` field holds JSON-serializable data — anything the
    source can reconstitute from its cache file on a subsequent load.
    ``metadata`` is for source-specific annotations (total counts,
    fetch-time signals, cache-invalidation hints) that inform rendering
    without polluting the items list.

    :attr:`ContextSource.render` turns a :class:`ContextSection` into
    the markdown block callers see.
    """

    source: str
    items: list[Any] = field(default_factory=list)
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form. Used by the cache layer."""
        return {
            "source": self.source,
            "items": self.items,
            "fetched_at": self.fetched_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextSection:
        """Rehydrate from a cache file."""
        raw_dt = data.get("fetched_at", "")
        try:
            fetched = datetime.fromisoformat(raw_dt) if raw_dt else datetime.now(timezone.utc)
        except ValueError:
            fetched = datetime.now(timezone.utc)
        return cls(
            source=data.get("source", ""),
            items=list(data.get("items", []) or []),
            fetched_at=fetched,
            metadata=dict(data.get("metadata", {}) or {}),
        )


@dataclass
class Context:
    """The collected output of a :class:`ContextRequest`.

    A dict of :class:`ContextSection` keyed by source name, plus the
    original request (so curators have the caller's depth/window
    preferences available without re-plumbing them).
    """

    sections: dict[str, ContextSection] = field(default_factory=dict)
    request: ContextRequest = field(default_factory=ContextRequest)

    def section(self, source: str) -> ContextSection | None:
        """Convenience lookup — returns ``None`` if the source wasn't collected."""
        return self.sections.get(source)

    def has(self, source: str) -> bool:
        return source in self.sections and bool(self.sections[source].items)


@runtime_checkable
class ContextSource(Protocol):
    """Protocol every context source implements.

    Sources are stateless registration units — an adapter module
    instantiates one, registers it via
    :func:`work_buddy.context.registry.register`, and the
    :class:`ContextCollector` dispatches by name.

    ``collect`` fetches raw data. ``render`` turns a cached section
    into markdown at a chosen depth. ``is_stale`` lets a source
    self-invalidate its cache on cheap external checks (git HEAD sha,
    store mtime, ledger tail) — default implementation is always-fresh
    so sources that can't cheaply self-check can skip it.

    ``drill_down`` is for the ``context_drill_down`` MCP capability —
    less-capable agents ask for more detail on a specific item by id
    and field. Default raises ``NotImplementedError`` until a source
    opts in.
    """

    @property
    def name(self) -> str:
        """Stable identifier used in ``sources=`` / cache paths."""
        ...

    def collect(self, request: ContextRequest) -> ContextSection:
        """Fetch raw data. Must be JSON-serializable via ``section.to_dict()``."""
        ...

    def render(
        self,
        section: ContextSection,
        depth: ContextDepth,
    ) -> str:
        """Render a section into a markdown block at the given depth."""
        ...

    def is_stale(
        self,
        cached: ContextSection,
        request: ContextRequest,
    ) -> bool:
        """Return True if ``cached`` should be re-fetched.

        Called even when ``max_age_seconds`` hasn't elapsed, so sources
        with cheap self-validation (``git rev-parse HEAD``, SQLite
        mtime, chrome ledger tail ts) can report "the world changed
        under me" without waiting for the age floor.

        Default implementation returns ``False`` — the age floor is the
        only gate.
        """
        ...

    def drill_down(
        self,
        item_id: str,
        field: str,
    ) -> dict[str, Any]:
        """Return full detail for one item on demand.

        ``item_id`` identifies an item within this source's domain
        (task id, commit sha, project slug, …). ``field`` selects
        which expansion the caller wants ("note", "full_message",
        "description", "diff_stats"). Sources that haven't opted in
        raise ``NotImplementedError``.
        """
        ...


class BaseContextSource:
    """Convenience base class providing sensible defaults.

    Concrete sources override ``collect`` and ``render`` at minimum.
    ``is_stale`` and ``drill_down`` only need overriding for sources
    that want cheap self-invalidation or drill-down support.

    Subclasses must set :attr:`name` either as a class attribute or
    via ``__init__``; the default ``AttributeError`` is loud on
    purpose.
    """

    name: str = ""  # concrete sources override

    def collect(self, request: ContextRequest) -> ContextSection:  # pragma: no cover
        raise NotImplementedError(
            f"{type(self).__name__}.collect must be implemented by the source."
        )

    def render(
        self,
        section: ContextSection,
        depth: ContextDepth,
    ) -> str:  # pragma: no cover
        raise NotImplementedError(
            f"{type(self).__name__}.render must be implemented by the source."
        )

    def is_stale(
        self,
        cached: ContextSection,
        request: ContextRequest,
    ) -> bool:
        """Default: cache is never stale by source signal.

        The collector's ``max_age_seconds`` floor still applies — this
        only says "the source itself has no cheap way to self-check."
        """
        return False

    def drill_down(
        self,
        item_id: str,
        field: str,
    ) -> dict[str, Any]:
        """Default: no drill-down support. Phase-7 sources override."""
        raise NotImplementedError(
            f"{type(self).__name__!r} does not support context_drill_down yet."
        )
