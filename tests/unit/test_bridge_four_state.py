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
from work_buddy.obsidian.errors import (
    ObsidianNotRunning,
    ObsidianPluginDisabled,
    ObsidianPluginMissing,
    ObsidianStartupRace,
)
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


# The four deterministic leaves of the "why is the bridge unreachable"
# decision tree, with the typed-exception and string-state representations
# that must stay in agreement. The startup-race leaf is the one historically
# mislabelled as the terminal ``obsidian_not_running``.
_UNREACHABLE_LEAVES = [
    ("not_running", ObsidianNotRunning, "obsidian_not_running"),
    ("not_installed", ObsidianPluginMissing, "plugin_not_installed"),
    ("disabled", ObsidianPluginDisabled, "plugin_disabled"),
    ("startup_race", ObsidianStartupRace, "obsidian_startup_race"),
]


def _setup_unreachable_leaf(scenario: str, monkeypatch, tmp_path) -> None:
    """Configure mocks so the unreachable classifier produces ``scenario``."""
    monkeypatch.setattr(bridge_mod, "_last_failure_kind", "unreachable", raising=False)
    if scenario == "not_running":
        monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: False)
        return

    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: True)
    vault = tmp_path / "vault"
    config_dir = vault / ".obsidian"
    if scenario == "not_installed":
        config_dir.mkdir(parents=True)
    elif scenario == "disabled":
        _write_plugin_manifest(config_dir)
        _write_community_plugins(config_dir, enabled=["some-other-plugin"])
    elif scenario == "startup_race":
        _write_plugin_manifest(config_dir)
        _write_community_plugins(config_dir, enabled=["work-buddy"])
    else:  # pragma: no cover - guard against typos in parametrization
        raise ValueError(f"unknown scenario: {scenario}")

    import work_buddy.health.requirement_checks as rq
    monkeypatch.setattr(rq, "_vault_root", lambda: vault)
    monkeypatch.setattr(rq, "_obsidian_config_dir", lambda _v: config_dir)


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
    """Startup race: plugin enabled on disk but the port hasn't bound yet.

    Classified as the non-terminal ``obsidian_startup_race`` — NOT the
    terminal ``obsidian_not_running`` — so the return-dict retry path
    treats it as transient, mirroring the typed ``ObsidianStartupRace``
    the exception path raises for the identical condition.
    """
    _setup_unreachable_leaf("startup_race", monkeypatch, tmp_path)

    info = bridge_mod.get_last_bridge_state()
    assert info["state"] == "obsidian_startup_race"
    assert "starting up" in info["detail"] or "failed to bind" in info["detail"]


@pytest.mark.parametrize(
    "scenario, expected_type, expected_state", _UNREACHABLE_LEAVES
)
def test_unreachable_classifiers_agree(
    scenario, expected_type, expected_state, monkeypatch, tmp_path
):
    """Anti-drift invariant: the typed-exception classifier
    (``_refine_unreachable_kind``) and the string-state classifier
    (``get_last_bridge_state``) both derive from ``_classify_unreachable``,
    so they must agree on every leaf. Guards against the two classifiers
    diverging — e.g. a startup race classified terminal in one and
    transient in the other.
    """
    _setup_unreachable_leaf(scenario, monkeypatch, tmp_path)
    assert bridge_mod._refine_unreachable_kind() is expected_type
    assert bridge_mod.get_last_bridge_state()["state"] == expected_state


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


def test_bridge_failure_startup_race_is_not_terminal(monkeypatch, tmp_path):
    """A startup race must NOT be flagged terminal.

    The return-dict retry path (``classify_bridge_result`` →
    ``is_terminal_bridge_failure``) keys off ``_bridge_terminal``; a false
    terminal here suppresses the in-process retries that let the bridge
    recover once the port binds.
    """
    _setup_unreachable_leaf("startup_race", monkeypatch, tmp_path)

    result = bridge_failure("task_create couldn't read master list")
    assert result["_bridge_state"] == "obsidian_startup_race"
    assert result["_bridge_terminal"] is False
    assert is_terminal_bridge_failure(result) is False


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
