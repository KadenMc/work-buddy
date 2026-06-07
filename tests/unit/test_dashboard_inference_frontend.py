"""Smoke tests for the Settings › Inference sub-view wiring.

The JS is a Python string assembled into the single-page app. We verify the
sub-view renders, the sub-tab guard accepts ``'inference'`` (a silent
rewrite-to-status would otherwise hide the panel), and the inference.call_logged
SSE handler routes through the surface mutator without a wholesale-loader call.
"""
from __future__ import annotations

from work_buddy.dashboard.frontend import render_page
from work_buddy.dashboard.frontend.scripts.core.event_bus import script as _event_bus_script
from work_buddy.dashboard.frontend.scripts.tabs.inference import script as _inference_script
from work_buddy.dashboard.frontend.scripts.tabs.settings import script as _settings_script


def test_inference_subview_in_page():
    page = render_page()
    assert 'id="ssp-inference"' in page
    assert 'id="inference-content"' in page
    assert "switchSettingsSubtab('inference')" in page


def test_settings_guard_accepts_inference():
    # Regression against the line-42 guard that silently rewrites unknown
    # sub-tabs to 'status' — if 'inference' isn't whitelisted, the panel never shows.
    src = _settings_script()
    assert "st !== 'inference'" in src
    assert "loadInference()" in src


def test_inference_surface_contract():
    src = _inference_script()
    assert "window.inferenceSurface" in src
    assert "function loadInference" in src
    assert "isMounted" in src


def test_inference_panel_affordances():
    """Trimmed panel: provenance feed + teaching help; broker-scheduler heritage removed."""
    src = _inference_script()
    # Teaching help (kept, retrimmed).
    assert "_infToggleHelp" in src
    assert "What this is" in src and "Escalation" in src
    # Provenance feed columns / cells.
    assert "Purpose" in src
    assert "Latency (ms)" in src
    assert "_infUsage" in src
    assert "inf-table-scroll" in src
    # Trimmed panel: no occupancy cards (those belong to the fleet view), no latency percentiles.
    assert "_infHealth" not in src
    assert "_infRenderCards" not in src
    assert "Occupancy" not in src
    assert "p95" not in src


def test_inference_activity_feed_renders():
    src = _inference_script()
    assert "_infRenderActivity" in src
    assert "Inference activity" in src      # panel/feed title
    assert "/api/inference-activity" in src
    assert "inf-mode-local" in src          # local/cloud badge


def test_recent_calls_table_is_merged():
    """The broker recent-calls table is folded into the unified activity table."""
    src = _inference_script()
    # Old broker table + its dropped columns are gone.
    assert "_infRenderRecent" not in src
    assert "Total (ms)" not in src
    assert "Tokens / items" not in src
    # Multi-select filters (Kind is a filter, not a column) via the shared
    # wbRenderFilters widget; redesigned usage cell; escalation chains marked.
    assert "wbRenderFilters('inf-filters'" in src
    assert "key: 'where'" in src and "key: 'kind'" in src and "key: 'status'" in src
    assert "_infGetSelected" in src and "_infOnChange" in src
    assert "_infUsage" in src
    assert "inf-chain" in src


def test_activity_sse_handler_registered():
    src = _event_bus_script()
    assert "eventBus.on('inference.call_logged'" in src
    assert "_refreshSoon('inferenceSurface')" in src
