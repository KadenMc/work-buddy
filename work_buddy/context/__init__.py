"""Unified context collection + curation for work-buddy.

Two-stage pipeline:

1. :class:`ContextCollector` — fetch raw signals from registered
   :class:`ContextSource` implementations. Each source produces
   :class:`ContextSection`\\ s of raw JSON-serializable data; results
   are cached under ``data/context/<source>/<bucket>.json`` keyed by
   the request parameters that affect fetch (not by depth — depth is
   a curation concern).
2. :class:`ContextCurator` — render a cached :class:`Context` into a
   markdown block or JSON payload, respecting depth / per-source depth
   / max_chars / target_date filters. Multiple curators can run over
   the same cached fetch without re-hitting the sources.

The split lets callers (LLM prompts, morning routine, Sonnet/Opus
agents) re-compose context on demand without paying for fresh
collection every time. Sonnet/Opus agents can call the curator via
the ``curate_context`` MCP capability to build their own views.

This module replaces the ad-hoc ``work_buddy/collectors/*`` producers
and the duplicate signal-gathering in ``work_buddy/triage/recommend``
and ``work_buddy/triage/adapters/*``. Migrations land in later phases
of the refactor — see plan
``C:\\Users\\Owner\\.claude\\plans\\1-bundle-writer-playful-llama.md``.
"""

from __future__ import annotations

from work_buddy.context.types import (
    BaseContextSource,
    Context,
    ContextDepth,
    ContextRequest,
    ContextSection,
    ContextSource,
)

__all__ = [
    "BaseContextSource",
    "Context",
    "ContextDepth",
    "ContextRequest",
    "ContextSection",
    "ContextSource",
]
