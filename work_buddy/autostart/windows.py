"""Windows auto-start backend: a per-user Task Scheduler task (no admin).

Starts the sidecar at logon under the provisioned venv's ``pythonw.exe``, via
``Register-ScheduledTask`` with ``-RunLevel Limited`` (per-user, no UAC). Task
name ``WB-Sidecar``. ``pythonw.exe`` has no console, so logon shows no window.
The daemon's entry point then gives itself a console with no window (see
``work_buddy.process.establish_host_context``), so the console programs it
runs stay invisible too. The PowerShell calls run as tool runs, so
registration opens no window either.
"""

from __future__ import annotations

import subprocess

from work_buddy.autostart import TASK_NAME
from work_buddy.logging_config import get_logger
from work_buddy.process import powershell_argv, run_tool
from work_buddy.process.host import sibling_image

logger = get_logger(__name__)


def _pythonw(python_exe: str) -> str:
    """Prefer ``pythonw.exe`` (no console) next to ``python.exe``; else python.exe."""
    return sibling_image(python_exe, "gui")[0]


def _run_ps(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return run_tool(powershell_argv(script), timeout=timeout)


def register(
    *,
    python_exe: str,
    home_dir: str,
    data_dir: str,
    name: str | None = None,
    module: str = "work_buddy.sidecar",
    description: str = "work-buddy sidecar daemon",
) -> dict:
    task = name or TASK_NAME
    pyw = _pythonw(python_exe)
    # Delete any existing task first, then create fresh. Register-ScheduledTask
    # -Force *should* overwrite, but overwriting a task created in another context
    # can fail with "Access is denied"; an explicit delete-then-create is more
    # reliable. Deleting a task you own is permitted unelevated. -Force stays as a
    # belt-and-suspenders. pythonw.exe keeps the child windowless; a short AtLogOn
    # delay lets the desktop settle first.
    unregister(name=task)  # best-effort; ignore result (there may be no existing task)
    # Escape single quotes for the single-quoted PowerShell strings below, so a
    # path like C:\Users\O'Brien\work-buddy cannot break out of the string.
    pyw_ps = pyw.replace("'", "''")
    home_ps = home_dir.replace("'", "''")
    desc_ps = description.replace("'", "''")
    script = (
        f"$a = New-ScheduledTaskAction -Execute '{pyw_ps}' "
        f"-Argument '-m {module}' -WorkingDirectory '{home_ps}'; "
        f"$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
        f"$t.Delay = 'PT15S'; "
        f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries -StartWhenAvailable "
        f"-ExecutionTimeLimit ([TimeSpan]::Zero); "
        f"$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        f"-LogonType Interactive -RunLevel Limited; "
        f"Register-ScheduledTask -TaskName '{task}' -Action $a -Trigger $t "
        f"-Settings $s -Principal $p -Force "
        f"-Description '{desc_ps}' | Out-Null"
    )
    try:
        r = _run_ps(script)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"Register-ScheduledTask did not run: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "detail": f"Register-ScheduledTask failed: {r.stderr.strip()[:400]}"}
    return {"ok": True, "detail": f"Registered scheduled task {task!r}: {pyw} -m {module}"}


def unregister(*, name: str | None = None) -> dict:
    task = name or TASK_NAME
    script = (
        f"if (Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue) "
        f"{{ Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false }}"
    )
    try:
        r = _run_ps(script)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"Unregister did not run: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "detail": f"Unregister failed: {r.stderr.strip()[:400]}"}
    return {"ok": True, "detail": f"Removed scheduled task {task!r} (if present)"}


def is_registered(*, name: str | None = None) -> bool:
    task = name or TASK_NAME
    script = (
        f"if (Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue) "
        f"{{ 'yes' }} else {{ 'no' }}"
    )
    try:
        r = _run_ps(script, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip() == "yes"
