"""Four-state bridge-diagnostic propagation.

Covers the previously-lost classification across bridge write-path failures:
  1. Obsidian not running (connection refused + process check)
  2. Bridge timing out (port open, HTTP timeout)
  3. Plugin not installed (manifest missing on disk)
  4. Plugin installed but disabled (manifest present, slug absent from community-plugins.json)
  Plus ``http_error`` (non-2xx from a reachable bridge).

Terminal states (1/3/4) short-circuit the @bridge_retry decorator so a
disabled plugin doesn't burn three 60-second sleeps.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import work_buddy.obsidian.bridge as bridge_mod
from work_buddy.obsidian.retry import (
    bridge_failure,
    bridge_retry,
    is_bridge_failure,
    is_terminal_bridge_failure,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bridge_state(monkeypatch):
    """Reset the module-level failure counters before each test."""
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "", raising=False)
    monkeypatch.setattr(bridge_mod, "_last_failure_status", None, raising=False)
    monkeypatch.setattr(bridge_mod, "_last_failure_reason", "", raising=False)


def _write_plugin_manifest(config_dir: Path) -> None:
    plugin_dir = config_dir / "plugins" / "obsidian-work-buddy"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.json").write_text('{"id":"work-buddy","version":"0.1.0"}', encoding="utf-8")


def _write_community_plugins(config_dir: Path, enabled: list[str]) -> None:
    (config_dir / "community-plugins.json").write_text(json.dumps(enabled), encoding="utf-8")


# ---------------------------------------------------------------------------
# get_last_bridge_state classification
# ---------------------------------------------------------------------------


def test_state_ok_when_no_recent_failure():
    info = bridge_mod.get_last_bridge_state()
    assert info["state"] == "ok"


def test_state_timeout_is_classified(monkeypatch):
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "timeout", raising=False)
    info = bridge_mod.get_last_bridge_state()
    assert info["state"] == "timeout"
    assert "lagging" in info["detail"] or "latency" in info["detail"] or "timed out" in info["detail"]
    assert info["status"] is None


def test_state_http_error_carries_status(monkeypatch):
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "http_error", raising=False)
    monkeypatch.setattr(bridge_mod, "_last_failure_status", 500, raising=False)
    info = bridge_mod.get_last_bridge_state()
    assert info["state"] == "http_error"
    assert info["status"] == 500


def test_state_obsidian_not_running(monkeypatch):
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "unreachable", raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: False)
    info = bridge_mod.get_last_bridge_state()
    assert info["state"] == "obsidian_not_running"


def test_state_plugin_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "unreachable", raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: True)

    # Vault with .obsidian/ but no plugin folder.
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)

    import work_buddy.health.requirement_checks as rq
    monkeypatch.setattr(rq, "_vault_root", lambda: vault)
    monkeypatch.setattr(rq, "_obsidian_config_dir", lambda _v: vault / ".obsidian")

    info = bridge_mod.get_last_bridge_state()
    assert info["state"] == "plugin_not_installed"
    assert "install" in info["detail"].lower()


def test_state_plugin_disabled(monkeypatch, tmp_path):
    """Manifest on disk, community-plugins.json missing the slug → disabled."""
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "unreachable", raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: True)

    vault = tmp_path / "vault"
    config_dir = vault / ".obsidian"
    _write_plugin_manifest(config_dir)
    _write_community_plugins(config_dir, enabled=["some-other-plugin"])

    import work_buddy.health.requirement_checks as rq
    monkeypatch.setattr(rq, "_vault_root", lambda: vault)
    monkeypatch.setattr(rq, "_obsidian_config_dir", lambda _v: config_dir)

    info = bridge_mod.get_last_bridge_state()
    assert info["state"] == "plugin_disabled"
    assert "enable" in info["detail"].lower() or "toggle" in info["detail"].lower()


def test_state_plugin_ok_but_port_refused(monkeypatch, tmp_path):
    """Rare race: plugin is enabled on disk but port still refuses."""
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "unreachable", raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: True)

    vault = tmp_path / "vault"
    config_dir = vault / ".obsidian"
    _write_plugin_manifest(config_dir)
    _write_community_plugins(config_dir, enabled=["work-buddy"])

    import work_buddy.health.requirement_checks as rq
    monkeypatch.setattr(rq, "_vault_root", lambda: vault)
    monkeypatch.setattr(rq, "_obsidian_config_dir", lambda _v: config_dir)

    info = bridge_mod.get_last_bridge_state()
    # Falls back to "obsidian_not_running" state with the race-disclaimer
    # detail — see get_last_bridge_state's trailing branch.
    assert info["state"] == "obsidian_not_running"
    assert "starting up" in info["detail"] or "failed to bind" in info["detail"]


# ---------------------------------------------------------------------------
# bridge_failure enrichment + terminal-state short-circuit
# ---------------------------------------------------------------------------


def test_bridge_failure_auto_enriches_from_module_state(monkeypatch):
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "timeout", raising=False)
    result = bridge_failure("write blew up")
    assert is_bridge_failure(result)
    assert result["_bridge_state"] == "timeout"
    assert result["_bridge_state_detail"]
    assert result["_bridge_terminal"] is False


def test_bridge_failure_marks_plugin_disabled_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "unreachable", raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: True)

    vault = tmp_path / "vault"
    config_dir = vault / ".obsidian"
    _write_plugin_manifest(config_dir)
    _write_community_plugins(config_dir, enabled=[])

    import work_buddy.health.requirement_checks as rq
    monkeypatch.setattr(rq, "_vault_root", lambda: vault)
    monkeypatch.setattr(rq, "_obsidian_config_dir", lambda _v: config_dir)

    result = bridge_failure("task_create couldn't write note")
    assert result["_bridge_state"] == "plugin_disabled"
    assert result["_bridge_terminal"] is True
    assert is_terminal_bridge_failure(result)


def test_bridge_failure_explicit_state_overrides_auto(monkeypatch):
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "timeout", raising=False)
    result = bridge_failure(
        "custom", state="http_error", state_detail="bridge returned 500"
    )
    assert result["_bridge_state"] == "http_error"
    assert result["_bridge_state_detail"] == "bridge returned 500"


def test_bridge_retry_short_circuits_on_terminal_state(monkeypatch, tmp_path):
    """A terminal failure must return immediately — no sleep loop."""
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "unreachable", raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: True)

    # Set up a "plugin disabled" filesystem state.
    vault = tmp_path / "vault"
    config_dir = vault / ".obsidian"
    _write_plugin_manifest(config_dir)
    _write_community_plugins(config_dir, enabled=[])

    import work_buddy.health.requirement_checks as rq
    monkeypatch.setattr(rq, "_vault_root", lambda: vault)
    monkeypatch.setattr(rq, "_obsidian_config_dir", lambda _v: config_dir)

    # If time.sleep is called, we'd notice: fail loudly.
    import work_buddy.obsidian.retry as retry_mod
    monkeypatch.setattr(
        retry_mod.time, "sleep",
        lambda _s: pytest.fail("retry must not sleep on terminal failure"),
    )
    monkeypatch.setattr(bridge_mod, "is_available", lambda: False)

    call_count = {"n": 0}

    @bridge_retry(max_retries=3, wait_seconds=60)
    def fn():
        call_count["n"] += 1
        return bridge_failure("plugin disabled cannot write")

    result = fn()

    assert call_count["n"] == 1  # one attempt, no retries
    assert result["_bridge_state"] == "plugin_disabled"
    assert result["_bridge_terminal"] is True


def test_bridge_retry_still_loops_on_transient_state(monkeypatch):
    """Non-terminal states (timeout) must still retry.

    ``@bridge_retry`` is a thin shim over the resilience framework — the
    retry loop lives in ``RetryStrategy``, which waits via ``asyncio.sleep``
    (jittered exponential backoff). The mechanism assert in this test
    targets ``strategies.asyncio.sleep`` rather than ``retry.time.sleep``:
    same intent ("there was a wait between attempts"), observed at the
    actual sleep site.
    """
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "timeout", raising=False)

    import work_buddy.resilience.strategies as strat_mod
    sleeps: list[float] = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(strat_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(bridge_mod, "is_available", lambda: True)

    call_count = {"n": 0}

    @bridge_retry(max_retries=3, wait_seconds=60)
    def fn():
        call_count["n"] += 1
        return bridge_failure("transient")

    result = fn()

    assert call_count["n"] == 3  # all three attempts used
    assert len(sleeps) == 2      # waits between attempts 1-2 and 2-3
    assert result["_bridge_state"] == "timeout"
    assert result["_bridge_terminal"] is False
