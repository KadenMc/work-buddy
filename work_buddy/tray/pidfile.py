"""Single-instance pid file for the tray process.

Mirror of ``sidecar/pid.py`` with one addition: :func:`withdraw`, the
cross-process graceful-stop signal. ``tray.stop_running`` removes the file;
the running tray re-checks :func:`owns_pid_file` on every poll tick and quits
cleanly when it no longer owns it (which also self-heals the rare double-spawn
race: the loser notices a successor's pid in the file and exits).
"""

from __future__ import annotations

import atexit
import os
import tempfile

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve
from work_buddy.utils.process import is_process_alive

logger = get_logger(__name__)

TRAY_PID_FILE = resolve("runtime/tray-pid")


def check_existing_tray() -> int | None:
    """Return the pid of a live tray, else ``None`` (cleaning stale files)."""
    if not TRAY_PID_FILE.exists():
        return None
    try:
        pid = int(TRAY_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        logger.warning("Corrupt tray pid file - removing: %s", TRAY_PID_FILE)
        _remove_pid_file()
        return None
    if is_process_alive(pid):
        return pid
    logger.info("Stale tray pid file (pid=%d not alive) - removing.", pid)
    _remove_pid_file()
    return None


def write_pid_file() -> None:
    """Record this process as the tray instance (atomic write + atexit cleanup)."""
    pid = os.getpid()
    TRAY_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=TRAY_PID_FILE.parent, prefix=".tray_pid_", suffix=".tmp"
    )
    try:
        os.write(fd, f"{pid}\n".encode())
        os.close(fd)
        os.replace(tmp_path, TRAY_PID_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    atexit.register(cleanup_pid_file)
    logger.info("Tray pid file written: %s (pid=%d)", TRAY_PID_FILE, pid)


def owns_pid_file() -> bool:
    """True while the file exists and records THIS process.

    The running tray checks this every poll tick: a missing file is the
    graceful stop signal from ``stop_running``, and a file naming another pid
    means a successor took over, so this instance should bow out.
    """
    try:
        return int(TRAY_PID_FILE.read_text().strip()) == os.getpid()
    except (FileNotFoundError, ValueError, OSError):
        return False


def withdraw() -> None:
    """Remove the pid file regardless of owner: the graceful-stop signal."""
    _remove_pid_file()


def cleanup_pid_file() -> None:
    """Remove the pid file if it records this process (ownership-guarded).

    Same guard as the sidecar's: the atexit hook can fire long after another
    tray has taken over, and an unguarded delete would erase the successor's
    file.
    """
    try:
        recorded = int(TRAY_PID_FILE.read_text().strip())
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        return
    if recorded != os.getpid():
        return
    _remove_pid_file()


def _remove_pid_file() -> None:
    try:
        TRAY_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
