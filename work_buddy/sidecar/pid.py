"""PID file management for the sidecar daemon.

Ported from ClaudeClaw's pid.ts — adapted for Python/Windows.
"""

import atexit
import os
import signal
import sys
import tempfile
from pathlib import Path

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve

logger = get_logger(__name__)

PID_FILE = resolve("runtime/sidecar-pid")


def _is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running.

    Uses ctypes on Windows (os.kill signal-0 is unreliable there),
    falls back to os.kill on other platforms.
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


def check_existing_daemon() -> int | None:
    """Check if a daemon is already running.

    Returns the PID if alive, ``None`` otherwise.
    Cleans up stale PID files automatically.
    """
    if not PID_FILE.exists():
        return None

    try:
        pid_text = PID_FILE.read_text().strip()
        pid = int(pid_text)
    except (ValueError, OSError):
        logger.warning("Corrupt PID file — removing: %s", PID_FILE)
        _remove_pid_file()
        return None

    if _is_process_alive(pid):
        return pid

    logger.info("Stale PID file (pid=%d not alive) — removing.", pid)
    _remove_pid_file()
    return None


def write_pid_file() -> None:
    """Write the current process PID to the PID file (atomic on NTFS)."""
    pid = os.getpid()

    # Atomic write: write to temp, then rename
    fd, tmp_path = tempfile.mkstemp(
        dir=PID_FILE.parent, prefix=".sidecar_pid_", suffix=".tmp"
    )
    try:
        os.write(fd, f"{pid}\n".encode())
        os.close(fd)
        os.replace(tmp_path, PID_FILE)
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    atexit.register(cleanup_pid_file)
    logger.info("PID file written: %s (pid=%d)", PID_FILE, pid)


def cleanup_pid_file() -> None:
    """Remove the PID file. Safe to call multiple times."""
    _remove_pid_file()


def _remove_pid_file() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
