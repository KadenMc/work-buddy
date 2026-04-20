"""Process-wide registry of :class:`ContextSource` implementations.

Each source module instantiates its implementation and registers it
at import time via :func:`register`. The :class:`ContextCollector`
dispatches requests by looking up registered sources by name.

Registration is idempotent (re-registering the same name replaces
the prior entry — useful for hot-reload during development). Names
must be stable since they key into cache paths on disk.
"""

from __future__ import annotations

from work_buddy.context.types import ContextSource
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


_SOURCES: dict[str, ContextSource] = {}


def register(source: ContextSource) -> None:
    """Add (or replace) a source under its ``name``."""
    name = source.name
    if name in _SOURCES and _SOURCES[name] is not source:
        logger.debug("context.registry: replacing source %r", name)
    _SOURCES[name] = source


def unregister(name: str) -> ContextSource | None:
    """Remove a source by name. Returns the removed instance, or ``None``."""
    return _SOURCES.pop(name, None)


def get(name: str) -> ContextSource | None:
    """Lookup by name. ``None`` when the source isn't registered."""
    return _SOURCES.get(name)


def all_sources() -> dict[str, ContextSource]:
    """Shallow copy of the registry — safe to iterate while sources mutate."""
    return dict(_SOURCES)


def names() -> list[str]:
    """All registered source names, sorted for stable iteration."""
    return sorted(_SOURCES.keys())


def clear() -> None:
    """Drop every registered source. Test-only — production code should not call this."""
    _SOURCES.clear()
