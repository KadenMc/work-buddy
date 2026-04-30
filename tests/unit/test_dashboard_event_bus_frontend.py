"""Smoke tests for the browser-side event-bus dispatcher.

The dispatcher is JS embedded in a Python string. We test:

1. The string exposes the expected window.eventBus API surface.
2. ``render_page()`` includes the dispatcher and emits it before any
   other script that might register a handler at load time.
3. The header gained an ``#event-bus-status`` indicator.
"""

from __future__ import annotations

from work_buddy.dashboard.frontend import render_page
from work_buddy.dashboard.frontend.script_event_bus import _event_bus_script


def test_dispatcher_exposes_public_api():
    src = _event_bus_script()
    # Public API
    assert "window.eventBus" in src
    assert "on(eventType, handler)" in src
    assert "off(eventType, handler)" in src
    assert "isConnected()" in src
    # EventSource wiring
    assert "new EventSource('/api/events')" in src
    # Handler dispatch by event_type
    assert "event.event_type" in src or "evt.event_type" in src


def test_dispatcher_handles_connection_lifecycle():
    src = _event_bus_script()
    # Open / error / message listeners are all wired.
    assert "addEventListener('open'" in src
    assert "addEventListener('error'" in src
    assert "addEventListener('message'" in src
    # Status indicator updates on lifecycle.
    assert "_setStatus('connected'" in src
    assert "_setStatus('disconnected'" in src


def test_render_page_emits_bus_before_other_scripts():
    """The bus must be defined before any other module's load-time code,
    so handlers can be registered synchronously without races."""
    page = render_page()
    bus_pos = page.find("window.eventBus = {")
    main_pos = page.find("function switchTab")  # core script_main marker
    assert bus_pos > 0, "bus script not in page"
    assert main_pos > 0, "main script not in page"
    assert bus_pos < main_pos, "bus must precede main script"


def test_header_has_event_bus_status_indicator():
    page = render_page()
    assert 'id="event-bus-status"' in page


def test_dispatcher_routes_pool_events_to_review_surface_mutators():
    """The dispatcher must call per-card mutators on window.reviewSurface,
    not panel-wide loaders. See architecture/event-bus."""
    src = _event_bus_script()
    # All four pool events are handled.
    assert "'pool.entry_added'" in src
    assert "'pool.entry_state_changed'" in src
    assert "'pool.attraction_passes_bumped'" in src
    assert "'pool.forced_context_stored'" in src
    # Each calls a per-card mutator on reviewSurface (via _withSurface).
    assert "appendCard" in src
    assert "removeCard" in src
    assert "updateCard" in src
    assert "bumpAttractionPasses" in src
    assert "setForcedContextStored" in src
    # Terminal-state list drives remove vs update branching.
    assert "'reviewed'" in src and "'quarantined'" in src


def test_dispatcher_uses_isMounted_guard_for_all_surfaces():
    """Each handler must skip when its surface isn't mounted.
    switchTab refreshes the surface fresh on next visit — never
    fall back to a wholesale loader."""
    src = _event_bus_script()
    assert "_withSurface" in src
    assert "isMounted" in src


def test_no_wholesale_loader_calls_in_event_handlers():
    """Regression guardrail: SSE handler bodies MUST NOT call any
    panel-wide loader. The smart-refresh approach (deleted) routed
    events to staticLoaders[panel](), which wholesale-rewrote
    container.innerHTML and wiped sibling drafts.

    See architecture/event-bus dev_notes for the regression history.
    """
    src = _event_bus_script()
    forbidden = [
        "loadReview(",
        "loadTasks(",
        "loadSettings(",
        "loadCosts(",
        "refreshCostsData(",
        "staticLoaders[",
        "_smartRefresh",
        "_panelHasUserContent",
        "pendingPanels",
    ]
    for pattern in forbidden:
        assert pattern not in src, (
            f"forbidden wholesale-refresh pattern present in dispatcher: "
            f"{pattern!r}. SSE handlers must call per-card surface "
            f"mutators (e.g. window.reviewSurface.appendCard) only."
        )


def test_review_surface_handle_uses_morphdom():
    """The Review surface's updateCard must use morphdom for surgical
    diffing — not container.innerHTML rewrites or hand-rolled
    attribute mutators that miss edge cases. Phoenix LiveView /
    Hotwire convention."""
    from work_buddy.dashboard.frontend.script_triage import (
        _triage_review_script,
    )
    src = _triage_review_script()
    assert "window.morphdom" in src or "morphdom(" in src
    # Sanity: the handle's mutators are present on the return value.
    assert "appendCard" in src and "removeCard" in src and "updateCard" in src


def test_review_surface_announces_via_aria_live():
    """removeCard / appendCard must write to a polite aria-live region
    so screen-reader users hear card transitions. WCAG 4.1.3 AA."""
    from work_buddy.dashboard.frontend.script_triage import (
        _triage_review_script,
    )
    src = _triage_review_script()
    assert "role" in src and "'status'" in src
    assert "aria-live" in src
    assert "_announce(" in src


def test_review_surface_has_pending_removals_set():
    """Ordering protection: in-process state-change events can arrive
    before cross-process add events for the same card. removeCard
    must record the key in _pendingRemovals when no card is found,
    and appendCard must consult that set before mounting."""
    from work_buddy.dashboard.frontend.script_triage import (
        _triage_review_script,
    )
    src = _triage_review_script()
    assert "_pendingRemovals" in src


def test_legacy_30s_timer_is_gone():
    """Regression guardrail: the global panel-refresh setInterval and
    the dataRefreshers table that disguised destructive rewrites as
    data-only refreshes must not return. Future agents touching the
    dashboard should not re-introduce a global timer.

    See knowledge: 'Refresh-bug guardrail' in services/dashboard.
    """
    from work_buddy.dashboard.frontend.script_main import _script
    src = _script()
    # Only updateClock may use setInterval — no panel-refresh interval.
    interval_lines = [l for l in src.splitlines() if "setInterval(" in l]
    for line in interval_lines:
        assert "updateClock" in line, (
            f"Unexpected setInterval (only updateClock is allowed): {line!r}"
        )
    # dataRefreshers / startAutoRefresh: prose comments may reference them
    # historically, but no code (assignment, indexing, or call) may use them.
    code_patterns = [
        "dataRefreshers =",
        "dataRefreshers[",
        "dataRefreshers.",
        "startAutoRefresh(",
        "function startAutoRefresh",
    ]
    for pat in code_patterns:
        assert pat not in src, (
            f"legacy refresh code still present: {pat!r}"
        )


def test_visibilitychange_listener_refreshes_active_tab():
    from work_buddy.dashboard.frontend.script_main import _script
    src = _script()
    assert "addEventListener('visibilitychange'" in src
    assert "document.visibilityState" in src
