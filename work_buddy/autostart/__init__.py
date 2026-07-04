"""Login auto-start registration for the work-buddy sidecar.

Registers a detached, windowless sidecar to launch at login under the
interpreter the installer provisioned (the uv venv's python), so a fresh machine
brings work-buddy up without a manual ``wbuddy start``. Per-OS backends: Windows
Task Scheduler, Linux systemd ``--user``, macOS launchd LaunchAgent. The
task/unit/agent names are stable so health checks and
``notifications.service_hints`` stay correct.

Public surface: :func:`register` / :func:`unregister` / :func:`is_registered` /
:func:`status`, each dispatching to the backend for the current OS. Backends
return ``{"ok": bool, "detail": str}`` for the mutating calls.
"""

from __future__ import annotations

from pathlib import Path

from work_buddy.compat import IS_MACOS, IS_WINDOWS

# Stable identifiers, shared with notifications.service_hints and health checks.
TASK_NAME = "WB-Sidecar"               # Windows scheduled task
UNIT_NAME = "wb-sidecar"               # Linux systemd --user unit (wb-sidecar.service)
AGENT_LABEL = "com.workbuddy.sidecar"  # macOS launchd LaunchAgent


def _os_name() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    return "linux"


def _backend():
    if IS_WINDOWS:
        from work_buddy.autostart import windows as backend
    elif IS_MACOS:
        from work_buddy.autostart import macos as backend
    else:
        from work_buddy.autostart import linux as backend
    return backend


def register(
    *, python_exe: str | Path, home_dir: str | Path, data_dir: str | Path
) -> dict:
    """Register the sidecar to auto-start at login (idempotent, replaces existing).

    ``python_exe`` is the interpreter to launch under (the uv venv python).
    ``home_dir`` is the working copy (the sidecar's working directory).
    ``data_dir`` is the per-user mutable-state dir, passed to backends that carry
    it as an env var. The absolute ``paths.data_root`` in ``config.local.yaml`` is
    the primary way the sidecar resolves that dir; the env var is belt-and-suspenders
    for backends whose launcher supports it.
    """
    return _backend().register(
        python_exe=str(python_exe), home_dir=str(home_dir), data_dir=str(data_dir)
    )


def unregister() -> dict:
    """Remove the auto-start registration. Ok (no-op) when already absent."""
    return _backend().unregister()


def is_registered() -> bool:
    """True if the auto-start registration currently exists."""
    return _backend().is_registered()


def status() -> dict:
    """Return ``{"os": <name>, "registered": bool}``."""
    return {"os": _os_name(), "registered": _backend().is_registered()}
