"""Sidecar lifecycle helpers for the ``wbuddy`` CLI: start (detached), stop, status.

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

# A healthy daemon rewrites the state file every health-check interval (~30s by
# default), stamping its own pid and last_tick_at. The supervisor loop that
# writes it runs no jobs (job execution lives on the daemon's dispatch thread),
# so three missed ticks means the daemon itself has stopped supervising.
# Treat that as wedged rather than merely busy.
_STALE_TICK_S = 90.0
# The daemon writes its pid file early in boot but does not publish state until
# its services are healthy and it takes its first tick (which can be ~60s out).
# Within this window of the pid file being (re)written, a not-yet-publishing
# daemon is presumed to be booting rather than wedged, so `wbuddy start` waits for
# it instead of racing the daemon's own takeover against itself.
_BOOT_GRACE_S = 90.0
# Only classify a busy dispatch phase as noteworthy once it has run long enough
# (a cycle legitimately spends seconds in each phase). Shared by the CLI status
# renderer and the tray so the threshold lives once.
DISPATCH_BUSY_DISPLAY_S = 120.0


def _pid_file_age_s() -> float:
    """Seconds since the sidecar pid file was last written; +inf if absent."""
    try:
        return time.time() - _pid.PID_FILE.stat().st_mtime
    except OSError:
        return float("inf")


def _daemon_health(pid: int | None, state) -> str:
    """Classify the daemon behind the pid file.

    Returns one of:

    - ``"up"``: alive and publishing fresh state (safe to leave running).
    - ``"booting"``: alive, not yet publishing, pid file written recently.
    - ``"wedged"``: alive, not publishing, pid file is old (it stopped ticking).
    - ``"down"``: no live daemon.

    A live pid is NOT proof of a working sidecar: a daemon can hold its pid file
    while hung, so its children never come up and it never ticks. The lifecycle
    verbs gate on this classification, not on pid-liveness, so ``wbuddy start`` takes
    over a wedged daemon (matching the daemon's own single-instance-by-
    replacement boot, daemon.py) instead of refusing to start.
    """
    if pid is None:
        return "down"
    if state is not None and state.pid == pid:
        last = state.last_tick_at or state.started_at
        if last and (time.time() - last) <= _STALE_TICK_S:
            return "up"
    return "booting" if _pid_file_age_s() <= _BOOT_GRACE_S else "wedged"


# Public name for external consumers (the tray reads status through this).
# The underscore original stays for the existing internal callers.
daemon_health = _daemon_health


def dispatch_busy(state) -> dict | None:
    """Classify the daemon's dispatch loop as noteworthy-busy.

    Returns ``{"phase", "job", "busy_for_s"}`` once a non-idle phase has run
    past ``DISPATCH_BUSY_DISPLAY_S``, else ``None`` (including for state files
    written by a daemon without dispatch fields). Jobs, message dispatch, and
    retry sweeps run inline on the dispatch thread and may legitimately block
    for minutes while the supervisor keeps ticking, so busy is an overlay on a
    healthy daemon, not a fifth health state.
    """
    if state is None or not getattr(state, "dispatch_phase", None):
        return None
    if state.dispatch_phase == "idle":
        return None
    busy_for = (
        (time.time() - state.dispatch_phase_since)
        if state.dispatch_phase_since
        else 0.0
    )
    if busy_for < DISPATCH_BUSY_DISPLAY_S:
        return None
    return {
        "phase": state.dispatch_phase,
        "job": state.dispatch_job,
        "busy_for_s": busy_for,
    }


def start_sidecar(*, foreground: bool = False, wait_seconds: float = 15.0) -> dict:
    """Start the sidecar.

    ``foreground=True`` runs the daemon inline in this process (blocking),
    equivalent to ``python -m work_buddy.sidecar --foreground``. Otherwise the
    daemon is spawned detached (no console window) and we wait up to
    ``wait_seconds`` for it to take over any stale pid and write its own.

    Idempotent only for a *healthy* sidecar: if a daemon is up (publishing fresh
    state) or still booting, this reports it and does not spawn a second one. If
    the pid file names a wedged daemon (alive but no longer ticking), this spawns
    a fresh daemon, whose boot pipeline takes the wedged process over. Use
    ``restart`` to cycle a healthy sidecar.
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
    health = _daemon_health(existing, _state.load_state())
    if health in ("up", "booting"):
        return {
            "started": True,
            "already_running": True,
            "pid": existing,
            "state": _state.load_state(),
            "detail": (
                "Sidecar already running" if health == "up" else "Sidecar starting up"
            ),
        }

    # health is "down" (nothing running) or "wedged" (a hung daemon holds the
    # pid file). Spawn the daemon. Its boot pipeline takes over any existing pid
    # (single-instance by replacement, daemon.py) and starts fresh. Gating on
    # health, not bare pid-liveness, is what lets start recover a wedged daemon:
    # an alive pid whose services are dead is not a working sidecar to defer to.
    #
    # Strip our own session id so the daemon's __main__ self-assigns a
    # ``sidecar-`` id. That id is the sidecar consent principal and must be the
    # daemon's own, never inherited (see sidecar/__main__.py).
    child_env = {k: v for k, v in os.environ.items() if k != "WORK_BUDDY_SESSION_ID"}
    # Detach stdio to a null sink. The daemon runs windowless with its own
    # hidden console (see detached_process_kwargs) and logs to its own files, so
    # it has no use for the launching shell's std handles. Pointing them at
    # DEVNULL keeps it independent of that shell's lifetime.
    subprocess.Popen(
        [sys.executable, "-m", "work_buddy.sidecar"],
        cwd=str(paths.repo_root()),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detached_process_kwargs(),
    )

    # Confirm on the pid file. The daemon writes it early in boot, but only
    # after taking over any prior pid, so we wait for a pid that is both live
    # AND different from the wedged one we are replacing (``existing``).
    # Otherwise we could latch onto the dying zombie's pid mid-takeover. Gating
    # on the state file instead would report a false failure, since the daemon
    # publishes state only on its first tick, up to ~60s later.
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(0.3)
        new_pid = _pid.check_existing_daemon()
        if new_pid and new_pid != existing:
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
            "Check 'wbuddy status'."
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
    """Return the sidecar liveness classification + state snapshot (no side effects).

    ``health`` is one of ``down`` | ``booting`` | ``wedged`` | ``up`` (see
    ``_daemon_health``). ``running`` stays True whenever a pid is alive, so
    callers that only care about process liveness are unaffected, but a wedged
    daemon is now distinguishable from a healthy one.
    """
    pid = _pid.check_existing_daemon()
    state = _state.load_state()
    return {
        "running": pid is not None,
        "health": _daemon_health(pid, state),
        "pid": pid,
        "state": state,
    }
