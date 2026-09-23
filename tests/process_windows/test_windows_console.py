"""Runtime console behaviour on real Windows.

The unit tests check the launch policy on paper. These run throwaway probe
processes under ``pythonw.exe``, which has no console, exactly like the
sidecar under its logon task, and ask Windows what each process got. They
never start work-buddy itself and never register scheduled tasks.

A console window handle of 0 means the process has no visible console
window. That is what keeps background console programs invisible.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.process_windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows console behaviour"),
]

HERE = Path(__file__).resolve().parent
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
FORCE_RELAUNCH = "WORK_BUDDY_HOST_FORCE_RELAUNCH"


def _windows_build() -> int:
    return sys.getwindowsversion().build if sys.platform == "win32" else 0


def _probe_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("WORK_BUDDY_SESSION_ID", FORCE_RELAUNCH)}
    env.update(extra)
    return env


def _start_probe_host(result: Path, mode: str, *args: str, **env: str) -> subprocess.Popen:
    return subprocess.Popen(
        [str(PYTHONW), "-m", "probe_host", str(result), mode, *args],
        cwd=HERE,
        env=_probe_env(**env),
    )


def _wait_for(path: Path, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.1)
    raise AssertionError(f"probe wrote no result to {path}")


def _alive(pid: int) -> bool:
    from work_buddy.utils.process import is_process_alive

    return is_process_alive(pid)


def _gone_within(pids: list[int], seconds: float) -> list[int]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if _alive(pid)]
        if not alive:
            return []
        time.sleep(0.1)
    return [pid for pid in pids if _alive(pid)]


def _terminate(pid: int, exit_code: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x0001, False, pid)
    assert handle, f"cannot open process {pid}"
    try:
        assert kernel32.TerminateProcess(ctypes.c_void_p(handle), exit_code)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _cleanup(*processes: subprocess.Popen, pids: tuple[int, ...] = ()) -> None:
    for process in processes:
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
    for pid in pids:
        if _alive(pid):
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )


# -- tool runs from a host with no console -------------------------------------------


def test_a_tool_run_from_a_gui_host_has_no_window(tmp_path) -> None:
    result = tmp_path / "tool-run.json"
    host = _start_probe_host(result, "tool-run")
    try:
        record = _wait_for(result)
        host.wait(timeout=60)
    finally:
        _cleanup(host)

    assert record["host"]["console_cp"] == 0, "the probe host should have no console"
    assert record["child"]["console_cp"] != 0
    assert record["child"]["console_window"] == 0


def test_powershell_through_the_helper_is_non_interactive_and_windowless(tmp_path) -> None:
    result = tmp_path / "powershell.json"
    host = _start_probe_host(result, "powershell")
    try:
        record = _wait_for(result, timeout=180)
        host.wait(timeout=60)
    finally:
        _cleanup(host)

    assert "-NoProfile" in record["argv"] and "-NonInteractive" in record["argv"]
    assert record["returncode"] == 0
    assert record["child_window"] == 0


# -- establishing the host context ------------------------------------------------------


@pytest.mark.skipif(
    _windows_build() < 26100, reason="AllocConsoleWithOptions needs Windows 11 24H2 or later",
)
def test_the_host_allocates_a_windowless_console_in_process(tmp_path) -> None:
    result = tmp_path / "allocate.json"
    host = _start_probe_host(result, "host")
    try:
        record = _wait_for(result)
        host.wait(timeout=60)
    finally:
        _cleanup(host)

    assert record["context"]["mechanism"] == "allocate_api"
    assert record["context"]["console"] == "allocated"
    assert record["host"]["console_cp"] != 0
    # A console program started WITHOUT the no-window flag inherits the
    # host's console, and so has no window either.
    assert record["child"]["console_window"] == 0
    # A tool run shares that console too.
    assert record["tool_child"]["console_cp"] != 0
    assert record["tool_child"]["console_window"] == 0


def test_the_relaunch_fallback_gives_a_windowless_console(tmp_path) -> None:
    result = tmp_path / "relaunch.json"
    host = _start_probe_host(result, "host", **{FORCE_RELAUNCH: "1"})
    try:
        record = _wait_for(result)
        assert host.wait(timeout=60) == 0
    finally:
        _cleanup(host)

    assert record["context"]["mechanism"] == "relaunch"
    assert record["context"]["console"] == "hidden"
    assert record["host"]["console_cp"] != 0
    assert record["host"]["console_window"] == 0
    assert record["child"]["console_window"] == 0
    assert record["tool_child"]["console_window"] == 0


def test_ending_the_relaunch_parent_ends_the_host_and_its_children(tmp_path) -> None:
    """Ending the scheduled task ends the process it started: here, the
    waiting parent. Its kill-on-close job must take the relaunched host with
    it, and the host's own job must take the host's children."""
    result = tmp_path / "end-parent.json"
    host = _start_probe_host(result, "host-hold", "120", **{FORCE_RELAUNCH: "1"})
    pids: tuple[int, ...] = ()
    try:
        record = _wait_for(result)
        pids = (record["host"]["pid"], record["probe"]["pid"])
        assert record["context"]["mechanism"] == "relaunch"
        assert record["child_in_job"] is True

        _terminate(host.pid, 1)

        assert _gone_within(list(pids), 15) == []
    finally:
        _cleanup(host, pids=pids)


def test_the_relaunch_parent_exits_with_the_host_exit_code(tmp_path) -> None:
    """So the scheduled task records the daemon's result."""
    result = tmp_path / "exit-code.json"
    host = _start_probe_host(result, "host-hold", "120", **{FORCE_RELAUNCH: "1"})
    pids: tuple[int, ...] = ()
    try:
        record = _wait_for(result)
        pids = (record["host"]["pid"], record["probe"]["pid"])

        _terminate(record["host"]["pid"], 77)

        assert host.wait(timeout=30) == 77
        assert _gone_within([record["probe"]["pid"]], 15) == []
    finally:
        _cleanup(host, pids=pids)


# -- tree termination -----------------------------------------------------------------------


def test_a_timed_out_tool_run_leaves_no_console_program_behind(tmp_path) -> None:
    from work_buddy.process import ProcessTimeout, run_tool

    marker = tmp_path / "ping.pid"
    parent = (
        "import subprocess, sys, time\n"
        "ping = subprocess.Popen(['ping', '-n', '60', '127.0.0.1'], stdout=subprocess.DEVNULL)\n"
        f"open({str(marker)!r}, 'w').write(str(ping.pid))\n"
        "time.sleep(60)\n"
    )

    started = time.monotonic()
    with pytest.raises(ProcessTimeout):
        run_tool([sys.executable, "-c", parent], timeout=5)

    assert time.monotonic() - started < 30
    ping_pid = int(marker.read_text())
    try:
        assert _gone_within([ping_pid], 10) == []
    finally:
        _cleanup(pids=(ping_pid,))
