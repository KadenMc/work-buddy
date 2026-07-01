"""Unit tests for the ``wbuddy`` CLI (``work_buddy.cli``).

Covers argparse routing, ``wbuddy mcp print`` output, ``wbuddy status`` rendering
against a synthetic ``SidecarState``, and the lifecycle helpers with the
sidecar plumbing mocked (no real process spawn or kill).
"""

from __future__ import annotations

import json
import time
from unittest.mock import Mock

import pytest

from work_buddy.cli import commands, dispatch, lifecycle
from work_buddy.sidecar.state import ServiceHealth, SidecarState


# ---------------------------------------------------------------------------
# wbuddy mcp print
# ---------------------------------------------------------------------------

def test_mcp_print_emits_http_config(capsys, monkeypatch):
    monkeypatch.setattr("work_buddy.mcp_server.server._get_port", lambda: 5126)
    rc = dispatch.main(["mcp", "print"])
    assert rc == 0
    server = json.loads(capsys.readouterr().out)["mcpServers"]["work-buddy"]
    assert server["type"] == "http"
    assert server["url"] == "http://localhost:5126/mcp"


def test_mcp_print_follows_configured_port(capsys, monkeypatch):
    monkeypatch.setattr("work_buddy.mcp_server.server._get_port", lambda: 9999)
    dispatch.main(["mcp", "print"])
    url = json.loads(capsys.readouterr().out)["mcpServers"]["work-buddy"]["url"]
    assert url == "http://localhost:9999/mcp"


def test_mcp_print_routes_to_handler(monkeypatch):
    monkeypatch.setattr(commands, "cmd_mcp_print", lambda args: 7)
    assert dispatch.main(["mcp", "print"]) == 7


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "verb",
    ["start", "stop", "restart", "status", "doctor", "setup", "dashboard"],
)
def test_routes_verb_to_handler(monkeypatch, verb):
    monkeypatch.setitem(dispatch._HANDLERS, verb, lambda args: 42)
    assert dispatch.main([verb]) == 42


def test_no_command_is_usage_error():
    assert dispatch.main([]) == 2


def test_unknown_command_is_usage_error():
    assert dispatch.main(["bogus"]) == 2


# ---------------------------------------------------------------------------
# wbuddy status rendering
# ---------------------------------------------------------------------------

def _state_with_services() -> SidecarState:
    st = SidecarState(
        started_at=time.time() - 3600,
        pid=123,
        last_tick_at=time.time() - 10,
    )
    st.services = {
        "mcp_gateway": ServiceHealth(name="mcp_gateway", port=5126, status="healthy"),
        "embedding": ServiceHealth(
            name="embedding", port=5124, status="unhealthy", crash_count=2
        ),
    }
    return st


def test_status_running_render(capsys, monkeypatch):
    st = _state_with_services()
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": True, "health": "up", "pid": 123, "state": st},
    )
    rc = dispatch.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Sidecar running (pid=123" in out
    assert "mcp_gateway :5126: healthy" in out
    assert "embedding :5124: unhealthy, 2 crash(es)" in out


def test_status_not_running(capsys, monkeypatch):
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": False, "health": "down", "pid": None, "state": None},
    )
    rc = dispatch.main(["status"])
    assert rc == 1
    assert "Sidecar not running." in capsys.readouterr().out


def test_status_json(capsys, monkeypatch):
    st = _state_with_services()
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": True, "health": "up", "pid": 123, "state": st},
    )
    rc = dispatch.main(["status", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["running"] is True and out["pid"] == 123 and out["health"] == "up"
    assert out["state"]["services"]["mcp_gateway"]["status"] == "healthy"


def test_status_booting_after_start(capsys, monkeypatch):
    # Live pid (pid file) differs from the state file's pid: a just-started
    # daemon that has not published its state yet. Status must not show the
    # previous daemon's stale uptime.
    st = _state_with_services()  # st.pid == 123
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": True, "health": "booting", "pid": 999, "state": st},
    )
    rc = dispatch.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pid=999" in out
    assert "starting up" in out
    assert "uptime=" not in out


def test_status_wedged_reports_and_fails(capsys, monkeypatch):
    # Alive pid but no fresh state: a hung daemon. Status must say so (not
    # "starting up" forever) and exit non-zero so scripts can detect it.
    st = _state_with_services()  # st.pid == 123, does not match the live pid
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": True, "health": "wedged", "pid": 999, "state": st},
    )
    rc = dispatch.main(["status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "pid=999" in out
    assert "wedged" in out
    assert "uptime=" not in out


# ---------------------------------------------------------------------------
# Lifecycle helpers (sidecar plumbing mocked)
# ---------------------------------------------------------------------------

def test_start_is_idempotent_when_healthy(monkeypatch):
    # A daemon that is alive AND publishing fresh state must not be restarted.
    st = SidecarState(pid=999, started_at=time.time(), last_tick_at=time.time())
    monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: 999)
    monkeypatch.setattr(lifecycle._state, "load_state", lambda: st)
    popen = Mock()
    monkeypatch.setattr(lifecycle.subprocess, "Popen", popen)
    res = lifecycle.start_sidecar()
    assert res["already_running"] is True and res["pid"] == 999
    popen.assert_not_called()


def test_start_takes_over_wedged_daemon(monkeypatch):
    # A pid file naming an alive-but-wedged daemon must not block start: spawn a
    # fresh daemon (which takes the wedged one over) instead of refusing. This
    # is the bug the pid-liveness-only check had: it reported "already running"
    # and did nothing.
    monkeypatch.setattr(lifecycle, "_daemon_health", lambda *a: "wedged")
    seq = iter([20880, 26756])  # existing-check, then the confirm poll
    monkeypatch.setattr(
        lifecycle._pid, "check_existing_daemon", lambda: next(seq, 26756)
    )
    monkeypatch.setattr(lifecycle._state, "load_state", lambda: None)
    popen = Mock()
    monkeypatch.setattr(lifecycle.subprocess, "Popen", popen)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _s: None)
    res = lifecycle.start_sidecar(wait_seconds=2.0)
    assert res["started"] is True and res["already_running"] is False
    assert res["pid"] == 26756
    popen.assert_called_once()


