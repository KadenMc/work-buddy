"""Regression: sidecar port-cleanup must actually verify the port is free.

Background: on 2026-04-17 a sidecar restart silently failed to kill
an orphaned mcp_gateway process on Windows. The root cause was
``os.kill(pid, SIGTERM)``, which is unreliable cross-process on
Windows. The cleanup helper reported success, the new Popen died,
and the sidecar happily logged ``"Started mcp_gateway (pid=...)"``
even though the child was already dead. The orphan kept serving
pre-fix code against port 5126 for hours.

These tests pin the new contract:

1. ``kill_process_on_port`` now escalates to a platform-appropriate
   force-kill (``taskkill /F`` on Windows, SIGKILL on Unix) when
   SIGTERM doesn't free the port.
2. It polls until the port is confirmed free OR the wait window
   expires.
3. The return value truthfully reflects whether the port is free —
   so ``_start_child`` can refuse to start when cleanup failed.
"""

from __future__ import annotations

import signal
from unittest.mock import patch

import pytest

from work_buddy import compat


def test_kill_returns_true_when_no_pids_on_port(monkeypatch):
    """Happy path: nothing holds the port, cleanup is a no-op True."""
    monkeypatch.setattr(compat, "_find_pids_on_port", lambda p: set())
    assert compat.kill_process_on_port(5126) is True


def test_kill_sends_sigterm_then_verifies_empty(monkeypatch):
    """SIGTERM actually cleared the port → return True without
    needing the escalation path."""
    calls = {"found": [{1234}, set()]}  # first call has PID; second is empty
    killed = []

    def fake_find(p):
        return calls["found"].pop(0) if calls["found"] else set()

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr(compat, "_find_pids_on_port", fake_find)
    monkeypatch.setattr(compat.os, "kill", fake_kill)

    assert compat.kill_process_on_port(5126, wait_seconds=0.5) is True
    assert killed == [(1234, signal.SIGTERM)]


def test_kill_escalates_to_force_kill_when_sigterm_ignored(monkeypatch):
    """The real-world failure mode: SIGTERM does nothing on Windows.
    We must escalate and verify."""
    # PID sticks around until _force_kill_pid is called, then goes away.
    state = {"alive": {9999}}

    def fake_find(p):
        return set(state["alive"])

    def fake_kill(pid, sig):
        pass  # SIGTERM silently ignored (the Windows bug)

    force_killed = []

    def fake_force(pid):
        force_killed.append(pid)
        state["alive"].discard(pid)

    monkeypatch.setattr(compat, "_find_pids_on_port", fake_find)
    monkeypatch.setattr(compat.os, "kill", fake_kill)
    monkeypatch.setattr(compat, "_force_kill_pid", fake_force)

    result = compat.kill_process_on_port(5126, wait_seconds=1.0)
    assert result is True
    assert force_killed == [9999], "force-kill must run when SIGTERM is ignored"


def test_kill_returns_false_when_orphan_cannot_be_killed(monkeypatch):
    """Port still held after escalation → return False so the caller
    doesn't try to bind and produce a silently-dead child."""
    state = {"alive": {9999}}

    def fake_find(p):
        return set(state["alive"])

    def fake_kill(pid, sig):
        pass

    def fake_force(pid):
        pass  # pretend even force-kill failed (orphan is stubborn)

    monkeypatch.setattr(compat, "_find_pids_on_port", fake_find)
    monkeypatch.setattr(compat.os, "kill", fake_kill)
    monkeypatch.setattr(compat, "_force_kill_pid", fake_force)

    result = compat.kill_process_on_port(5126, wait_seconds=0.8)
    assert result is False, (
        "Must return False when the port is still held so the sidecar "
        "refuses to spawn a doomed child."
    )


def test_kill_exceptions_during_find_do_not_crash(monkeypatch):
    """Best-effort: an exception in the pid-scan shouldn't propagate."""
    def fake_find(p):
        raise RuntimeError("boom")

    monkeypatch.setattr(compat, "_find_pids_on_port", fake_find)
    # Shouldn't raise. True because no pids were seen.
    assert compat.kill_process_on_port(5126, wait_seconds=0.1) is True


