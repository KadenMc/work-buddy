"""Regression: sidecar takeover must reap the old daemon's children.

Background — May 2026: a single dashboard child process (PID 29868)
ran continuously for 16 days through several sidecar restarts. The
daemon log file ``dashboard.1.log`` showed it had started 2026-04-19
and was still binding port 5127 on 2026-05-05, serving stale in-memory
bytecode that pre-dated weeks of merged commits.

Root cause chain:

1. ``takeover_existing_daemon`` calls ``os.kill(pid, signal.SIGTERM)``.
   On Windows this is ``TerminateProcess`` — a hard kill that does NOT
   trigger the daemon's signal handler, so its ``_shutdown`` cleanup
   (which would ``terminate()`` each child) never runs.
2. The orphaned children survive on their bound ports.
3. The new daemon's ``_start_child`` calls ``_kill_process_on_port`` to
   clean up — but if that fails (PID lookup timeout, force-kill blocked),
   the new daemon refuses to spawn and the orphan keeps serving
   ``/health`` against stale bytecode forever.

Fix: kill the daemon's direct children FIRST, then the daemon itself.
This way orphans are never created, regardless of whether the
daemon's signal handler ever fires.

These tests pin that contract.
"""

from __future__ import annotations

import signal
from unittest.mock import patch

import pytest

from work_buddy import compat
from work_buddy.sidecar import pid as sidecar_pid


# ---------------------------------------------------------------------------
# find_child_pids — child enumeration
# ---------------------------------------------------------------------------


def test_find_child_pids_windows_uses_wmic_first(monkeypatch):
    """WMIC fast path must run before PowerShell fallback.

    PowerShell cold start is 6-15s; if WMIC works we should never pay
    that cost. The mock returns parseable WMIC output and the fallback
    must not be invoked.
    """
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    called: list[list[str]] = []

    def fake_run(cmd, **kw):
        called.append(cmd)

        class _R:
            returncode = 0
            stderr = ""
            # WMIC output: header line + one PID per line
            stdout = "ProcessId\n29868\n7261\n"
        return _R()

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    children = compat.find_child_pids(7260)
    assert children == {29868, 7261}
    # Must resolve in the WMIC call only — no PowerShell fallback.
    assert len(called) == 1
    assert called[0][0] == "wmic"


def test_find_child_pids_windows_falls_back_to_powershell(monkeypatch):
    """When WMIC is missing (modern Win11 deprecation), the helper must
    fall through to PowerShell rather than silently return empty."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    called: list[list[str]] = []

    def fake_run(cmd, **kw):
        called.append(cmd)
        if cmd[0] == "wmic":
            raise FileNotFoundError("wmic not installed")

        class _R:
            returncode = 0
            stderr = ""
            stdout = "12345\n"
        return _R()

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    children = compat.find_child_pids(9999)
    assert children == {12345}
    assert len(called) == 2
    assert called[0][0] == "wmic"
    assert called[1][0] == "powershell.exe"
    # Cold-PowerShell mitigation: -NoProfile must be set or this can take
    # 6-15s and time out.
    assert "-NoProfile" in called[1]


def test_find_child_pids_returns_empty_when_no_children(monkeypatch):
    """A daemon with no children → empty set, not an error."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)

    def fake_run(cmd, **kw):
        class _R:
            returncode = 0
            stderr = ""
            stdout = "ProcessId\n"  # WMIC header only, no PIDs
        return _R()

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    assert compat.find_child_pids(1) == set()


def test_find_child_pids_unix_uses_pgrep(monkeypatch):
    """Unix path is straightforward: ``pgrep -P <pid>`` lists children."""
    monkeypatch.setattr(compat, "IS_WINDOWS", False)
    called: list[list[str]] = []

    def fake_run(cmd, **kw):
        called.append(cmd)

        class _R:
            returncode = 0
            stderr = ""
            stdout = "100\n200\n300\n"
        return _R()

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    assert compat.find_child_pids(50) == {100, 200, 300}
    assert called[0] == ["pgrep", "-P", "50"]


# ---------------------------------------------------------------------------
# takeover_existing_daemon — orphan-prevention contract
# ---------------------------------------------------------------------------


def test_takeover_kills_children_before_daemon(monkeypatch):
    """The order matters. If we killed the daemon first on Windows,
    ``TerminateProcess`` would hard-kill it, the daemon's signal
    handler would never fire, and the children would orphan. By
    killing children first, even a hard-killed daemon leaves no
    survivors.
    """
    call_order: list[tuple[str, int]] = []

    def fake_find(pid):
        return {29868, 7261}  # two children

    def fake_force_kill(pid):
        call_order.append(("force_kill", pid))

    def fake_os_kill(pid, sig):
        call_order.append(("os_kill", pid))

    # Simulate the daemon dying on the first signal so the polling
    # loop exits cleanly. The first call to _is_process_alive after
    # the kill returns False.
    alive_state = {"alive": True}

    def fake_is_alive(pid):
        # Once we've sent any signal, pretend the daemon dies.
        if any(c[0] in ("os_kill", "force_kill") and c[1] == pid for c in call_order):
            return False
        return alive_state["alive"]

    monkeypatch.setattr(sidecar_pid.os, "kill", fake_os_kill)
    monkeypatch.setattr(sidecar_pid, "_is_process_alive", fake_is_alive)
    monkeypatch.setattr(sidecar_pid, "_remove_pid_file", lambda: None)
    # Patch the late-bound imports inside takeover_existing_daemon:
    monkeypatch.setattr(compat, "find_child_pids", fake_find)
    monkeypatch.setattr(compat, "_force_kill_pid", fake_force_kill)

    assert sidecar_pid.takeover_existing_daemon(7260, wait_seconds=0.5) is True

    # Children were force-killed before any os.kill on the daemon.
    force_kill_calls = [c for c in call_order if c[0] == "force_kill"]
    daemon_kill_idx = next(
        i for i, c in enumerate(call_order) if c[0] == "os_kill" and c[1] == 7260
    )
    for fkc in force_kill_calls:
        assert call_order.index(fkc) < daemon_kill_idx, (
            "Children must be force-killed BEFORE the daemon — otherwise "
            "a hard-killed daemon orphans them and the new daemon's "
            "port-cleanup is the only remaining defense."
        )
    # Both children were targeted.
    assert {pid for action, pid in force_kill_calls} == {29868, 7261}


