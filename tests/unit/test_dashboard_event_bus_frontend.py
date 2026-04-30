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


def test_smart_refresh_event_panel_map_present():
    """The dispatcher must wire every taxonomy event to a panel loader."""
    src = _event_bus_script()
    expected_pairs = {
        "'pool.entry_added'":              "'review'",
        "'pool.entry_state_changed'":      "'review'",
        "'pool.attraction_passes_bumped'": "'review'",
        "'pool.forced_context_stored'":    "'review'",
        "'task.created'":                  "'tasks'",
        "'task.state_changed'":            "'tasks'",
        "'task.description_changed'":      "'tasks'",
        "'component.health_changed'":      "'settings'",
        "'component.preference_changed'":  "'settings'",
        "'llm.call_logged'":               "'costs'",
    }
    for event_key, panel_value in expected_pairs.items():
        assert event_key in src, f"event {event_key} not mapped"
        assert panel_value in src, f"panel {panel_value} not in map"


def test_smart_refresh_skips_when_input_focused():
    src = _event_bus_script()
    # The defining check: a focused INPUT/TEXTAREA in the active panel
    # adds the panel to pendingPanels rather than running the loader.
    assert "_focusedInsidePanel" in src
    assert "pendingPanels.add" in src
    assert "pendingPanels.delete" in src
    # Drain on focusout, debounced.
    assert "addEventListener('focusout'" in src


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
