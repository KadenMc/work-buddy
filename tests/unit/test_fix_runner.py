"""Tests for the requirement-fix dispatcher (Fix-A)."""

from __future__ import annotations

from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_graph_cache():
    from work_buddy.control.graph import invalidate_graph
    invalidate_graph()
    yield
    invalidate_graph()


# ---------------------------------------------------------------------------
# Dispatcher: schema validation + dispatch
# ---------------------------------------------------------------------------

def test_run_fix_unknown_requirement_returns_ok_false():
    from work_buddy.control.fix_runner import run_fix
    result = run_fix("does/not/exist")
    assert result["ok"] is False
    assert "Unknown requirement" in result["detail"]


def test_run_fix_no_fix_returns_ok_false():
    """A requirement with fix_kind='none' refuses politely."""
    from work_buddy.control.fix_runner import run_fix
    # core/config/repos-root has no fixer registered yet
    result = run_fix("core/config/repos-root")
    assert result["ok"] is False
    assert "no automated fix" in result["detail"].lower()


def test_run_fix_input_required_missing_params():
    """input_required requirements reject calls with missing fields."""
    from work_buddy.control.fix_runner import run_fix
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    # Synthesize an input_required req for the duration of this test
    fake = mock.Mock()
    fake.fix_kind = "input_required"
    fake.fix_fn = "tests.unit.test_fix_runner._noop_fixer"
    fake.fix_params = {"path": {"required": True}}
    with mock.patch.dict(REQUIREMENT_REGISTRY, {"fake/req": fake}, clear=False):
        result = run_fix("fake/req", params={})
    assert result["ok"] is False
    assert "Missing required input" in result["detail"]


def _noop_fixer(**kwargs):
    return {"ok": True, "detail": "ok", "side_effects": []}


# ---------------------------------------------------------------------------
# Smoke fix: data/ writable
# ---------------------------------------------------------------------------

def test_fix_data_writable_creates_missing_dir(tmp_path, monkeypatch):
    """fix_data_writable creates the data dir if missing and reports the path."""
    target = tmp_path / "data"
    assert not target.exists()
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda *a, **k: target)
    from work_buddy.health.fixers import fix_data_writable
    result = fix_data_writable()
    assert result["ok"] is True
    assert target.exists() and target.is_dir()
    assert any("Created" in s for s in result["side_effects"])


def test_fix_data_writable_idempotent(tmp_path, monkeypatch):
    """Second invocation when dir already exists is a no-op success."""
    target = tmp_path / "data"
    target.mkdir()
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda *a, **k: target)
    from work_buddy.health.fixers import fix_data_writable
    result = fix_data_writable()
    assert result["ok"] is True
    assert result["side_effects"] == []  # nothing new to do


# ---------------------------------------------------------------------------
# End-to-end via run_fix: smoke fix actually applies
# ---------------------------------------------------------------------------

def test_run_fix_smoke_end_to_end(tmp_path, monkeypatch):
    """run_fix('core/data/writable') dispatches to the smoke fixer and
    re-runs the check, returning the fresh recheck result."""
    target = tmp_path / "data"
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda *a, **k: target)
    from work_buddy.control.fix_runner import run_fix
    result = run_fix("core/data/writable")
    assert result["ok"] is True
    assert result["recheck"] is not None
    assert result["recheck"]["ok"] is True
    assert target.exists()


# ---------------------------------------------------------------------------
# Help-brief builder (Fix-A: replaces Status-tab diagnose hint)
# ---------------------------------------------------------------------------

def test_help_brief_for_unknown_node():
    from work_buddy.control.help_briefs import build_help_brief
    brief = build_help_brief("not:a:real:node")
    assert "not currently in the control graph" in brief


def test_help_brief_for_requirement_includes_metadata():
    from work_buddy.control.help_briefs import build_help_brief
    # Pick a real registered requirement
    brief = build_help_brief("req:core/data/writable")
    assert "core/data/writable" in brief
    assert "data/" in brief or "data directory" in brief.lower()
    # The Fix-A smoke fix is programmatic — brief should mention it
    assert "programmatic" in brief.lower() or "apply this with a single click" in brief.lower()


def test_help_brief_for_component_includes_diagnostic_section():
    from work_buddy.control.help_briefs import build_help_brief
    brief = build_help_brief("component:obsidian")
    assert "obsidian" in brief.lower()
    assert "diagnostic" in brief.lower()


# ---------------------------------------------------------------------------
# Endpoint integration via Flask test client
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from work_buddy.dashboard.service import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_fix_endpoint_404_unknown_returns_ok_false_payload(client):
    """The endpoint never 404s on unknown req_ids — the dispatcher
    returns a structured ok:false payload so the UI can show a toast
    instead of an opaque HTTP error."""
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=False):
        resp = client.post("/api/control/fix/totally/fake/id", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False


def test_fix_endpoint_blocked_in_read_only(client):
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=True):
        resp = client.post("/api/control/fix/core/data/writable", json={})
    assert resp.status_code == 403


def test_fix_endpoint_smoke_e2e(client, tmp_path, monkeypatch):
    """End-to-end via Flask: POST to the smoke fix and get back recheck data."""
    target = tmp_path / "data"
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda *a, **k: target)
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=False), \
         mock.patch("work_buddy.consent.grant_consent"):
        resp = client.post("/api/control/fix/core/data/writable", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert target.exists()


def test_help_endpoint_blocked_in_read_only(client):
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=True):
        resp = client.post("/api/control/help/component:obsidian")
    assert resp.status_code == 403


def test_help_endpoint_dispatches(client):
    """The endpoint calls into help_briefs.spawn_help_agent."""
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=False), \
         mock.patch(
             "work_buddy.control.help_briefs.spawn_help_agent",
             return_value={"ok": True, "detail": "ok", "session_id": "s", "pid": 1, "message": "ok"},
         ) as mock_spawn:
        resp = client.post("/api/control/help/component:obsidian")
    assert resp.status_code == 200
    assert mock_spawn.called
    data = resp.get_json()
    assert data["ok"] is True
