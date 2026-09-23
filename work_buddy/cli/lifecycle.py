"""Sidecar lifecycle helpers for the ``wbuddy`` CLI: start (detached), stop, status.

Thin wrappers over the existing sidecar plumbing. These read the PID and state
files and spawn or terminate the daemon directly, with no MCP dependency, so
they work before the gateway is up. They return structured dicts and never
print, so the dispatch layer owns all rendering and the helpers stay testable.

Reused, not reimplemented:
- ``sidecar.instance_lock`` for the single-instance question. It is the
  authority these verbs gate and verify on, because it asks the kernel rather
  than the filesystem: ``start`` refuses to spawn beside a held lock, and
  ``stop`` is not allowed to claim success while one is still held.
- ``sidecar.pid.check_existing_daemon`` / ``takeover_existing_daemon`` to learn
  *which* daemon is running and to terminate it.
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
from work_buddy.sidecar import instance_lock
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
# How long ``stop`` waits for the kernel to drop the dead daemon's instance
# lock before declaring the stop unverified. Handle teardown after termination
# is sub-second in practice; the margin is for a loaded machine.
_STOP_VERIFY_S = 5.0


def _pid_file_age_s() -> float:
    """Seconds since the sidecar pid file was last written; +inf if absent."""
    try:
        return time.time() - _pid.PID_FILE.stat().st_mtime
    except OSError:
        return float("inf")


def _wait_for_lock_release(timeout_s: float) -> bool:
    """Poll until the instance lock is free, or ``timeout_s`` elapses.

    The kernel releases the lock when the owner's handles close, which
    happens as part of process teardown rather than instantaneously with the
    terminate call. A bounded wait keeps ``stop`` from raising a false
    "still running" alarm on that tail, while still failing loudly if a daemon
    genuinely survives.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if not instance_lock.is_locked():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def _lock_age_s() -> float:
    """Seconds since the instance lock file was last touched; +inf if absent.

    The boot-grace analogue of :func:`_pid_file_age_s` for a daemon that holds
    the lock but has not yet written a PID file: the first few seconds of boot,
    where the lock exists and the PID file does not.
    """
    try:
        return time.time() - instance_lock.LOCK_FILE.stat().st_mtime
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
    verbs gate on this classification, not on pid-liveness: ``wbuddy start``
    declines to spawn beside a wedged daemon, and ``wbuddy restart`` replaces
    it.

    ``pid is None`` does not imply ``down``. The PID file can be missing or
    stale while a daemon runs, and reading that as "down" would make ``start``
    spawn a second daemon. The instance lock is the authority on liveness, so
    a daemon with no PID file classifies as ``booting`` (it may be
    pre-``write_pid_file``) or ``wedged``, never ``down``.
    """
    if pid is None:
        # Ask the kernel, not the filesystem. A held lock means a live daemon
        # regardless of what the PID file says or whether it exists at all.
        if not instance_lock.is_locked():
            return "down"
        return "booting" if _lock_age_s() <= _BOOT_GRACE_S else "wedged"
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
    ``wait_seconds`` for it to claim the instance lock and write its pid.

    Idempotent, and never a source of a second sidecar: if any daemon holds the
    instance lock (up, booting, or wedged), this reports it and does not
    spawn. Use ``restart`` (which stops the incumbent and verifies it stopped)
    to cycle or to replace a wedged daemon.
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

    # health is "wedged": a daemon holds the lock but has stopped ticking.
    # Spawning here would start a second daemon beside it, so ``start``
    # declines. Replacing a wedged daemon is ``restart``'s job, because it
    # stops the incumbent and verifies it stopped before launching a successor.
    if health == "wedged" and instance_lock.is_locked():
        return {
            "started": False,
            "already_running": True,
            "pid": existing,
            "state": _state.load_state(),
            "detail": (
                "A sidecar is running but no longer ticking (wedged). Refusing "
                "to start a second one. Run 'wbuddy restart' to replace it."
            ),
        }

    # Nothing holds the instance lock, so spawning is safe. The daemon claims
    # the lock as the first statement of its boot and exits if it loses, so
    # even if something else wins the race between here and there, the loser is
    # a short-lived process rather than a second sidecar.
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

    # The spawn did not confirm through the PID file. A held lock means a
    # sidecar is running, and since the lock was free when this function
    # decided to spawn, it is most likely the one just launched, still short of
    # a readable PID file. Report that as running: calling it a failure invites
    # the operator to launch again. A free lock means the launched daemon died
    # during boot, which is a genuine failure.
    if instance_lock.is_locked():
        owner = instance_lock.read_owner() or {}
        return {
            "started": True,
            "already_running": False,
            "pid": owner.get("pid"),
            "state": _state.load_state(),
            "detail": (
                "Sidecar is running (it holds the instance lock"
                + (f", pid={owner['pid']}" if owner.get("pid") else "")
                + ") but had not confirmed through its PID file within "
                f"{wait_seconds:.0f}s. Check 'wbuddy status'."
            ),
        }
    return {
        "started": False,
        "already_running": False,
        "pid": _pid.check_existing_daemon(),
        "state": _state.load_state(),
        "detail": (
            f"Sidecar spawn issued but not confirmed within {wait_seconds:.0f}s, "
            "and nothing holds the instance lock, so the daemon failed during "
            "boot. Check its log via 'wbuddy status'."
        ),
    }


def stop_sidecar() -> dict:
    """Stop work-buddy, then prove it stopped.

    Uses the existing takeover path (SIGTERM, poll, escalate to force-kill),
    which also reaps the daemon's child services so they do not orphan.

    The verification is the point. Terminating the pid that the PID file names
    proves only that that process ended. If the file named the wrong daemon,
    the one that matters is still running and will rebuild its services within
    seconds. So after the takeover, stop asks the kernel whether any daemon
    still holds the instance lock, and reports failure, naming the survivors,
    if one does. A stop that cannot verify itself is a status report, not a
    stop.
    """
    existing = _pid.check_existing_daemon()
    if not existing:
        # No usable PID record. That is NOT proof nothing is running, so
        # consult the lock before reporting "not running".
        if not instance_lock.is_locked():
            return {"stopped": False, "was_running": False, "pid": None,
                    "detail": "Sidecar not running."}
        owner = instance_lock.read_owner() or {}
        survivors = instance_lock.find_sidecar_processes()
        return {
            "stopped": False,
            "was_running": True,
            "pid": owner.get("pid"),
            "survivors": survivors,
            "detail": (
                "A sidecar holds the instance lock but the PID file does not "
                "identify it, so it cannot be stopped safely"
                + (f" (likely pid={owner['pid']})" if owner.get("pid") else "")
                + (f"; candidate processes: {survivors}" if survivors else "")
                + ". Terminate it by PID and report this, because the PID file "
                "never disagree with the lock."
            ),
        }

    result = _pid.takeover_existing_daemon(existing)

    # Verify against the kernel, not against the return value of the thing we
    # just asked to do the work.
    if not _wait_for_lock_release(_STOP_VERIFY_S):
        survivors = instance_lock.find_sidecar_processes()
        return {
            "stopped": False,
            "was_running": True,
            "pid": existing,
            "survivors": survivors,
            "detail": (
                f"Stop targeted pid={existing} ({result.outcome}) but a sidecar "
                "still holds the instance lock. Work-buddy is STILL RUNNING"
                + (f"; surviving processes: {survivors}" if survivors else "")
                + "."
            ),
        }

    if not result.ok:
        return {
            "stopped": False,
            "was_running": True,
            "pid": existing,
            "detail": f"Could not terminate sidecar (pid={existing}).",
        }

    return {
        "stopped": True,
        "was_running": True,
        "pid": existing,
        "detail": (
            "Sidecar stopped."
            if result.terminated
            else f"No sidecar was running ({result.outcome})."
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
    locked = instance_lock.is_locked()
    return {
        # A held lock is proof of a live daemon even when the PID file is
        # missing or names something unverifiable, so ``running`` must not be
        # a restatement of "the PID file parsed".
        "running": pid is not None or locked,
        "health": _daemon_health(pid, state),
        "pid": pid,
        "state": state,
        "instance_locked": locked,
        # Present only when the two disagree, which should be impossible and is
        # therefore worth surfacing rather than smoothing over.
        "pid_file": _pid.probe_pid_file() if locked and pid is None else None,
    }
