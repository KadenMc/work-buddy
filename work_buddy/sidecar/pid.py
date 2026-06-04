"""PID file management for the sidecar daemon.

Ported from ClaudeClaw's pid.ts — adapted for Python/Windows.
"""

import atexit
import os
import signal
import tempfile
from pathlib import Path

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve
from work_buddy.utils.process import is_process_alive

logger = get_logger(__name__)

PID_FILE = resolve("runtime/sidecar-pid")

# Process-liveness lives in work_buddy.utils.process (shared with the IR vector
# store's orphan-temp sweep). Kept under the private name for internal callers.
_is_process_alive = is_process_alive


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


def takeover_existing_daemon(pid: int, *, wait_seconds: float = 10.0) -> bool:
    """Terminate an existing sidecar process so a new one can take over.

    On Windows, ``os.kill(pid, SIGTERM)`` is ``TerminateProcess`` — a
    hard kill that does **not** trigger the daemon's signal handler
    or its ``_shutdown`` cleanup. Without further action, the old
    daemon's child services (messaging, dashboard, …) orphan and
    survive on their bound ports, where they continue serving stale
    in-memory bytecode while the new daemon is unable to displace
    them. (May 2026: a dashboard child orphaned this way ran for 16
    days across multiple sidecar restarts.)

    To prevent that, we kill the daemon's direct children first, then
    the daemon itself. The order matters: terminating children before
    the supervisor avoids it triggering its own restart logic, and
    means even a hard-killed daemon never leaks orphans.

    Returns True once the daemon PID is confirmed dead.
    """
    import time as _time

    from work_buddy.compat import _force_kill_pid, find_child_pids  # type: ignore[attr-defined]

    logger.info("Taking over existing sidecar (pid=%d)...", pid)

    # Kill children first. The old daemon will not get a chance to run
    # its own ``_stop_child`` calls because we terminate it via
    # TerminateProcess on Windows / SIGTERM-as-hard-signal on Unix.
    children = find_child_pids(pid)
    if children:
        logger.info(
            "Reaping %d child process(es) of old daemon: %s",
            len(children), sorted(children),
        )
        for child_pid in children:
            _force_kill_pid(child_pid)

    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass

    deadline = _time.monotonic() + wait_seconds
    escalated = False
    while _time.monotonic() < deadline:
        if not _is_process_alive(pid):
            _remove_pid_file()
            logger.info("Previous sidecar (pid=%d) terminated.", pid)
            return True
        _time.sleep(0.2)
        if not escalated and _time.monotonic() > (deadline - wait_seconds / 2):
            escalated = True
            logger.warning(
                "Previous sidecar (pid=%d) did not exit on SIGTERM — "
                "escalating to force-kill.", pid,
            )
            _force_kill_pid(pid)

    if _is_process_alive(pid):
        logger.error(
            "Could not terminate existing sidecar (pid=%d) within %.0fs.",
            pid, wait_seconds,
        )
        return False
    _remove_pid_file()
    return True


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
