"""Cross-platform compatibility helpers.

Centralizes platform detection and provides OS-appropriate
implementations for subprocess management, path resolution,
and process utilities.
"""

import os
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


def kill_process_on_port(port: int) -> None:
    """Kill any process listening on the given port (best-effort cleanup).

    Uses platform-appropriate commands to find and kill the process.
    """
    import signal

    try:
        pids = _find_pids_on_port(port)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
    except Exception:
        pass  # Best-effort cleanup


def _find_pids_on_port(port: int) -> set[int]:
    """Find PIDs of processes listening on a given port."""
    if IS_WINDOWS:
        return _find_pids_on_port_windows(port)
    return _find_pids_on_port_unix(port)


def _find_pids_on_port_windows(port: int) -> set[int]:
    """Use PowerShell Get-NetTCPConnection to find PIDs on a port."""
    result = subprocess.run(
        [
            "powershell.exe", "-Command",
            f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue "
            "| Select-Object -ExpandProperty OwningProcess",
        ],
        capture_output=True, text=True, timeout=5,
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
