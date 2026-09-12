"""PID file management for the sidecar daemon.

Ported from ClaudeClaw's pid.ts — adapted for Python/Windows.
"""

import atexit
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve
from work_buddy.utils.process import is_process_alive, process_start_token

logger = get_logger(__name__)

PID_FILE = resolve("runtime/sidecar-pid")

# Process-liveness lives in work_buddy.utils.process (shared with the IR vector
# store's orphan-temp sweep). Kept under the private name for internal callers.
_is_process_alive = is_process_alive


def _identity_file() -> Path:
    """Companion identity for ``PID_FILE`` (derived so test redirection works)."""
    return PID_FILE.with_name(PID_FILE.name + ".identity.json")


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _process_description(pid: int) -> tuple[str, str] | None:
    """Return ``(image_name, command_line)`` for legacy PID validation.

    New sidecars carry a start-token companion and do not pay this subprocess
    cost.  The description path exists only to migrate safely from historical
    integer-only PID files.
    """
    if sys.platform == "win32":
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}';"
            "if($null -ne $p){"
            "[pscustomobject]@{Name=[string]$p.Name;CommandLine=[string]$p.CommandLine}"
            "| ConvertTo-Json -Compress}"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                return None
            return str(value.get("Name") or ""), str(value.get("CommandLine") or "")
        except (
            FileNotFoundError,
            OSError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ):
            return None

    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        name = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        return name, command.decode("utf-8", errors="replace").strip()
    except OSError:
        pass

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        return None
    name, _, command = output.partition(" ")
    return name, command.strip()


def _looks_like_sidecar_process(name: str, command_line: str) -> bool | None:
    """Classify a process description without granting on image name alone."""
    normalized_name = Path(name).name.casefold()
    normalized_command = command_line.replace("\\", "/").casefold()
    if re.search(r"(?:^|\s)-m\s+work_buddy\.sidecar(?:\s|$)", normalized_command):
        return True
    if (
        "wbuddy" in normalized_name
        and "--foreground" in normalized_command
        and re.search(r"(?:^|\s)start(?:\s|$)", normalized_command)
    ):
        return True
    if command_line:
        return False
    if normalized_name and normalized_name not in {
        "python",
        "python.exe",
        "pythonw",
        "pythonw.exe",
        "wbuddy",
        "wbuddy.exe",
    }:
        return False
    return None


def _legacy_process_is_sidecar(pid: int) -> bool | None:
    description = _process_description(pid)
    if description is None:
        return None
    return _looks_like_sidecar_process(*description)


