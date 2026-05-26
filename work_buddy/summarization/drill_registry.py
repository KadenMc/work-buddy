"""Per-source drill-handler registry for the unified search funnel.

Phase 2 (PR #131) shipped the `summary_search` funnel with a single
`_default_drill_handler` hardcoded to the `summary` source. This registry
generalizes that pattern: any IR source can register its own drill handler
that takes a top-ranked hit and returns the source-specific raw content
(e.g. for the `summary` source, the handler routes by `namespace` to
`session_search` per-session for `conversation_session`).

Pattern mirrors `work_buddy/disclosure/registry.py` — lazy factory under
`_register_defaults()` so importing this module does not pull in heavy
backends; the actual handler module is imported only on first use.

The registry is keyed by **IR source name** (`"summary"`, `"conversation"`,
`"chrome"`, ...), not by `namespace`. The `summary` source's handler
performs its own namespace dispatch internally — that dispatch was the
original `_default_drill_handler` and now lives at
`work_buddy.summarization.funnel._summary_namespace_drill_dispatch`.
"""

from __future__ import annotations

from typing import Any, Callable

# Drill handler signature (legacy summary-funnel shape, preserved):
#   (namespace, item_id, query, method, top_k) -> Any
#
# `namespace` is the summary namespace at call time (always equal to the
# source name for non-`summary` sources, since they have no internal
# namespace concept — for those, the handler can ignore it).
DrillHandler = Callable[[str, str, str, str, int], Any]


_HANDLERS: dict[str, DrillHandler] = {}


def register_drill_handler(source: str, handler: DrillHandler) -> None:
    """Register a drill handler for an IR source. Idempotent by name."""
    _HANDLERS[source] = handler


def get_drill_handler(source: str) -> DrillHandler | None:
    """Return the handler for `source`, or `None` if unregistered."""
    return _HANDLERS.get(source)


def available_sources() -> list[str]:
    """Return the list of sources with a registered drill handler, sorted."""
    return sorted(_HANDLERS.keys())


def _reset_for_tests() -> None:
    """Clear the registry. Test-only — never call from production code."""
    _HANDLERS.clear()


# ---------------------------------------------------------------------------
# Default registrations
# ---------------------------------------------------------------------------


def _register_defaults() -> None:
    """Register the built-in drill handlers. Lazy-imported handler bodies
    avoid pulling backends into the import path until first use."""

    def _summary_handler(
        namespace: str,
        item_id: str,
        query: str,
        method: str,
        top_k: int,
    ) -> Any:
        from work_buddy.summarization.funnel import (
            _summary_namespace_drill_dispatch,
        )

        return _summary_namespace_drill_dispatch(
            namespace, item_id, query, method, top_k,
        )

    register_drill_handler("summary", _summary_handler)


_register_defaults()
