"""Smoke tests for the Settings › Inference sub-view wiring.

The JS is a Python string assembled into the single-page app. We verify the
sub-view renders, the sub-tab guard accepts ``'inference'`` (a silent
rewrite-to-status would otherwise hide the panel), and the broker.state SSE
handler routes through the surface mutator without a wholesale-loader call.
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
    # Newest-first ordering of the oldest-first ring snapshot.
    assert ".slice().reverse()" in src


def test_inference_legibility_affordances():
    """The UX addendum: verdict, humanized labels, teaching help, units."""
    src = _inference_script()
    # Health verdict (the "see it, don't guess" signal).
    assert "_infHealth" in src
    assert "Contended" in src and "Healthy" in src
    # Humanized profile labels (raw key → role).
    assert "_infRole" in src
    assert "Embedding offload" in src
    # Inline teaching help.
    assert "_infToggleHelp" in src
    assert "Priority" in src and "Latency splits" in src
    # Units + window explanation.
    assert "Queue wait (ms)" in src
    assert "the table below lists recent calls" in src
    # Recent calls scroll viewport (Event Log pattern) so many rows fit.
    assert "inf-table-scroll" in src


def test_broker_state_handler_clean():
    src = _event_bus_script()
    assert "eventBus.on('broker.state'" in src
    assert "_refreshSoon('inferenceSurface')" in src
    # The dispatcher must not introduce any wholesale-loader call (mirrors the
    # invariant in test_dashboard_event_bus_frontend.py).
    for forbidden in ("loadSettings(", "loadInference(", "staticLoaders[", "_smartRefresh"):
        assert forbidden not in src
