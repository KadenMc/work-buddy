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


def test_kill_returns_true_when_port_is_free(monkeypatch):
    """Happy path: port isn't even listening → cleanup is a no-op True
    without ever invoking the PID lookup."""
    monkeypatch.setattr(compat, "_is_port_listening", lambda p, **kw: False)

    def forbidden_find(p):
        raise AssertionError(
            "Free-port fast path must not invoke _find_pids_on_port"
        )
    monkeypatch.setattr(compat, "_find_pids_on_port", forbidden_find)
    assert compat.kill_process_on_port(5126) is True


def test_kill_sends_sigterm_then_verifies_empty(monkeypatch):
    """SIGTERM actually cleared the port → return True without
    needing the escalation path. Ground truth is _is_port_listening,
    not the PID enumerator."""
    # Port is held at first, then becomes free after SIGTERM.
    listen_state = {"held": True}
    monkeypatch.setattr(
        compat, "_is_port_listening",
        lambda p, **kw: listen_state["held"],
    )

    killed: list[tuple[int, int]] = []

    def fake_find(p):
        return {1234} if listen_state["held"] else set()

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        # SIGTERM worked: PID releases the port.
        listen_state["held"] = False

    monkeypatch.setattr(compat, "_find_pids_on_port", fake_find)
    monkeypatch.setattr(compat.os, "kill", fake_kill)

    assert compat.kill_process_on_port(5126, wait_seconds=0.5) is True
    assert killed == [(1234, signal.SIGTERM)]


def test_kill_escalates_to_force_kill_when_sigterm_ignored(monkeypatch):
    """The real-world failure mode: SIGTERM does nothing on Windows.
    We must escalate and verify via the port-listening ground truth."""
    listen_state = {"held": True}
    monkeypatch.setattr(
        compat, "_is_port_listening",
        lambda p, **kw: listen_state["held"],
    )

    def fake_find(p):
        return {9999} if listen_state["held"] else set()

    def fake_kill(pid, sig):
        pass  # SIGTERM silently ignored (the Windows bug)

    force_killed: list[int] = []

    def fake_force(pid):
        force_killed.append(pid)
        listen_state["held"] = False  # taskkill /F worked

    monkeypatch.setattr(compat, "_find_pids_on_port", fake_find)
    monkeypatch.setattr(compat.os, "kill", fake_kill)
    monkeypatch.setattr(compat, "_force_kill_pid", fake_force)

    result = compat.kill_process_on_port(5126, wait_seconds=1.0)
    assert result is True
    assert force_killed == [9999], "force-kill must run when SIGTERM is ignored"


def test_kill_returns_false_when_orphan_cannot_be_killed(monkeypatch):
    """Port still held after escalation → return False so the caller
    doesn't try to bind and produce a silently-dead child."""
    monkeypatch.setattr(compat, "_is_port_listening", lambda p, **kw: True)

    def fake_find(p):
        return {9999}

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


def test_kill_returns_false_when_pid_lookup_raises_on_held_port(monkeypatch):
    """REGRESSION: 2026-05-02 — PowerShell Get-NetTCPConnection timed out
    at the function's 5s subprocess timeout. The exception was caught
    silently, ``pids = set()`` was returned, and the function reported
    True ("port cleaned") even though the orphan PID 16684 was still
    bound to port 5126. The new sidecar's ``_start_child`` then spawned
    a child that died on bind, while the orphan kept serving requests
    against stale code.

    Contract: if the port is provably held but the PID enumerator
    raised, refuse rather than guess. The caller will know the cleanup
    failed and abort the spawn — far better than a silent zombie that
    answers /health checks with the wrong bytecode for 27 hours.
    """
    monkeypatch.setattr(compat, "_is_port_listening", lambda p, **kw: True)

    def slow_find_that_times_out(p):
        # Mimics subprocess.TimeoutExpired bubbling up from a slow
        # PowerShell cold start.
        import subprocess
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=5)

    monkeypatch.setattr(compat, "_find_pids_on_port", slow_find_that_times_out)
    # No mock for os.kill — the lookup raises before we get there.
    result = compat.kill_process_on_port(5126, wait_seconds=0.5)
    assert result is False, (
        "When PID lookup fails on a held port, must NOT claim port is "
        "free. The previous silent-True behavior caused PID 16684 to "
        "survive multiple 'restarts' on 2026-05-02, serving stale "
        "registry data with no diagnostic to surface the gap."
    )


