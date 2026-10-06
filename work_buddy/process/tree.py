"""Process trees, port owners, and the kill-on-close Job Object.

Owners decide *when* a process should end. This module only knows *how*:

- :func:`terminate_tree` force-ends a process and everything it started.
- :func:`kill_process_on_port` frees a TCP port held by an orphan, and
  reports truthfully whether the port is free.
- :func:`find_child_pids` lists a process's direct children.
- :func:`create_kill_on_close_job` and :func:`assign_process_to_job` tie
  children's lifetimes to their parent on Windows.

Every lookup tool (``netstat``, PowerShell, ``wmic``, ``taskkill``, ``lsof``,
``ss``, ``pgrep``) runs as a tool run, so none of them opens a window.
Standard library only.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from typing import Any

from work_buddy.process.launch import run_tool, run_tool_spec
from work_buddy.process.policy import ToolRun, powershell_argv
from work_buddy.process.runner import ProcessHandle

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

_TASKKILL_TIMEOUT_S = 10.0


def terminate_pid(pid: int) -> bool:
    """Force-end ``pid`` and the processes it started.

    Windows: ``taskkill /F /T``, which works across processes where
    ``os.kill`` does not. Returns ``True`` only when ``taskkill`` succeeded,
    which means the request was accepted, not that the process has exited.
    The caller's resilience deadline does not bound it: ending a process is
    cleanup, which usually runs after a timeout, once that deadline has
    passed. ``taskkill`` keeps its own limit instead.

    POSIX: ``SIGKILL`` to the whole process group when ``pid`` leads one
    (and it is not this process's own group), otherwise to ``pid`` alone.
    Returns ``True`` when the signal was delivered or the process was
    already gone.
    """
    if IS_WINDOWS:
        try:
            completed = run_tool_spec(ToolRun(
                argv=("taskkill", "/F", "/T", "/PID", str(pid)),
                timeout=_TASKKILL_TIMEOUT_S,
                text=False,
                bound_by_caller_deadline=False,
            ))
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    getpgid = getattr(os, "getpgid", None)
    try:
        group = getpgid(pid) if getpgid is not None else None
    except ProcessLookupError:
        return True
    except OSError:
        group = None
    try:
        if group == pid and group != os.getpgrp():
            os.killpg(pid, sigkill)
        else:
            os.kill(pid, sigkill)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def terminate_tree(target: ProcessHandle | int) -> bool:
    """Force-end a process and everything it started.

    ``target`` is a process handle or a pid. A handle whose process has
    already exited is left alone, so a reused pid is never hit. Returns the
    result of :func:`terminate_pid`: whether the operating system accepted
    the request. It does not wait. An owner holding a handle confirms the
    exit with ``handle.wait(timeout=...)``.
    """
    if isinstance(target, int):
        return terminate_pid(target)
    try:
        if target.poll() is not None:
            return True
    except (OSError, ValueError):
        pass
    return terminate_pid(target.pid)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


def kill_process_on_port(port: int, *, wait_seconds: float = 5.0) -> bool:
    """Kill any process listening on ``port``, then verify the port is free.

    Returns ``True`` only when the port is confirmed free at the end of the
    wait window, so a caller can refuse to start a listener that would die
    on bind.

    ``os.kill(pid, SIGTERM)`` is unreliable across processes on Windows, so
    the polite signal is followed, halfway through the window, by
    :func:`terminate_pid`. A failed PID lookup never counts as a free port:
    when the lookup cannot say who holds it, this refuses rather than guess.
    """
    # A plain connect is the ground truth for "is the port free". The PID
    # lookups below spawn tools that can take seconds, so they run only
    # when the port is actually held.
    if not _is_port_listening(port):
        return True
    try:
        pids = _find_pids_on_port(port)
    except Exception as exc:
        logger.error(
            "kill_process_on_port(%d): PID lookup failed (%s: %s); "
            "refusing to claim port is free.",
            port, type(exc).__name__, exc,
        )
        return False

    if not pids:
        # Held, but the lookup names nobody: an IPv6-only listener or a
        # process this user may not inspect. It cannot be ended from here.
        logger.error(
            "kill_process_on_port(%d): port is held but PID lookup "
            "returned empty; cannot claim port is free.", port,
        )
        return False

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    deadline = time.monotonic() + wait_seconds
    escalated = False
    last_lookup_exc: Exception | None = None
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if not _is_port_listening(port):
            return True
        try:
            still_held = _find_pids_on_port(port)
            last_lookup_exc = None
        except Exception as exc:
            # The lookup failed mid-loop. The original PIDs are still known,
            # so escalation can go ahead against them.
            last_lookup_exc = exc
            still_held = pids
        if not escalated and time.monotonic() > (deadline - wait_seconds / 2):
            escalated = True
            for pid in still_held:
                terminate_pid(pid)

    if not _is_port_listening(port):
        return True
    if last_lookup_exc is not None:
        logger.error(
            "kill_process_on_port(%d): port still held after %.1fs; "
            "lookup last raised %s: %s",
            port, wait_seconds,
            type(last_lookup_exc).__name__, last_lookup_exc,
        )
    return False


def _is_port_listening(port: int, *, timeout: float = 0.1) -> bool:
    """True when a TCP connect to ``127.0.0.1:port`` succeeds.

    Needs no child process, unlike the PID lookups, which cost seconds on
    Windows when PowerShell has to start.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        s.close()


def _find_pids_on_port(port: int) -> set[int]:
    """PIDs of the processes listening on ``port``. Empty when it is free."""
    if not _is_port_listening(port):
        return set()
    if IS_WINDOWS:
        return _find_pids_on_port_windows(port)
    return _find_pids_on_port_unix(port)


def _find_pids_on_port_windows(port: int) -> set[int]:
    """Windows: ``netstat -ano`` first, then PowerShell's
    ``Get-NetTCPConnection`` when netstat names nobody.

    netstat is a native tool that answers in well under a second. PowerShell
    can take 6 to 15 seconds to start cold, hence its long timeout.
    """
    try:
        result = run_tool(["netstat", "-ano", "-p", "TCP"], timeout=10)
        pids: set[int] = set()
        suffix = f":{port}"
        for line in result.stdout.splitlines():
            # Layout: TCP <local> <foreign> <state> <pid>
            parts = line.split()
            if len(parts) < 5 or parts[0] != "TCP":
                continue
            local = parts[1]
            state = parts[3]
            if state != "LISTENING":
                continue
            # The local address is 0.0.0.0:<port> or [::]:<port>.
            if not local.endswith(suffix):
                continue
            pid_str = parts[-1]
            if pid_str.isdigit() and int(pid_str) > 0:
                pids.add(int(pid_str))
        if pids:
            return pids
        # Either free, or netstat's output could not be read. PowerShell
        # tells the two apart.
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    result = run_tool(
        powershell_argv(
            f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue "
            "| Select-Object -ExpandProperty OwningProcess"
        ),
        timeout=30,
    )
    pids = set()
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.isdigit() and int(line) > 0:
            pids.add(int(line))
    return pids


def _find_pids_on_port_unix(port: int) -> set[int]:
    """Linux and macOS: ``lsof`` first, then ``ss``."""
    pids: set[int] = set()
    try:
        result = run_tool(["lsof", "-ti", f":{port}"], timeout=5)
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
        if pids:
            return pids
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        result = run_tool(["ss", "-tlnp", f"sport = :{port}"], timeout=5)
        for match in re.finditer(r"pid=(\d+)", result.stdout):
            pid = int(match.group(1))
            if pid > 0:
                pids.add(pid)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return pids


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


def find_child_pids(pid: int) -> set[int]:
    """PIDs of the direct children of ``pid``.

    Used before terminating a daemon, so its children do not outlive it: a
    process ended with ``TerminateProcess`` cannot clean up after itself.

    Best effort: returns an empty set when enumeration fails. Callers must
    not rely on completeness. The port cleanup in ``kill_process_on_port``
    remains the second line of defense.
    """
    if IS_WINDOWS:
        return _find_child_pids_windows(pid)
    return _find_child_pids_unix(pid)


def _find_child_pids_windows(pid: int) -> set[int]:
    """Windows: ``wmic`` where it still ships, then PowerShell's
    ``Get-CimInstance``."""
    children: set[int] = set()
    try:
        result = run_tool(
            ["wmic", "process", "where", f"(parentprocessid={pid})", "get", "processid"],
            timeout=10,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit() and int(line) > 0:
                children.add(int(line))
        if children:
            return children
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        result = run_tool(
            powershell_argv(
                f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' "
                "| Select-Object -ExpandProperty ProcessId"
            ),
            timeout=15,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit() and int(line) > 0:
                children.add(int(line))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return children


def _find_child_pids_unix(pid: int) -> set[int]:
    """Linux and macOS: ``pgrep -P``."""
    children: set[int] = set()
    try:
        result = run_tool(["pgrep", "-P", str(pid)], timeout=5)
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit() and int(line) > 0:
                children.add(int(line))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return children


# ---------------------------------------------------------------------------
# The kill-on-close Job Object (Windows)
# ---------------------------------------------------------------------------

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

_job_api_cache: tuple[Any, Any] | None = None


def _job_api() -> tuple[Any, Any]:
    """kernel32 with the Job Object signatures, and the limit-information
    structure, loaded on first use."""
    global _job_api_cache
    if _job_api_cache is not None:
        return _job_api_cache
    import ctypes
    from ctypes import wintypes as wt

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wt.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wt.DWORD),
            ("SchedulingClass", wt.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wt.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.POINTER(JOBOBJECT_EXTENDED_LIMIT_INFORMATION),
        wt.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wt.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wt.BOOL
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    _job_api_cache = (kernel32, JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    return _job_api_cache


def create_kill_on_close_job(*, silent_breakaway: bool = False) -> int | None:
    """Create a Windows Job Object that ends every process in it when its
    last handle closes: when this process dies by any means, including a
    hard kill, a crash, or power loss, where no cleanup code of its own runs.

    ``silent_breakaway=True`` lets the processes in the job start children
    outside it, which then keep whatever job those processes give them. The
    host relaunch parent uses it, so the daemon's services stay in the
    daemon's own job rather than the parent's.

    Returns the job handle on Windows, or ``None`` elsewhere and on failure.
    The handle is never closed: the job lives exactly as long as this
    process, which is the point.

    Windows only, because no other platform has a single mechanism for this.
    Linux's ``PR_SET_PDEATHSIG`` needs an unsafe ``preexec_fn`` in a threaded
    process and fires on thread death, and macOS has no such guarantee. The
    cross-platform baseline is the next start's orphan sweep, through
    :func:`find_child_pids` and :func:`kill_process_on_port`.
    """
    if not IS_WINDOWS:
        return None
    import ctypes

    try:
        kernel32, limit_information = _job_api()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = limit_information()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if silent_breakaway:
            flags |= _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        info.BasicLimitInformation.LimitFlags = flags
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(job)
            raise error
        return int(job)
    except Exception as exc:
        logger.warning("Could not create kill-on-close Job Object: %s", exc)
        return None


def assign_process_to_job(job: int | None, pid: int) -> bool:
    """Put process ``pid`` into ``job``. Best effort, never raises.

    Returns ``False`` (and logs) on POSIX, for a ``None`` job, or when the
    assignment fails. It can fail legitimately when this process already sits
    in a restrictive job of its own, as some terminals and task wrappers
    arrange. That is not fatal: the next start's orphan sweep remains.
    """
    if not IS_WINDOWS or job is None:
        return False
    import ctypes

    try:
        kernel32, _ = _job_api()
        process = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid,
        )
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process)
        return True
    except Exception as exc:
        logger.warning("Could not assign pid %d to Job Object: %s", pid, exc)
        return False
