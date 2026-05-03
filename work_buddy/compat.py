"""Cross-platform compatibility helpers.

Centralizes platform detection and provides OS-appropriate
implementations for subprocess management, path resolution,
and process utilities.
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def subprocess_creation_flags() -> int:
    """Return subprocess creation flags appropriate for the current OS.

    On Windows, returns CREATE_NO_WINDOW to suppress console windows.
    On Unix, returns 0 (no special flags needed).
    """
    if IS_WINDOWS:
        return subprocess.CREATE_NO_WINDOW
    return 0


def detached_process_kwargs() -> dict:
    """Return kwargs for launching a fully detached background process.

    On Windows: CREATE_NO_WINDOW | DETACHED_PROCESS via creationflags.
    On Unix: start_new_session=True to detach from parent's process group.
    """
    if IS_WINDOWS:
        return {
            "creationflags": (
                subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            ),
        }
    return {"start_new_session": True}


def kill_process_on_port(port: int, *, wait_seconds: float = 5.0) -> bool:
    """Kill any process listening on the given port, then verify.

    Returns True when the port is confirmed free at the end of the
    wait window, False otherwise. Callers can use the return value to
    decide whether to proceed with binding their own listener.

    Windows gotcha: ``os.kill(pid, SIGTERM)`` is unreliable
    cross-process on Windows (works within the same console only, or
    not at all). This implementation tries SIGTERM first, then
    escalates to ``taskkill /F /PID`` on Windows — that one actually
    works on orphaned children from a previous sidecar run. Unix uses
    SIGTERM followed by SIGKILL.

    Real-world failure (2026-04-17): a sidecar restart left an old
    mcp_gateway (PID 22636) holding port 5126. ``os.kill`` reported
    success but did nothing. The newly-spawned child failed to bind
    and died. The sidecar logs said "Started mcp_gateway (pid=...)"
    so nothing looked wrong — except the wrong bytecode was live.
    This function's new verify-the-port-is-actually-free contract
    prevents that silent-failure mode.
    """
    import logging
    import signal
    import time as _time

    log = logging.getLogger(__name__)

    # CRITICAL: distinguish "no PIDs found" from "PID lookup failed".
    # On Windows, Get-NetTCPConnection inside PowerShell can take
    # 6–15s on a cold console (profile load + cmdlet JIT). The previous
    # 5s subprocess timeout combined with `except Exception: pids =
    # set()` produced a SILENT FALSE POSITIVE — the function returned
    # True ("port cleaned") even when the lookup never completed,
    # leaving the old process bound to the port. The new sidecar's
    # _start_child then spawned a child that died on bind while the
    # orphan kept serving requests with stale code, with health probes
    # cheerfully reporting 200 OK against the wrong process.
    #
    # Fix: use _is_port_listening (cheap, no subprocess) as the
    # ground-truth signal. Only return True when we have evidence the
    # port is free; on lookup failure, refuse rather than guess.
    if not _is_port_listening(port):
        return True
    try:
        pids = _find_pids_on_port(port)
    except Exception as exc:
        log.error(
            "kill_process_on_port(%d): PID lookup failed (%s: %s); "
            "refusing to claim port is free.",
            port, type(exc).__name__, exc,
        )
        return False

    if not pids:
        # Port held but lookup says no PIDs — could be IPv6-only listener
        # or a permission-restricted process. Can't kill what we can't
        # identify; refuse rather than mislead.
        log.error(
            "kill_process_on_port(%d): port is held but PID lookup "
            "returned empty; cannot claim port is free.", port,
        )
        return False

    # First pass: polite SIGTERM
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    # Poll until the port is free OR we time out
    deadline = _time.monotonic() + wait_seconds
    escalated = False
    last_lookup_exc: Exception | None = None
    while _time.monotonic() < deadline:
        _time.sleep(0.2)
        # Cheap pre-check: port free? — done.
        if not _is_port_listening(port):
            return True
        try:
            still_held = _find_pids_on_port(port)
            last_lookup_exc = None
        except Exception as exc:
            # Lookup failed mid-loop. Don't pretend the port is free —
            # but we still know the original PIDs to escalate against.
            last_lookup_exc = exc
            still_held = pids
        # Halfway through the window, escalate to force-kill
        if not escalated and _time.monotonic() > (deadline - wait_seconds / 2):
            escalated = True
            for pid in still_held:
                _force_kill_pid(pid)

    # Final answer must be truthful. _is_port_listening is the
    # ground-truth signal — never claim "free" without it agreeing.
    if not _is_port_listening(port):
        return True
    if last_lookup_exc is not None:
        log.error(
            "kill_process_on_port(%d): port still held after %.1fs; "
            "lookup last raised %s: %s",
            port, wait_seconds,
            type(last_lookup_exc).__name__, last_lookup_exc,
        )
    return False


def _force_kill_pid(pid: int) -> None:
    """Force-terminate a process using the most reliable method per OS."""
    if IS_WINDOWS:
        try:
            # taskkill /F works cross-process on Windows where os.kill
            # often silently fails. /T kills the process tree so any
            # children spawned by the orphan also go.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5, check=False,
            )
        except Exception:
            pass
    else:
        import signal
        # SIGKILL doesn't exist on Windows; on Unix it does. Use
        # getattr so this module imports cleanly on either platform
        # — the IS_WINDOWS branch above already handles Windows.
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.kill(pid, sigkill)
        except (OSError, ProcessLookupError):
            pass


def _is_port_listening(port: int, *, timeout: float = 0.1) -> bool:
    """Fast socket-based check: is anything listening on localhost:port?

    Avoids spawning PowerShell (Windows) or lsof/ss (Unix) just to
    discover the port is free — a 3–5s cost on Windows due to
    PowerShell cold-start. Returns True iff a TCP connect to
    127.0.0.1:port succeeds.
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
    """Find PIDs of processes listening on a given port.

    Fast path: if nothing is listening, return empty set without
    spawning a PID-enumeration subprocess. Only the kill-the-orphan
    path needs actual PIDs.
    """
    if not _is_port_listening(port):
        return set()
    if IS_WINDOWS:
        return _find_pids_on_port_windows(port)
    return _find_pids_on_port_unix(port)


