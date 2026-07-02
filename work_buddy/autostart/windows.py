"""Windows auto-start backend: a per-user Task Scheduler task (no admin).

Launches the sidecar windowless at logon under the provisioned venv's
``pythonw.exe`` (a console-less interpreter), via ``Register-ScheduledTask`` with
``-RunLevel Limited`` (per-user, no UAC). Task name ``WB-Sidecar``. The
PowerShell calls themselves run with ``CREATE_NO_WINDOW`` so registration does
not flash a console.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from work_buddy.autostart import TASK_NAME
from work_buddy.compat import subprocess_creation_flags
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _pythonw(python_exe: str) -> str:
    """Prefer ``pythonw.exe`` (no console) next to ``python.exe``; else python.exe."""
    cand = Path(python_exe).with_name("pythonw.exe")
    return str(cand) if cand.exists() else python_exe


def _run_ps(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=subprocess_creation_flags(),
    )


def register(*, python_exe: str, home_dir: str, data_dir: str) -> dict:
    pyw = _pythonw(python_exe)
    # -Force replaces an existing task, so this is idempotent. pythonw.exe keeps
    # the daemon windowless; a short AtLogOn delay lets the desktop settle first.
    script = (
        f"$a = New-ScheduledTaskAction -Execute '{pyw}' "
        f"-Argument '-m work_buddy.sidecar' -WorkingDirectory '{home_dir}'; "
        f"$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
        f"$t.Delay = 'PT15S'; "
        f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries -StartWhenAvailable "
        f"-ExecutionTimeLimit ([TimeSpan]::Zero); "
        f"$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        f"-LogonType Interactive -RunLevel Limited; "
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t "
        f"-Settings $s -Principal $p -Force "
        f"-Description 'work-buddy sidecar daemon' | Out-Null"
    )
    try:
        r = _run_ps(script)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"Register-ScheduledTask did not run: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "detail": f"Register-ScheduledTask failed: {r.stderr.strip()[:400]}"}
    return {"ok": True, "detail": f"Registered scheduled task {TASK_NAME!r}: {pyw} -m work_buddy.sidecar"}


def unregister() -> dict:
    script = (
        f"if (Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue) "
        f"{{ Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false }}"
    )
    try:
        r = _run_ps(script)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"Unregister did not run: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "detail": f"Unregister failed: {r.stderr.strip()[:400]}"}
    return {"ok": True, "detail": f"Removed scheduled task {TASK_NAME!r} (if present)"}


def is_registered() -> bool:
    script = (
        f"if (Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue) "
        f"{{ 'yes' }} else {{ 'no' }}"
    )
    try:
        r = _run_ps(script, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip() == "yes"
