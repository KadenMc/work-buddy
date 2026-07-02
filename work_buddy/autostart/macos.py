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


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "work-buddy"


def _write_plist(python_exe: str, home_dir: str, data_dir: str) -> Path:
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [python_exe, "-m", "work_buddy.sidecar"],
        "EnvironmentVariables": {"WORK_BUDDY_DATA_DIR": data_dir},
        "WorkingDirectory": home_dir,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "sidecar.out.log"),
        "StandardErrorPath": str(log_dir / "sidecar.err.log"),
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    return path


def register(*, python_exe: str, home_dir: str, data_dir: str) -> dict:
    path = _write_plist(python_exe, home_dir, data_dir)
    uid = os.getuid()
    # Bootout any prior instance so bootstrap does not fail on a stale label.
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{AGENT_LABEL}"],
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
    return {"ok": True, "detail": f"Loaded LaunchAgent {AGENT_LABEL}: {path}"}


def unregister() -> dict:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{AGENT_LABEL}"],
        capture_output=True, text=True,
    )
    _plist_path().unlink(missing_ok=True)
    return {"ok": True, "detail": f"Unloaded and removed LaunchAgent {AGENT_LABEL} (if present)"}


def is_registered() -> bool:
    return _plist_path().exists()