def _find_pids_on_port_windows(port: int) -> set[int]:
    """Find PIDs of Windows processes listening on ``port``.

    Tries fast path first (``netstat -ano`` — no PowerShell cold start),
    falls back to PowerShell ``Get-NetTCPConnection`` if netstat parsing
    fails. Both paths use ``-NoProfile`` and a generous timeout because
    PowerShell on Windows is notoriously slow on first invocation
    (6–15s with profile load) — and the previous 5s timeout was the
    direct cause of a long-lived orphan-gateway bug.
    """
    # Fast path: netstat is a native Win32 tool, ~50–200ms cold.
    # Output columns: Proto Local Foreign State PID
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
        )
        pids: set[int] = set()
        suffix = f":{port}"
        for line in result.stdout.splitlines():
            parts = line.split()
            # Match LISTENING rows with Local addr ending in :<port>.
            # Layout: TCP <local> <foreign> <state> <pid>
            if len(parts) < 5 or parts[0] != "TCP":
                continue
            local = parts[1]
            state = parts[3]
            if state != "LISTENING":
                continue
            # Local can be 0.0.0.0:5126 or [::]:5126 — both end with :port
            if not local.endswith(suffix):
                continue
            pid_str = parts[-1]
            if pid_str.isdigit() and int(pid_str) > 0:
                pids.add(int(pid_str))
        if pids:
            return pids
        # No matches: either truly free or netstat output unparseable.
        # Fall through to PowerShell to disambiguate.
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # fall through

    # Slow path: PowerShell. Use -NoProfile to skip 5–10s of profile
    # loading, and bump timeout to 30s to ride out cmdlet JIT.
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-Command",
            f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue "
            "| Select-Object -ExpandProperty OwningProcess",
        ],
        capture_output=True, text=True, timeout=30,
    )
    pids = set()
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.isdigit() and int(line) > 0:
            pids.add(int(line))
    return pids


def _find_pids_on_port_unix(port: int) -> set[int]:
    """Use lsof or ss to find PIDs on a port (Linux/macOS)."""
    pids: set[int] = set()

    # Try lsof first (available on macOS and most Linux)
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
        if pids:
            return pids
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: ss (Linux)
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=5,
        )
        import re
        for match in re.finditer(r"pid=(\d+)", result.stdout):
            pid = int(match.group(1))
            if pid > 0:
                pids.add(pid)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return pids


def obsidian_log_path() -> Path:
    """Resolve the Obsidian main process log file path for the current OS."""
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            appdata = str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "obsidian" / "obsidian.log"
    elif IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.log"
    else:
        # Linux: XDG_CONFIG_HOME or ~/.config
        config_home = os.environ.get("XDG_CONFIG_HOME", "")
        if not config_home:
            config_home = str(Path.home() / ".config")
        return Path(config_home) / "obsidian" / "obsidian.log"


def chrome_native_messaging_dir() -> Path:
    """Resolve Chrome's native messaging hosts directory for the current OS."""
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "Google" / "Chrome" / "NativeMessagingHosts"
    elif IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
    else:
        return Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"


def conda_activate_command(repo_root: str, module: str) -> list[str]:
    """Build a command to run a Python module in the work-buddy conda env.

    On Windows: uses powershell.exe with conda activate.
    On Unix: uses bash with conda activate (assumes conda init has been done).
    """
    if IS_WINDOWS:
        return [
            "powershell.exe", "-Command",
            f"cd '{repo_root}'; conda activate work-buddy; "
            f"python -m {module}",
        ]
    else:
        return [
            "bash", "-c",
            f"cd '{repo_root}' && "
            f"eval \"$(conda shell.bash hook 2>/dev/null)\" && "
            f"conda activate work-buddy && "
            f"python -m {module}",
        ]