def _record_matches_process(pid: int) -> bool | None:
    """Verify that ``pid`` is the sidecar instance which wrote the record.

    ``None`` means identity could not be proven.  Destructive callers must
    refuse in that state.
    """
    identity_path = _identity_file()
    try:
        value = json.loads(identity_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _legacy_process_is_sidecar(pid)
    except (OSError, json.JSONDecodeError, TypeError):
        return _legacy_process_is_sidecar(pid)

    try:
        recorded_pid = int(value["pid"])
        recorded_token = str(value["start_token"])
    except (KeyError, TypeError, ValueError):
        return _legacy_process_is_sidecar(pid)
    if recorded_pid != pid:
        # A legacy/downgraded daemon may have overwritten only the integer PID
        # file while leaving a newer companion behind. Validate it as legacy.
        return _legacy_process_is_sidecar(pid)
    current_token = process_start_token(pid)
    if current_token is None:
        return None
    return current_token == recorded_token


def check_existing_daemon() -> int | None:
    """Check if a daemon is already running.

    Returns the PID if alive, ``None`` otherwise.
    Cleans up stale PID files automatically.
    """
    if not PID_FILE.exists():
        return None

    pid = _read_pid()
    if pid is None:
        logger.warning("Corrupt PID file — removing: %s", PID_FILE)
        _remove_pid_file()
        return None

    if _is_process_alive(pid):
        matches = _record_matches_process(pid)
        if matches is False:
            logger.warning(
                "Stale PID file names a different process (pid=%d); "
                "removing it without terminating that process.",
                pid,
            )
            _remove_pid_file()
            return None
        if matches is None:
            logger.warning(
                "Could not verify whether live pid=%d is the recorded sidecar; "
                "leaving the PID file in place.",
                pid,
            )
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

    # Capture the process lifetime before validating the persisted record.
    # Rechecking this token after validation closes the gap where the recorded
    # process exits and Windows reuses its PID between those two operations.
    expected_start = process_start_token(pid)
    if expected_start is None:
        if not _is_process_alive(pid):
            _remove_pid_file()
            return True
        logger.error(
            "Refusing to terminate pid=%d because its process-start identity "
            "could not be read.",
            pid,
        )
        return False

    matches = _record_matches_process(pid)
    if matches is False:
        logger.warning(
            "Refusing to terminate pid=%d because it is not the recorded "
            "sidecar; removing the stale PID file.",
            pid,
        )
        _remove_pid_file()
        return True
    if matches is None:
        logger.error(
            "Refusing to terminate pid=%d because its sidecar identity "
            "could not be verified.",
            pid,
        )
        return False

    if process_start_token(pid) != expected_start:
        logger.info(
            "Recorded sidecar pid=%d exited or was reused during identity "
            "verification; leaving the replacement process untouched.",
            pid,
        )
        _remove_pid_file()
        return True

    logger.info("Taking over existing sidecar (pid=%d)...", pid)

    # Kill children first. The old daemon will not get a chance to run
    # its own ``_stop_child`` calls because we terminate it via
    # TerminateProcess on Windows / SIGTERM-as-hard-signal on Unix.
    children = find_child_pids(pid)
    if process_start_token(pid) != expected_start:
        logger.info(
            "Recorded sidecar pid=%d exited or was reused during takeover; "
            "leaving the replacement process untouched.",
            pid,
        )
        _remove_pid_file()
        return True
    if children:
        logger.info(
            "Reaping %d child process(es) of old daemon: %s",
            len(children), sorted(children),
        )
        for child_pid in children:
            _force_kill_pid(child_pid)

    if process_start_token(pid) != expected_start:
        _remove_pid_file()
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass

    deadline = _time.monotonic() + wait_seconds
    escalated = False
    while _time.monotonic() < deadline:
        if process_start_token(pid) != expected_start:
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
            if process_start_token(pid) != expected_start:
                _remove_pid_file()
                return True
            _force_kill_pid(pid)

    if process_start_token(pid) == expected_start:
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
    start_token = process_start_token(pid)
    if start_token is None:
        raise RuntimeError("Could not determine sidecar process-start identity")

    identity_path = _identity_file()
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_fd, identity_tmp = tempfile.mkstemp(
        dir=identity_path.parent,
        prefix=".sidecar_identity_",
        suffix=".tmp",
    )
    try:
        os.write(
            identity_fd,
            json.dumps(
                {"pid": pid, "start_token": start_token},
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        os.close(identity_fd)
        os.replace(identity_tmp, identity_path)
    except Exception:
        try:
            os.close(identity_fd)
        except OSError:
            pass
        try:
            os.unlink(identity_tmp)
        except OSError:
            pass
        raise

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
    """Remove the PID file if it records this process. Safe to call
    multiple times.

    Guarded on pid ownership: ``write_pid_file`` registers this as an
    atexit hook, and that hook can fire long after another daemon has
    taken over and written its own pid to the file. An unguarded delete
    would erase the live successor's pid file, leaving ``wbuddy status``
    reporting a healthy daemon as not running until its next restart.
    A file naming a different pid is therefore left alone. Stale-file
    removal for dead foreign pids belongs to ``check_existing_daemon``.
    """
    try:
        recorded = int(PID_FILE.read_text().strip())
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        # Unreadable or corrupt: ownership is unknowable. Leave it for
        # check_existing_daemon, which removes corrupt files explicitly.
        return
    if recorded != os.getpid():
        logger.debug(
            "cleanup_pid_file: %s records pid=%d (not ours, %d), leaving it.",
            PID_FILE, recorded, os.getpid(),
        )
        return
    _remove_pid_file()


def _remove_pid_file() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _identity_file().unlink(missing_ok=True)
    except OSError:
        pass