def test_takeover_with_no_children_still_kills_daemon(monkeypatch):
    """A daemon with no children → just kill the daemon. No-op on the
    children path; no spurious force-kills."""
    monkeypatch.setattr(compat, "find_child_pids", lambda pid: set())
    fk_calls: list[int] = []
    monkeypatch.setattr(compat, "_force_kill_pid", lambda pid: fk_calls.append(pid))
    monkeypatch.setattr(sidecar_pid.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(sidecar_pid, "_is_process_alive", lambda pid: False)
    monkeypatch.setattr(sidecar_pid, "_remove_pid_file", lambda: None)

    assert sidecar_pid.takeover_existing_daemon(7260, wait_seconds=0.3) is True
    # No children → no force-kill calls during the children-reap step.
    assert fk_calls == []


# ---------------------------------------------------------------------------
# Job Object — OS-enforced kill-time reaping (Windows hard-kill window)
# ---------------------------------------------------------------------------
#
# The takeover sweep above closes the cross-restart orphan window, but only
# on the *next* startup. The Job Object closes the gap in between: when the
# daemon is hard-killed (taskkill /F, crash) no signal handler runs, so on
# Windows children orphan until the next boot. KILL_ON_JOB_CLOSE makes the
# OS reap them the instant the daemon's process object is destroyed.


class _FakeWin32Job:
    """Minimal stand-in for the ``win32job`` module."""

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    def __init__(self):
        self.created = False
        self.set_flags = None

    def CreateJobObject(self, sa, name):
        self.created = True
        return "JOB_HANDLE"

    def QueryInformationJobObject(self, job, kind):
        return {"BasicLimitInformation": {"LimitFlags": 0}}

    def SetInformationJobObject(self, job, kind, info):
        self.set_flags = info["BasicLimitInformation"]["LimitFlags"]

    def AssignProcessToJobObject(self, job, handle):
        pass


def test_create_job_returns_none_on_non_windows(monkeypatch):
    """Windows-only: on Unix the helper no-ops to None (the cross-platform
    baseline is the startup orphan sweep, not a Job Object)."""
    monkeypatch.setattr(compat, "IS_WINDOWS", False)
    assert compat.create_kill_on_close_job() is None


def test_assign_returns_false_on_non_windows(monkeypatch):
    monkeypatch.setattr(compat, "IS_WINDOWS", False)
    assert compat.assign_process_to_job("JOB", 1234) is False


def test_assign_returns_false_when_job_is_none(monkeypatch):
    """A None job (creation failed) must make assignment a safe no-op."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    assert compat.assign_process_to_job(None, 1234) is False


def test_create_job_sets_kill_on_close_flag(monkeypatch):
    """The job must carry KILL_ON_JOB_CLOSE — that flag is the whole point."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    fake = _FakeWin32Job()
    monkeypatch.setitem(__import__("sys").modules, "win32job", fake)

    job = compat.create_kill_on_close_job()
    assert job == "JOB_HANDLE"
    assert fake.created
    assert fake.set_flags & fake.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_create_job_swallows_errors_returns_none(monkeypatch):
    """Job creation must never raise — a failure degrades to None and the
    startup sweep remains the fallback."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)

    class _Boom:
        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        def CreateJobObject(self, *a):
            raise OSError("access denied")

    monkeypatch.setitem(__import__("sys").modules, "win32job", _Boom())
    assert compat.create_kill_on_close_job() is None


def test_assign_is_best_effort_on_failure(monkeypatch):
    """Assignment can fail under nested-job restrictions — must return False,
    never raise, so a child still starts and the sweep covers it."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)

    class _BoomJob:
        def AssignProcessToJobObject(self, job, h):
            raise OSError("nested job limits")

    class _Api:
        def OpenProcess(self, *a):
            return "PROC_HANDLE"

        def CloseHandle(self, h):
            pass

    class _Con:
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "win32job", _BoomJob())
    monkeypatch.setitem(_sys.modules, "win32api", _Api())
    monkeypatch.setitem(_sys.modules, "win32con", _Con())

    assert compat.assign_process_to_job("JOB_HANDLE", 4321) is False


def test_assign_closes_process_handle_on_success(monkeypatch):
    """The *process* handle must be closed after assigning; only the *job*
    handle stays open (closing the job handle early would kill children)."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    closed: list[str] = []

    class _Job:
        def AssignProcessToJobObject(self, job, h):
            pass

    class _Api:
        def OpenProcess(self, *a):
            return "PROC_HANDLE"

        def CloseHandle(self, h):
            closed.append(h)

    class _Con:
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "win32job", _Job())
    monkeypatch.setitem(_sys.modules, "win32api", _Api())
    monkeypatch.setitem(_sys.modules, "win32con", _Con())

    assert compat.assign_process_to_job("JOB_HANDLE", 4321) is True
    assert closed == ["PROC_HANDLE"]