def test_kill_returns_false_when_port_held_but_pids_empty(monkeypatch):
    """If the port is listening but PID lookup returns empty (e.g.
    IPv6-only listener missed by parser, or permission-restricted
    process), refuse to claim free. Same reasoning as the timeout
    case: silent True is worse than honest False."""
    monkeypatch.setattr(compat, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(compat, "_find_pids_on_port", lambda p: set())
    assert compat.kill_process_on_port(5126, wait_seconds=0.3) is False


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

    Two-stage path on Windows: netstat fast path first; if it doesn't
    produce a parseable result, fall back to PowerShell. Either way we
    must end up with the PID — silent empty-set is the bug we're
    guarding against (see 2026-05-02 regression in
    test_kill_returns_false_when_pid_lookup_raises_on_held_port).
    """
    monkeypatch.setattr(compat, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(compat, "IS_WINDOWS", True)

    called: list[list[str]] = []

    def fake_run(cmd, **kw):
        called.append(cmd)

        class _R:
            returncode = 0
            stderr = ""
            # netstat output the helper can parse:
            # "  TCP    0.0.0.0:5126           0.0.0.0:0              LISTENING       4242"
            if cmd and cmd[0] == "netstat":
                stdout = (
                    "Active Connections\n\n"
                    "  Proto  Local Address          Foreign Address        State           PID\n"
                    "  TCP    0.0.0.0:5126           0.0.0.0:0              LISTENING       4242\n"
                )
            else:
                # PowerShell fallback shape
                stdout = "4242\n"

        return _R()

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    pids = compat._find_pids_on_port(5126)
    assert pids == {4242}
    # Fast path should resolve in one call; we don't fall through to
    # PowerShell when netstat already returned the PID.
    assert len(called) == 1, (
        "netstat fast path should resolve held ports without invoking "
        "PowerShell (which can take 6–15s on cold start)"
    )
    assert called[0][0] == "netstat", (
        "netstat must be tried before PowerShell — PowerShell cold "
        "start was the direct cause of the silent-True bug on 2026-05-02"
    )


def test_find_pids_falls_back_to_powershell_when_netstat_fails(monkeypatch):
    """If netstat is missing or returns unparseable output, the helper
    must still find the PID via PowerShell — never silently empty."""
    monkeypatch.setattr(compat, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(compat, "IS_WINDOWS", True)

    called: list[list[str]] = []

    def fake_run(cmd, **kw):
        called.append(cmd)
        if cmd[0] == "netstat":
            raise FileNotFoundError("no netstat in this PATH")

        class _R:
            stdout = "9876\n"
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    pids = compat._find_pids_on_port(5126)
    assert pids == {9876}
    assert len(called) == 2
    assert called[0][0] == "netstat"
    assert called[1][0] == "powershell.exe"
    # CRITICAL: -NoProfile prevents 5–10s of profile loading on cold
    # PowerShell. Without it, the previous 5s subprocess timeout (now
    # 30s) would silently kick in and return empty pids.
    assert "-NoProfile" in called[1], (
        "PowerShell fallback must use -NoProfile — profile loading was "
        "a contributor to the >5s timeouts that masked the orphan PID"
    )


def test_is_port_listening_returns_false_for_closed_port():
    """Live sanity check: a random high port should not be listening.

    This test binds nothing; it only confirms the helper returns False
    quickly when no one is on the port. Keeps the fast-path honest.
    """
    # Pick a port far from sidecar ports; extremely unlikely to be in use.
    assert compat._is_port_listening(40127, timeout=0.2) is False