# ---------------------------------------------------------------------------
# _force_kill_pid platform-specific behavior
# ---------------------------------------------------------------------------

def test_force_kill_windows_uses_taskkill_slash_F(monkeypatch):
    """Windows path must use taskkill /F /PID — os.kill is unreliable."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    recorded = []

    def fake_run(cmd, **kw):
        recorded.append(cmd)
        class _R: returncode = 0
        return _R()

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    compat._force_kill_pid(12345)

    assert len(recorded) == 1
    cmd = recorded[0]
    assert cmd[0] == "taskkill"
    assert "/F" in cmd
    assert "12345" in cmd


def test_force_kill_unix_uses_sigkill(monkeypatch):
    """Unix path should use SIGKILL (or SIGTERM fallback on Windows
    test runners where SIGKILL isn't defined — the IS_WINDOWS branch
    handles real Windows and never reaches this path)."""
    monkeypatch.setattr(compat, "IS_WINDOWS", False)
    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr(compat.os, "kill", fake_kill)
    compat._force_kill_pid(12345)

    expected_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    assert killed == [(12345, expected_sig)]


def test_force_kill_tolerates_dead_process(monkeypatch):
    """If the process is already gone, don't crash."""
    monkeypatch.setattr(compat, "IS_WINDOWS", False)

    def fake_kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(compat.os, "kill", fake_kill)
    # Should not raise
    compat._force_kill_pid(12345)


# ---------------------------------------------------------------------------
# Socket-based fast path — regression guard for the PowerShell-cost fix
# ---------------------------------------------------------------------------
# Background: on 2026-04-18 sidecar restart took ~27s because ``_start_child``
# called ``kill_process_on_port`` for every service, which shelled out to
# ``powershell.exe Get-NetTCPConnection`` — a 3–5s cold-start cost per call,
# paid even when the port is free. Fix: ``_find_pids_on_port`` now does a
# fast socket connect and returns an empty set without ever invoking the
# subprocess if nothing is listening. These tests lock in that contract so a
# future refactor can't silently bring the 5s-per-service penalty back.


def test_find_pids_fast_path_does_not_spawn_subprocess(monkeypatch):
    """Free port → never invoke the PID-enumeration subprocess.

    The socket pre-check must short-circuit before any subprocess runs.
    If this test fails, sidecar restart silently regresses to ~5s per
    service and Claude Code hits ConnectionRefused again during the gap.
    """
    # Pretend nothing is listening on the port.
    monkeypatch.setattr(compat, "_is_port_listening", lambda p, **kw: False)

    called = {"run": 0}

    def forbidden_run(*args, **kwargs):
        called["run"] += 1
        raise AssertionError(
            "subprocess.run must not be called on the free-port fast path"
        )

    monkeypatch.setattr(compat.subprocess, "run", forbidden_run)

    assert compat._find_pids_on_port(5126) == set()
    assert called["run"] == 0


def test_find_pids_falls_through_when_port_is_held(monkeypatch):
    """Port is listening → the expensive PID enumeration path must run.

    Semantics unchanged when the port actually has a holder: we still
    shell out to find the PID so we can kill it.
    """
    monkeypatch.setattr(compat, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(compat, "IS_WINDOWS", True)

    called = {"run": 0}

    def fake_run(cmd, **kw):
        called["run"] += 1
        class _R:
            stdout = "4242\n"
            returncode = 0
        return _R()

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    pids = compat._find_pids_on_port(5126)
    assert pids == {4242}
    assert called["run"] == 1, "held port must fall through to PID enumeration"


def test_is_port_listening_returns_false_for_closed_port():
    """Live sanity check: a random high port should not be listening.

    This test binds nothing; it only confirms the helper returns False
    quickly when no one is on the port. Keeps the fast-path honest.
    """
    # Pick a port far from sidecar ports; extremely unlikely to be in use.
    assert compat._is_port_listening(40127, timeout=0.2) is False
