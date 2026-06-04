"""Process utilities for work-buddy.

A cross-platform process-liveness primitive, shared by the sidecar's PID-file
management and the IR vector store's orphan-temp recovery sweep.
"""

import os
import sys


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
