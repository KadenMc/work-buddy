"""Cleanup adapter framework — Stage 4 deliverable.

When a user clicks the Clean Up button on a Thread card (UX.md §6),
the inciting source is mutated (e.g., delete the journal note line).
Different inciting sources need different mutation logic; each
source registers a CleanupAdapter.

The user's intent for Clean Up: "the work this Thread represents
is already done outside the system — clean up the source so the
agent doesn't keep proposing it."

Stage 4.0 ships the framework + registry. Stage 4.4 wires the
journal-note adapter (the canonical first case). Stage 4.13 adds
the Chrome adapter when the Chrome pipeline migrates to v5.

UX.md §6 is the spec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of an adapter's cleanup attempt.

    ``source_already_gone`` is True when the cleanup target couldn't
    be found (e.g., user manually edited the journal already).
    Treated as success — the user's intent ("this is handled") is
    fulfilled either way.
    """

    success: bool
    detail: Optional[str] = None       # for the cleanup event log
    source_already_gone: bool = False  # True iff target was missing


# ---------------------------------------------------------------------------
# Adapter type
# ---------------------------------------------------------------------------


CanCleanUpFn = Callable[["object"], bool]   # type-hinted as Thread at call site
CleanupFn = Callable[["object"], CleanupResult]


@dataclass(frozen=True)
class CleanupAdapter:
    """Registry entry. Each inciting-event source registers one.

    ``source`` matches ``Thread.inciting_event_summary['source']``.
    ``can_clean_up(thread)`` lets the adapter say no per-Thread
    (e.g., a journal adapter that requires a known line number).
    ``cleanup(thread)`` performs the mutation and returns a
    CleanupResult.
    """

    source: str
    can_clean_up: CanCleanUpFn
    cleanup: CleanupFn

    # Human-readable description of what cleanup does for this
    # source. Used in the UI's hover tooltip on the Clean Up button.
    description: str = ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_ADAPTERS: dict[str, CleanupAdapter] = {}


def register_cleanup_adapter(adapter: CleanupAdapter) -> None:
    """Register an adapter for a given inciting-event source.

    Re-registration replaces the existing adapter — useful for
    tests and live re-loading.
    """
    _ADAPTERS[adapter.source] = adapter


def clear_cleanup_adapters() -> None:
    """Test/utility: drop all registered adapters."""
    _ADAPTERS.clear()


def get_cleanup_adapter(source: str) -> Optional[CleanupAdapter]:
    """Look up the adapter for a given source. None if unregistered."""
    return _ADAPTERS.get(source)


def find_cleanup_adapter(thread) -> Optional[CleanupAdapter]:  # type: ignore[no-untyped-def]
    """Look up the adapter for a Thread by its inciting source.

    Reads ``thread.inciting_event_summary['source']``. Returns None
    if the source has no registered adapter.
    """
    summary = getattr(thread, "inciting_event_summary", None) or {}
    source = summary.get("source")
    if source is None:
        return None
    return _ADAPTERS.get(source)


def can_clean_up(thread) -> bool:  # type: ignore[no-untyped-def]
    """True iff a cleanup adapter is registered for ``thread``'s
    inciting source AND the adapter declares it can clean up this
    specific Thread.

    Used by the UI to decide whether to show the Clean Up button.
    """
    adapter = find_cleanup_adapter(thread)
    if adapter is None:
        return False
    try:
        return bool(adapter.can_clean_up(thread))
    except Exception as e:
        logger.warning(
            "can_clean_up adapter for %r raised %s; treating as False",
            adapter.source, e,
        )
        return False


def perform_cleanup(thread) -> CleanupResult:  # type: ignore[no-untyped-def]
    """Invoke the adapter for ``thread``. Returns a CleanupResult.

    If no adapter is registered, returns a failure result (caller
    should never call this without first checking ``can_clean_up``,
    but defensive).
    """
    adapter = find_cleanup_adapter(thread)
    if adapter is None:
        return CleanupResult(
            success=False,
            detail="no cleanup adapter registered for this source",
        )
    try:
        return adapter.cleanup(thread)
    except Exception as e:
        logger.exception(
            "Cleanup adapter %r raised %s", adapter.source, e,
        )
        return CleanupResult(
            success=False,
            detail=f"adapter raised {type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def registered_sources() -> list[str]:
    """List every source with a registered adapter. Useful for
    docs / logs / sidecar startup banners."""
    return sorted(_ADAPTERS)
