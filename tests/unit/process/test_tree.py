"""Process trees, port owners, and the kill-on-close Job Object.

Port cleanup must report truthfully whether a port is free. A cleanup that
claims success while an orphan still holds the port lets a new service die
on bind while an old one keeps serving stale code, and nothing looks wrong.
"""

from __future__ import annotations

import pathlib
import signal
import subprocess
import sys
import time

import pytest

from work_buddy.process import ProcessResult, tree


def _ended(pid: int) -> bool:
    """True once ``pid`` has exited. A killed process whose parent has not
    reaped it yet (a Linux zombie) has exited too."""
    from work_buddy.process import is_process_alive

    if not is_process_alive(pid):
        return True
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"


# ---------------------------------------------------------------------------
# kill_process_on_port
# ---------------------------------------------------------------------------


def test_free_port_is_reported_free_without_a_lookup(monkeypatch) -> None:
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: False)

    def forbidden(port):
        raise AssertionError("a free port must not start a PID lookup")

    monkeypatch.setattr(tree, "_find_pids_on_port", forbidden)

    assert tree.kill_process_on_port(5126) is True


def test_polite_signal_that_frees_the_port_is_enough(monkeypatch) -> None:
    held = {"port": True}
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: held["port"])
    monkeypatch.setattr(tree, "_find_pids_on_port", lambda p: {1234} if held["port"] else set())
    signals: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        signals.append((pid, sig))
        held["port"] = False

    monkeypatch.setattr(tree.os, "kill", fake_kill)

    assert tree.kill_process_on_port(5126, wait_seconds=0.5) is True
    assert signals == [(1234, signal.SIGTERM)]


def test_ignored_polite_signal_escalates_to_force(monkeypatch) -> None:
    held = {"port": True}
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: held["port"])
    monkeypatch.setattr(tree, "_find_pids_on_port", lambda p: {9999} if held["port"] else set())
    monkeypatch.setattr(tree.os, "kill", lambda pid, sig: None)
    forced: list[int] = []

    def fake_force(pid):
        forced.append(pid)
        held["port"] = False
        return True

    monkeypatch.setattr(tree, "terminate_pid", fake_force)

    assert tree.kill_process_on_port(5126, wait_seconds=1.0) is True
    assert forced == [9999]


def test_port_still_held_after_force_is_reported_held(monkeypatch) -> None:
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(tree, "_find_pids_on_port", lambda p: {9999})
    monkeypatch.setattr(tree.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(tree, "terminate_pid", lambda pid: False)

    assert tree.kill_process_on_port(5126, wait_seconds=0.8) is False


def test_failed_lookup_on_a_held_port_never_counts_as_free(monkeypatch) -> None:
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: True)

    def slow_lookup(port):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=5)

    monkeypatch.setattr(tree, "_find_pids_on_port", slow_lookup)

    assert tree.kill_process_on_port(5126, wait_seconds=0.5) is False


def test_held_port_with_no_known_owner_is_reported_held(monkeypatch) -> None:
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(tree, "_find_pids_on_port", lambda p: set())

    assert tree.kill_process_on_port(5126, wait_seconds=0.3) is False


def test_free_port_lookup_starts_no_process(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: False)

    assert tree._find_pids_on_port(5126) == set()
    assert recording_runner.launches == []


_NETSTAT = (
    "Active Connections\n\n"
    "  Proto  Local Address          Foreign Address        State           PID\n"
    "  TCP    0.0.0.0:5126           0.0.0.0:0              LISTENING       4242\n"
    "  TCP    127.0.0.1:5126         127.0.0.1:50000        ESTABLISHED     1111\n"
)


def test_held_port_lookup_uses_netstat_before_powershell(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["netstat"], stdout=_NETSTAT)

    assert tree._find_pids_on_port(5126) == {4242}
    assert recording_runner.argvs() == [["netstat", "-ano", "-p", "TCP"]]


def test_held_port_lookup_falls_back_to_non_interactive_powershell(
    monkeypatch, recording_runner,
) -> None:
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["netstat"], raises=FileNotFoundError("no netstat"))
    recording_runner.script(["powershell.exe"], stdout="9876\n")

    assert tree._find_pids_on_port(5126) == {9876}
    powershell = recording_runner.launches[1]
    assert powershell.argv[0] == "powershell.exe"
    assert "-NoProfile" in powershell.argv and "-NonInteractive" in powershell.argv
    assert powershell.timeout == 30


