"""Smoke tests for the browser-side event-bus dispatcher.

The dispatcher is JS embedded in a Python string. We test:

1. The string exposes the expected window.eventBus API surface.
2. ``render_page()`` includes the dispatcher and emits it before any
   other script that might register a handler at load time.
3. The header gained an ``#event-bus-status`` indicator.
"""

from __future__ import annotations

from pathlib import Path

from work_buddy.dashboard.frontend import render_page
from work_buddy.dashboard.frontend.scripts.core.event_bus import script as _event_bus_script


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
    from work_buddy.dashboard.frontend.scripts.surfaces.triage import (
        review_script,
    )
    src = review_script()
    assert "window.morphdom" in src or "morphdom(" in src
    # Sanity: the handle's mutators are present on the return value.
    assert "appendCard" in src and "removeCard" in src and "updateCard" in src


def test_review_surface_announces_via_aria_live():
    """removeCard / appendCard must write to a polite aria-live region
    so screen-reader users hear card transitions. WCAG 4.1.3 AA."""
    from work_buddy.dashboard.frontend.scripts.surfaces.triage import (
        review_script,
    )
    src = review_script()
    assert "role" in src and "'status'" in src
    assert "aria-live" in src
    assert "_announce(" in src


def test_review_surface_has_pending_removals_set():
    """Ordering protection: in-process state-change events can arrive
    before cross-process add events for the same card. removeCard
    must record the key in _pendingRemovals when no card is found,
    and appendCard must consult that set before mounting."""
    from work_buddy.dashboard.frontend.scripts.surfaces.triage import (
        review_script,
    )
    src = review_script()
    assert "_pendingRemovals" in src


def test_legacy_30s_timer_is_gone():
    """Regression guardrail: the global panel-refresh setInterval and
    the dataRefreshers table that disguised destructive rewrites as
    data-only refreshes must not return. Future agents touching the
    dashboard should not re-introduce a global timer.

    See knowledge: 'Refresh-bug guardrail' in services/dashboard.
    """
    from work_buddy.dashboard.frontend.scripts.core.page import script
    src = script()
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
    from work_buddy.dashboard.frontend.scripts.core.page import script
    src = script()
    assert "addEventListener('visibilitychange'" in src
    assert "document.visibilityState" in src


def test_assembled_javascript_init_runs():
    """Eval the rendered <script> in a stubbed Node context and confirm it
    completes without throwing.

    Runtime smoke test that catches what ``--check`` (syntax-only)
    misses. Particularly: TDZ ReferenceErrors when a module's top-level
    code touches a ``let``/``const`` from a module that hasn't evaluated
    yet (the bug class that produced
    'Cannot access "_jobRegistryPromise" before initialization' and
    'Cannot access "costsState" before initialization' after the
    scripts/{core,tabs,surfaces}/ restructure).

    Skips when Node isn't on PATH; the test harness ``eval_dashboard_init.cjs``
    sets up a minimal browser stub (document, window, EventSource, fetch,
    setInterval) so the script's init phase can run end-to-end.
    """
    import shutil
    import subprocess
    import tempfile

    import pytest

    if shutil.which("node") is None:
        pytest.skip("node not on PATH")

    # Render the page to a temp file the harness can read.
    html = render_page()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", encoding="utf-8", delete=False
    ) as fh:
        fh.write(html)
        html_path = fh.name

    harness = (
        Path(__file__).parent / "eval_dashboard_init.cjs"
    ).resolve()
    try:
        result = subprocess.run(
            ["node", str(harness), html_path],
            capture_output=True,
            text=True,
        )
    finally:
        import os
        os.unlink(html_path)

    assert result.returncode == 0, (
        f"Dashboard JS threw during init:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_assembled_javascript_parses():
    """The full ``render_page()`` JS must be syntactically valid.

    Each script module's content lives in a Python r-string, but the
    page concatenates them all into one ``<script>`` block. Per-module
    string-content tests do NOT detect cross-module breakage like an
    orphan function body whose ``function`` declaration was lost
    during an extraction (the failure mode that produced
    'Uncaught SyntaxError: Illegal return statement' after the
    scripts/{core,tabs,surfaces}/ restructure).

    Skips when Node.js isn't available — in CI the runner provides it.
    """
    import re
    import shutil
    import subprocess
    import tempfile

    import pytest

    if shutil.which("node") is None:
        pytest.skip("node not on PATH")

    html = render_page()
    m = re.search(r"<script>(.*?)</script>\s*</body>", html, re.S)
    assert m, "render_page output is missing the body <script> block"
    js = m.group(1)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", encoding="utf-8", delete=False
    ) as fh:
        fh.write(js)
        path = fh.name
    try:
        result = subprocess.run(
            ["node", "--check", path], capture_output=True, text=True
        )
    finally:
        import os
        os.unlink(path)

    assert result.returncode == 0, (
        f"Assembled JS failed Node syntax check:\n{result.stderr}"
    )
