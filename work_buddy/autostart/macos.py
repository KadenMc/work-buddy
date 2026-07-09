"""macOS auto-start backend: a launchd LaunchAgent (per-user, no root).

Writes ``~/Library/LaunchAgents/com.workbuddy.sidecar.plist`` and loads it, so
the sidecar starts at login under the provisioned venv python. launchd agents
are inherently windowless; ``ProcessType=Background`` keeps it out of the UI.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from work_buddy.autostart import AGENT_LABEL
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _plist_path(label: str | None = None) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label or AGENT_LABEL}.plist"


def _log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "work-buddy"


def _write_plist(
    python_exe: str,
    home_dir: str,
    data_dir: str,
    *,
    label: str,
    module: str,
    keep_alive: bool,
    log_basename: str,
) -> Path:
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": label,
        "ProgramArguments": [python_exe, "-m", module],
        "EnvironmentVariables": {"WORK_BUDDY_DATA_DIR": data_dir},
        "WorkingDirectory": home_dir,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / f"{log_basename}.out.log"),
        "StandardErrorPath": str(log_dir / f"{log_basename}.err.log"),
    }
    if keep_alive:
        plist["KeepAlive"] = {"SuccessfulExit": False}
    path = _plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    return path


def register(
    *,
    python_exe: str,
    home_dir: str,
    data_dir: str,
    name: str | None = None,
    module: str = "work_buddy.sidecar",
    description: str = "work-buddy sidecar daemon",  # launchd has no description field; accepted for interface parity
    keep_alive: bool = True,
    log_basename: str = "sidecar",
) -> dict:
    label = name or AGENT_LABEL
    path = _write_plist(
        python_exe, home_dir, data_dir,
        label=label, module=module, keep_alive=keep_alive, log_basename=log_basename,
    )
    uid = os.getuid()
    # Bootout any prior instance so bootstrap does not fail on a stale label.
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{label}"],
        capture_output=True, text=True,
    )
    try:
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"launchctl did not run: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "detail": f"launchctl bootstrap failed: {r.stderr.strip()[:400]}"}
    return {"ok": True, "detail": f"Loaded LaunchAgent {label}: {path}"}


def unregister(*, name: str | None = None) -> dict:
    label = name or AGENT_LABEL
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{label}"],
        capture_output=True, text=True,
    )
    _plist_path(label).unlink(missing_ok=True)
    return {"ok": True, "detail": f"Unloaded and removed LaunchAgent {label} (if present)"}


def is_registered(*, name: str | None = None) -> bool:
    return _plist_path(name).exists()