def test_posix_port_lookup_uses_lsof_then_ss(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "_is_port_listening", lambda p, **kw: True)
    monkeypatch.setattr(tree, "IS_WINDOWS", False)
    recording_runner.script(["lsof"], stdout="")
    recording_runner.script(["ss"], stdout='LISTEN 0 128 *:5126 users:(("python",pid=321,fd=3))')

    assert tree._find_pids_on_port(5126) == {321}
    assert [argv[0] for argv in recording_runner.argvs()] == ["lsof", "ss"]


def test_is_port_listening_is_false_for_a_closed_port() -> None:
    assert tree._is_port_listening(40127, timeout=0.2) is False


# ---------------------------------------------------------------------------
# terminate_pid and terminate_tree
# ---------------------------------------------------------------------------


def test_windows_termination_ends_the_tree_with_taskkill(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["taskkill"], returncode=0)

    assert tree.terminate_pid(12345) is True
    [launch] = recording_runner.launches
    assert list(launch.argv) == ["taskkill", "/F", "/T", "/PID", "12345"]


def test_windows_termination_reports_a_rejected_request(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["taskkill"], returncode=5)

    assert tree.terminate_pid(12345) is False


def test_windows_termination_reports_a_missing_taskkill(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["taskkill"], raises=FileNotFoundError("no taskkill"))

    assert tree.terminate_pid(12345) is False


@pytest.mark.parametrize(
    "remaining_s",
    [None, 0.5],
    ids=["deadline-passed", "deadline-nearly-spent"],
)
def test_windows_termination_is_not_bound_by_the_caller_deadline(
    monkeypatch, recording_runner, remaining_s,
) -> None:
    """Ending a process is cleanup, which usually runs after a timeout, once
    the caller's deadline has passed. The kill still starts, with its own
    limit rather than what is left of the deadline."""
    from work_buddy.resilience import Deadline, ResilienceContext, use_context

    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["taskkill"], returncode=0)
    deadline = Deadline(at=0.0) if remaining_s is None else Deadline.after(remaining_s)

    with use_context(ResilienceContext(operation_key="test", deadline=deadline)):
        assert tree.terminate_pid(12345) is True

    [launch] = recording_runner.launches
    assert list(launch.argv) == ["taskkill", "/F", "/T", "/PID", "12345"]
    assert launch.timeout == tree._TASKKILL_TIMEOUT_S


