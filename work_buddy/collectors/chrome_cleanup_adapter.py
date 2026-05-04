"""Chrome-tab cleanup adapter — wires the thread-level "Clean Up"
button on legacy Chrome-tab sub-threads (one tab = one thread, the
pre-rebuild shape) to the working ``chrome_collector.close_tabs``.

In the new unified pipeline shape, Chrome tabs live as ContextItems
on group sub-threads — the "Clean Up" button doesn't surface on
items. This adapter therefore only matters for any legacy chrome_tab
sub-threads still in the DB. New runs don't produce them.

Used to be a stub returning "not yet wired" because the previous
audit didn't realize the Chrome native-messaging host supports
mutations. It does — see ``work_buddy/collectors/chrome_collector.py``,
``chrome_extension/background.js``. So this adapter now actually
closes the tab.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _chrome_tab_can_clean_up(thread) -> bool:  # type: ignore[no-untyped-def]
    summary = getattr(thread, "inciting_event_summary", None) or {}
    return summary.get("source") == "chrome_tab"


def _chrome_tab_cleanup(thread):  # type: ignore[no-untyped-def]
    """Close the Chrome tab represented by ``thread``.

    The thread's ``inciting_event_summary`` should carry a
    ``tab_id`` (set when the legacy chrome path created the thread).
    If absent, we surface a friendly failure rather than guessing.
    """
    from work_buddy.threads.cleanup import CleanupResult

    summary = getattr(thread, "inciting_event_summary", None) or {}
    tab_id = summary.get("tab_id")
    if tab_id is None:
        # Some legacy threads stored the tab_id under the first
        # context item's payload instead. Try that as a fallback.
        try:
            ci = (thread.context_items or ())[0]
            tab_id = (ci.payload or {}).get("tab_id")
        except (IndexError, AttributeError):
            tab_id = None
    if tab_id is None:
        return CleanupResult(
            success=False,
            detail=(
                "No tab_id on this thread's inciting summary or first "
                "context item; can't determine which Chrome tab to "
                "close."
            ),
        )

    try:
        from work_buddy.collectors.chrome_collector import close_tabs
        result = close_tabs([int(tab_id)])
    except Exception as e:
        logger.warning(
            "chrome_tab_cleanup: close_tabs raised: %s", e,
        )
        return CleanupResult(success=False, detail=str(e))

    if result is None or not result.get("ok"):
        return CleanupResult(
            success=False,
            detail=(
                f"close_tabs reported failure: "
                f"{(result or {}).get('error') or 'no response'}"
            ),
        )
    return CleanupResult(
        success=True,
        detail=f"Closed Chrome tab {tab_id}",
    )


def register_chrome_tab_cleanup_adapter() -> None:
    """Register the Chrome-tab cleanup adapter with the cleanup
    registry. Bootstrap calls this alongside the journal adapter."""
    from work_buddy.threads.cleanup import (
        CleanupAdapter, register_cleanup_adapter,
    )
    register_cleanup_adapter(CleanupAdapter(
        source="chrome_tab",
        can_clean_up=_chrome_tab_can_clean_up,
        cleanup=_chrome_tab_cleanup,
        description="Close the Chrome tab via the native-messaging host.",
    ))