def test_start_ignores_dying_zombie_pid(monkeypatch):
    # Mid-takeover the old pid can still read as alive for a moment. The confirm
    # loop must skip it and latch only onto the new daemon's pid.
    monkeypatch.setattr(lifecycle, "_daemon_health", lambda *a: "wedged")
    seq = iter([20880, 20880, 26756])  # existing, still-dying, then the new pid
    monkeypatch.setattr(
        lifecycle._pid, "check_existing_daemon", lambda: next(seq, 26756)
    )
    monkeypatch.setattr(lifecycle._state, "load_state", lambda: None)
    monkeypatch.setattr(lifecycle.subprocess, "Popen", Mock())
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _s: None)
    res = lifecycle.start_sidecar(wait_seconds=2.0)
    assert res["pid"] == 26756 and res["started"] is True


def test_start_spawns_when_absent(monkeypatch):
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "agent-123")
    seq = iter([None, 555])  # initial existing-check, then the loop poll
    monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: next(seq, 555))
    # State file deliberately empty: start must confirm on the pid file alone,
    # because the daemon rewrites the state file only on its first tick.
    monkeypatch.setattr(lifecycle._state, "load_state", lambda: None)
    popen = Mock()
    monkeypatch.setattr(lifecycle.subprocess, "Popen", popen)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _s: None)
    res = lifecycle.start_sidecar(wait_seconds=2.0)
    assert res["started"] is True and res["pid"] == 555
    popen.assert_called_once()
    # The daemon must self-assign its own sidecar consent principal, so wb must
    # not leak its WORK_BUDDY_SESSION_ID into the spawned daemon's environment.
    child_env = popen.call_args.kwargs["env"]
    assert "WORK_BUDDY_SESSION_ID" not in child_env


def test_stop_when_not_running(monkeypatch):
    monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: None)
    res = lifecycle.stop_sidecar()
    assert res["was_running"] is False and res["stopped"] is False


def test_stop_calls_takeover(monkeypatch):
    monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: 555)
    seen = {}
    monkeypatch.setattr(
        lifecycle._pid, "takeover_existing_daemon",
        lambda pid: seen.setdefault("pid", pid) is None or True,
    )
    res = lifecycle.stop_sidecar()
    assert res["stopped"] is True and seen["pid"] == 555


# ---------------------------------------------------------------------------
# _daemon_health classifier (the fix for pid-liveness-only "already running")
# ---------------------------------------------------------------------------

def test_daemon_health_down_when_no_pid():
    assert lifecycle._daemon_health(None, None) == "down"


def test_daemon_health_up_when_publishing_fresh():
    st = SidecarState(pid=42, started_at=time.time(), last_tick_at=time.time())
    assert lifecycle._daemon_health(42, st) == "up"


def test_daemon_health_booting_when_pid_file_fresh(monkeypatch):
    # Alive, not yet publishing matching state, but the pid file was just
    # written: presumed booting, not wedged.
    monkeypatch.setattr(lifecycle, "_pid_file_age_s", lambda: 5.0)
    assert lifecycle._daemon_health(42, None) == "booting"


def test_daemon_health_wedged_when_pid_file_old(monkeypatch):
    # Alive, not publishing, and the pid file is old: it has stopped ticking.
    monkeypatch.setattr(lifecycle, "_pid_file_age_s", lambda: 9999.0)
    assert lifecycle._daemon_health(42, None) == "wedged"


def test_daemon_health_wedged_when_tick_stale(monkeypatch):
    # State names this pid but last_tick is far in the past: the daemon
    # published once, then hung. With an old pid file that reads as wedged.
    monkeypatch.setattr(lifecycle, "_pid_file_age_s", lambda: 9999.0)
    st = SidecarState(
        pid=42, started_at=time.time() - 500, last_tick_at=time.time() - 500
    )
    assert lifecycle._daemon_health(42, st) == "wedged"


# ---------------------------------------------------------------------------
# wbuddy setup
# ---------------------------------------------------------------------------

def test_setup_renders_bootstrap_and_mcp(capsys, monkeypatch):
    from work_buddy.health.requirements import RequirementResult

    class FakeChecker:
        def check_bootstrap(self):
            return [
                RequirementResult(
                    id="core/config/config-yaml-exists", ok=True, detail="found",
                    fix_hint="", severity="required", component=None,
                ),
            ]

        def summarize(self, results):
            return {
                "total": 1, "passed": 1, "failed_required": 0,
                "failed_recommended": 0, "all_required_pass": True, "failures": [],
            }

    monkeypatch.setattr(
        "work_buddy.health.requirements.RequirementChecker", FakeChecker
    )
    monkeypatch.setattr("work_buddy.mcp_server.server._get_port", lambda: 5126)
    rc = dispatch.main(["setup"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1/1 passed" in out
    assert "http://localhost:5126/mcp" in out
    assert "/wb-setup guided" in out
