"""Event Sources — adapters that normalize external state into ``Event``s.

A source is a user-authored ``.md`` file (``kind: event_source``) under
``.data/event_sources/``, validated like a user job and hot-reloaded. The
loader turns it into an :class:`EventSourceDef`; the poller drives the
reconciling fetch → diff → emit loop.
"""

from __future__ import annotations

from work_buddy.events.sources.definition import (
    EventSourceDef,
    parse_interval,
    parse_source_md,
    validate_source_fm,
)

__all__ = [
    "EventSourceDef",
    "parse_interval",
    "parse_source_md",
    "validate_source_fm",
]
