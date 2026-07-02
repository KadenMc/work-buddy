"""Linux auto-start backend: a systemd ``--user`` unit (no root).

Writes ``~/.config/systemd/user/wb-sidecar.service`` and enables it, so the
sidecar starts at login under the provisioned venv python. No sudo. For
start-before-login (headless hosts) the user can opt into
``loginctl enable-linger`` separately; that is documented, not automatic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from work_buddy.autostart import UNIT_NAME
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_UNIT_TEMPLATE = """\
[Unit]
Description=work-buddy sidecar daemon
After=default.target

[Service]
Type=simple
WorkingDirectory={home}
Environment=WORK_BUDDY_DATA_DIR={data}
ExecStart={python} -m work_buddy.sidecar
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{UNIT_NAME}.service"


def _systemctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def register(*, python_exe: str, home_dir: str, data_dir: str) -> dict:
    unit = _unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        _UNIT_TEMPLATE.format(home=home_dir, data=data_dir, python=python_exe)
    )
    try:
        _systemctl("daemon-reload")
        r = _systemctl("enable", "--now", UNIT_NAME)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"systemctl did not run: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "detail": f"systemctl enable failed: {r.stderr.strip()[:400]}"}
    return {"ok": True, "detail": f"Enabled systemd --user unit {UNIT_NAME}: {unit}"}


def unregister() -> dict:
    try:
        _systemctl("disable", "--now", UNIT_NAME)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"systemctl did not run: {exc}"}
    _unit_path().unlink(missing_ok=True)
    try:
        _systemctl("daemon-reload")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"ok": True, "detail": f"Disabled and removed {UNIT_NAME} (if present)"}


def is_registered() -> bool:
    if not _unit_path().exists():
        return False
    try:
        r = _systemctl("is-enabled", UNIT_NAME, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return True  # unit file exists but systemctl is unavailable; treat as present
    return r.returncode == 0 and r.stdout.strip() in {
        "enabled",
        "enabled-runtime",
        "static",
        "linked",
    }
