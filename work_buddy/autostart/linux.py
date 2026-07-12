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
Description={description}
After=default.target

[Service]
Type=simple
WorkingDirectory={home}
Environment={data_env}
ExecStart={python} -m {module}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _unit_quote(value: str) -> str:
    """Quote one systemd directive value without allowing specifier expansion."""
    if "\n" in value or "\r" in value:
        raise ValueError("systemd unit values cannot contain newlines")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _unit_path(unit: str | None = None) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{unit or UNIT_NAME}.service"


def _systemctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def register(
    *,
    python_exe: str,
    home_dir: str,
    data_dir: str,
    name: str | None = None,
    module: str = "work_buddy.sidecar",
    description: str = "work-buddy sidecar daemon",
) -> dict:
    unit_name = name or UNIT_NAME
    unit = _unit_path(unit_name)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        _UNIT_TEMPLATE.format(
            home=_unit_quote(home_dir),
            data_env=_unit_quote(f"WORK_BUDDY_DATA_DIR={data_dir}"),
            python=_unit_quote(python_exe),
            module=module,
            description=description,
        )
    )
    try:
        _systemctl("daemon-reload")
        r = _systemctl("enable", "--now", unit_name)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"systemctl did not run: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "detail": f"systemctl enable failed: {r.stderr.strip()[:400]}"}
    return {"ok": True, "detail": f"Enabled systemd --user unit {unit_name}: {unit}"}


def unregister(*, name: str | None = None) -> dict:
    unit_name = name or UNIT_NAME
    try:
        _systemctl("disable", "--now", unit_name)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"systemctl did not run: {exc}"}
    _unit_path(unit_name).unlink(missing_ok=True)
    try:
        _systemctl("daemon-reload")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"ok": True, "detail": f"Disabled and removed {unit_name} (if present)"}


def is_registered(*, name: str | None = None) -> bool:
    unit_name = name or UNIT_NAME
    if not _unit_path(unit_name).exists():
        return False
    try:
        r = _systemctl("is-enabled", unit_name, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return True  # unit file exists but systemctl is unavailable; treat as present
    return r.returncode == 0 and r.stdout.strip() in {
        "enabled",
        "enabled-runtime",
        "static",
        "linked",
    }