def test_posix_termination_sends_sigkill_to_a_plain_process(monkeypatch) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", False)
    monkeypatch.setattr(tree.os, "getpgid", lambda pid: 1, raising=False)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(tree.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert tree.terminate_pid(12345) is True
    assert signals == [(12345, getattr(signal, "SIGKILL", signal.SIGTERM))]


def test_posix_termination_ends_the_group_of_a_group_leader(monkeypatch) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", False)
    monkeypatch.setattr(tree.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(tree.os, "getpgrp", lambda: 1, raising=False)
    groups: list[int] = []
    monkeypatch.setattr(tree.os, "killpg", lambda pgid, sig: groups.append(pgid), raising=False)

    assert tree.terminate_pid(4321) is True
    assert groups == [4321]


def test_posix_termination_never_signals_its_own_group(monkeypatch) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", False)
    monkeypatch.setattr(tree.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(tree.os, "getpgrp", lambda: 4321, raising=False)
    monkeypatch.setattr(
        tree.os, "killpg", lambda *a: pytest.fail("signalled its own group"), raising=False,
    )
    signals: list[int] = []
    monkeypatch.setattr(tree.os, "kill", lambda pid, sig: signals.append(pid))

    assert tree.terminate_pid(4321) is True
    assert signals == [4321]


def test_posix_termination_of_a_gone_process_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", False)

    def gone(pid):
        raise ProcessLookupError

    monkeypatch.setattr(tree.os, "getpgid", gone, raising=False)

    assert tree.terminate_pid(12345) is True


def test_terminate_tree_leaves_an_exited_handle_alone(recording_runner) -> None:
    class Exited:
        pid = 777

        def poll(self):
            return 0

    assert tree.terminate_tree(Exited()) is True
    assert recording_runner.launches == []


def test_terminate_tree_ends_a_real_process_and_its_child(tmp_path) -> None:
    from work_buddy.process import SubprocessRunner, spawn_worker

    marker = tmp_path / "child.pid"
    parent = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    handle = spawn_worker(
        [sys.executable, "-c", parent], new_session=True, runner=SubprocessRunner(),
    )
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child_pid = int(marker.read_text())

    assert tree.terminate_tree(handle) is True
    handle.wait(timeout=10)
    deadline = time.monotonic() + 10
    while not _ended(child_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _ended(child_pid)


def test_terminate_tree_ends_a_real_process_after_the_caller_deadline_passed() -> None:
    from work_buddy.process import SubprocessRunner, spawn_worker
    from work_buddy.resilience import Deadline, ResilienceContext, use_context

    handle = spawn_worker(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        new_session=True, runner=SubprocessRunner(),
    )
    expired = ResilienceContext(operation_key="test", deadline=Deadline(at=0.0))

    try:
        with use_context(expired):
            assert tree.terminate_tree(handle) is True
        handle.wait(timeout=10)
    finally:
        if handle.poll() is None:
            handle.kill()


# ---------------------------------------------------------------------------
# find_child_pids
# ---------------------------------------------------------------------------


def test_windows_children_come_from_wmic_first(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["wmic"], stdout="ProcessId\n29868\n7261\n")

    assert tree.find_child_pids(7260) == {29868, 7261}
    assert [argv[0] for argv in recording_runner.argvs()] == ["wmic"]


def test_windows_children_fall_back_to_powershell_without_wmic(
    monkeypatch, recording_runner,
) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["wmic"], raises=FileNotFoundError("wmic not installed"))
    recording_runner.script(["powershell.exe"], stdout="12345\n")

    assert tree.find_child_pids(9999) == {12345}
    powershell = recording_runner.launches[1]
    assert "-NoProfile" in powershell.argv and "-NonInteractive" in powershell.argv


def test_a_process_without_children_has_none(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["wmic"], stdout="ProcessId\n")
    recording_runner.script(["powershell.exe"], stdout="")

    assert tree.find_child_pids(1) == set()


def test_posix_children_come_from_pgrep(monkeypatch, recording_runner) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", False)
    recording_runner.script(["pgrep"], stdout="100\n200\n300\n")

    assert tree.find_child_pids(50) == {100, 200, 300}
    assert recording_runner.argvs() == [["pgrep", "-P", "50"]]


def test_child_lookup_timeout_yields_an_empty_set(monkeypatch, recording_runner) -> None:
    from work_buddy.process import ProcessTimeout

    monkeypatch.setattr(tree, "IS_WINDOWS", False)
    recording_runner.script(["pgrep"], raises=ProcessTimeout(["pgrep"], 5))

    assert tree.find_child_pids(50) == set()


# ---------------------------------------------------------------------------
# The kill-on-close Job Object
# ---------------------------------------------------------------------------


def test_job_helpers_are_windows_only(monkeypatch) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", False)

    assert tree.create_kill_on_close_job() is None
    assert tree.assign_process_to_job(1, 1234) is False


def test_assigning_to_no_job_is_a_safe_no_op(monkeypatch) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)

    assert tree.assign_process_to_job(None, 1234) is False


class _FailingKernel32:
    """A kernel32 stand-in whose every Job Object call fails."""

    def CreateJobObjectW(self, *args):
        return None

    def OpenProcess(self, *args):
        return None

    def CloseHandle(self, handle):
        return True


def test_job_failures_degrade_to_none_and_false(monkeypatch) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    monkeypatch.setattr(tree, "_job_api", lambda: (_FailingKernel32(), object))
    monkeypatch.setattr("ctypes.get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr("ctypes.WinError", lambda code=None: OSError(code), raising=False)

    assert tree.create_kill_on_close_job() is None
    assert tree.assign_process_to_job(123, 4321) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_closing_the_job_ends_the_processes_in_it() -> None:
    """The flag that makes the job worth having: when the last handle to the
    job closes, as it does when its owner dies by any means, Windows ends
    every process in it."""
    import ctypes

    from work_buddy.process import SubprocessRunner, spawn_worker

    job = tree.create_kill_on_close_job()
    assert job is not None
    handle = spawn_worker(
        [sys.executable, "-c", "import time; time.sleep(60)"], runner=SubprocessRunner(),
    )
    try:
        assert tree.assign_process_to_job(job, handle.pid) is True
        assert handle.poll() is None
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(job))
        assert handle.wait(timeout=10) is not None
    finally:
        if handle.poll() is None:
            handle.kill()


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_silent_breakaway_job_is_created() -> None:
    assert tree.create_kill_on_close_job(silent_breakaway=True) is not None


def test_process_result_is_what_tool_runs_return(recording_runner, monkeypatch) -> None:
    monkeypatch.setattr(tree, "IS_WINDOWS", True)
    recording_runner.script(["taskkill"], respond=lambda launch: ProcessResult(
        list(launch.argv), 0, b"", b"",
    ))

    assert tree.terminate_pid(1) is True
    assert recording_runner.launches[0].decodes_output is False
