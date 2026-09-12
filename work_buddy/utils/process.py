"""Process utilities for work-buddy.

A cross-platform process-liveness primitive, shared by the sidecar's PID-file
management and the IR vector store's orphan-temp recovery sweep.
"""

import os
import subprocess
import sys
from pathlib import Path


def is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running.

    Uses ctypes on Windows (``os.kill`` signal-0 is unreliable there),
    falls back to ``os.kill`` on other platforms.
    """
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            # OpenProcess can succeed on terminated processes whose handles
            # haven't been fully released. Check the actual exit code:
            # STILL_ACTIVE (259) means genuinely running.
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259  # STILL_ACTIVE
            return False  # couldn't query — treat as dead
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (OSError, ProcessLookupError):
            return False


def process_start_token(pid: int) -> str | None:
    """Return an OS-stable token for one lifetime of ``pid``.

    PIDs are reused, so liveness alone cannot prove that a process is the one
    which originally wrote a PID file.  The token is deliberately opaque: it
    only needs to compare equal while the same process is alive and differ
    after the OS reuses the numeric PID.

    Returns ``None`` when the process is gone or its start identity cannot be
    queried.  Callers performing destructive work must treat that as
    unverified rather than falling back to PID-only identity.
    """
    if pid <= 0:
        return None

    if sys.platform == "win32":
        return _windows_process_start_token(pid)

    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8")
        # Field 2 (comm) is parenthesized and may contain spaces or ``)``.
        # Splitting after its final close-paren leaves field 3 at index 0;
        # Linux's process start time is field 22, therefore index 19.
        fields = raw.rsplit(")", 1)[1].split()
        return f"proc:{fields[19]}"
    except (OSError, IndexError):
        pass

    # macOS and other POSIX hosts do not expose Linux /proc.  ``lstart`` is
    # constant for a process lifetime and includes second-level resolution;
    # PID reuse within the same second is possible in theory, so callers still
    # pair this token with an explicit process-kind check before termination.
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    started = result.stdout.strip()
    return f"ps:{started}" if result.returncode == 0 and started else None


def _windows_process_start_token(pid: int) -> str | None:
    """Read a Windows process creation FILETIME using only the stdlib."""
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = _FILETIME()
        exited = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not get_process_times(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return f"win32:{ticks}"
    finally:
        close_handle(handle)
