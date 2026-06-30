"""Sidecar lifecycle helpers for the ``wb`` CLI: start (detached), stop, status.

Thin wrappers over the existing sidecar plumbing. These read the PID and state
files and spawn or terminate the daemon directly, with no MCP dependency, so
they work before the gateway is up. They return structured dicts and never
print, so the dispatch layer owns all rendering and the helpers stay testable.

Reused, not reimplemented:
- ``sidecar.pid.check_existing_daemon`` / ``takeover_existing_daemon`` for the
  single-instance check and termination (the daemon's own takeover-on-boot is
  what makes a fresh ``start`` safe).
- ``sidecar.state.load_state`` for the observability snapshot.
- ``compat.detached_process_kwargs`` for the no-console detached launch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from work_buddy import paths
from work_buddy.compat import detached_process_kwargs
from work_buddy.sidecar import pid as _pid
from work_buddy.sidecar import state as _state


def start_sidecar(*, foreground: bool = False, wait_seconds: float = 6.0) -> dict:
    """Start the sidecar.

    ``foreground=True`` runs the daemon inline in this process (blocking),
    equivalent to ``python -m work_buddy.sidecar --foreground``. Otherwise the
    daemon is spawned detached (no console window) and we wait up to
    ``wait_seconds`` for it to write its pid file.

    Idempotent: if a sidecar is already running, this reports it and does not
    spawn a second one (use ``restart`` to cycle a running sidecar).
    """
    if foreground:
        from work_buddy.sidecar.daemon import run as _run

        _run(foreground=True)
        return {
            "started": True,
            "already_running": False,
            "pid": None,
            "state": None,
            "detail": "Sidecar exited (foreground).",
        }

    existing = _pid.check_existing_daemon()
    if existing:
        return {
            "started": True,
            "already_running": True,
            "pid": existing,
            "state": _state.load_state(),
            "detail": "Sidecar already running.",
        }

    # Spawn the daemon with our own session id stripped so its __main__
    # self-assigns a ``sidecar-`` id. That id is the sidecar consent principal
    # and must be the daemon's own, never inherited (see sidecar/__main__.py).
    child_env = {k: v for k, v in os.environ.items() if k != "WORK_BUDDY_SESSION_ID"}
    # Detach stdio. With DETACHED_PROCESS the daemon has no console, so leaving
    # its std handles pointed at the launching shell makes it die when that
    # shell exits (a write to an invalid handle). The daemon logs to its own
    # files, so a null sink is correct here.
    subprocess.Popen(
        [sys.executable, "-m", "work_buddy.sidecar"],
        cwd=str(paths.repo_root()),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detached_process_kwargs(),
    )

    # Confirm via the PID file, which the daemon writes early in boot. The
    # state file is rewritten only on the daemon's first tick, so gating on
    # it would report a false failure for a sidecar that actually started.
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(0.3)
        new_pid = _pid.check_existing_daemon()
        if new_pid:
            return {
                "started": True,
                "already_running": False,
                "pid": new_pid,
                "state": _state.load_state(),
                "detail": "Sidecar started.",
            }

    return {
        "started": False,
        "already_running": False,
        "pid": _pid.check_existing_daemon(),
        "state": _state.load_state(),
        "detail": (
            f"Sidecar spawn issued but not confirmed within {wait_seconds:.0f}s. "
            "Check 'wb status'."
        ),
    }


def stop_sidecar() -> dict:
    """Terminate a running sidecar and its children.

    Uses the existing takeover path (SIGTERM, poll, escalate to force-kill),
    which also reaps the daemon's child services so they do not orphan.
    """
    existing = _pid.check_existing_daemon()
    if not existing:
        return {"stopped": False, "was_running": False, "pid": None,
                "detail": "Sidecar not running."}
    ok = _pid.takeover_existing_daemon(existing)
    return {
        "stopped": ok,
        "was_running": True,
        "pid": existing,
        "detail": (
            "Sidecar stopped."
            if ok
            else f"Could not terminate sidecar (pid={existing})."
        ),
    }


def sidecar_status() -> dict:
    """Return the current sidecar liveness + state snapshot (no side effects)."""
    pid = _pid.check_existing_daemon()
    return {"running": pid is not None, "pid": pid, "state": _state.load_state()}
