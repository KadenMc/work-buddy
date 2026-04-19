"""Sidecar daemon — main event loop, process supervisor, shutdown handling.

Entry point: ``python -m work_buddy.sidecar``

The daemon:
1. Checks for an existing instance (PID file)
2. Starts supervised child services (messaging, embedding)
3. Runs the scheduler tick loop (cron + heartbeat + hot-reload)
4. Polls for incoming messages to auto-dispatch as jobs
5. Writes sidecar_state.json on every tick for observability
"""

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.sidecar.pid import (
    PID_FILE,
    check_existing_daemon,
    cleanup_pid_file,
    takeover_existing_daemon,
    write_pid_file,
)
from work_buddy.sidecar.state import (
    STATE_FILE,
    SidecarState,
    ServiceHealth,
    cleanup_state_file,
    save_state,
)

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent

# ---------------------------------------------------------------------------
# Child service management
# ---------------------------------------------------------------------------


@dataclass
class ChildService:
    """A supervised child process (messaging, embedding, etc.)."""

    name: str
    module: str  # e.g. "work_buddy.messaging.service"
    port: int
    args: list[str] = field(default_factory=list)  # extra CLI args
    enabled: bool = True
    process: subprocess.Popen | None = None
    crash_count: int = 0
    last_crash: float = 0.0
    last_healthy: float = 0.0
    backoff_until: float = 0.0  # don't restart before this time


def _health_check(port: int, timeout: float = 2.0) -> bool:
    """HTTP health check against localhost:<port>/health."""
    url = f"http://localhost:{port}/health"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("status") in ("ok", "loading")
    except (URLError, OSError, json.JSONDecodeError, Exception):
        return False


def _kill_process_on_port(port: int, *, service_name: str = "") -> bool:
    """Kill any process listening on the given port (cleanup from prior crash).

    Returns True when the port is confirmed free, False when an
    orphan survived our kill attempts — in which case the caller
    should NOT try to bind because the Popen will silently die.
    """
    from work_buddy.compat import kill_process_on_port
    freed = kill_process_on_port(port, wait_seconds=5.0)
    if not freed:
        logger.error(
            "Port %d still held after kill attempts — cannot safely "
            "start %s. An orphaned process from a prior sidecar run "
            "is likely holding the port. Check with: "
            "Get-NetTCPConnection -LocalPort %d (Windows) or "
            "lsof -i:%d (Unix), then kill the owner manually.",
            port, service_name or "service", port, port,
        )
    return freed


def _get_conda_python() -> str:
    """Resolve the conda env's Python interpreter path.

    The sidecar itself runs inside ``conda activate work-buddy``,
    so ``sys.executable`` is already the correct interpreter.
    """
    return sys.executable


def _start_child(svc: ChildService) -> None:
    """Start a child service as a direct subprocess.

    Launches the conda env's Python directly (no PowerShell wrapper)
    so that ``svc.process.pid`` is the actual Python process, not a
    wrapper. This ensures ``terminate()`` actually stops the service.
    """
    # Kill any orphan from a prior sidecar crash and verify the port
    # is actually free before we try to bind our fresh child. Without
    # this verify step the Popen silently dies when an orphan still
    # holds the port, and the sidecar logs "Started %s (pid=...)"
    # even though the child is already dead.
    if not _kill_process_on_port(svc.port, service_name=svc.name):
        logger.error(
            "Refusing to start %s — port %d not freed. Fix the orphan "
            "and retry the restart.", svc.name, svc.port,
        )
        return

    python = _get_conda_python()
    cmd = [python, "-m", svc.module] + svc.args
    try:
        svc.process = subprocess.Popen(
            cmd,
            # Inherit parent's stdout/stderr so child log lines appear in the
            # sidecar's live console output. CREATE_NO_WINDOW still suppresses
            # a separate console window on Windows.
            cwd=str(_REPO_ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        logger.info(
            "Started %s (pid=%d, port=%d)", svc.name, svc.process.pid, svc.port
        )
    except OSError as exc:
        logger.error("Failed to start %s: %s", svc.name, exc)


def _stop_child(svc: ChildService) -> None:
    """Terminate a child service gracefully, then forcefully."""
    if svc.process is None:
        return

    pid = svc.process.pid
    try:
        svc.process.terminate()
        try:
            svc.process.wait(timeout=5)
            logger.info("Stopped %s (pid=%d) gracefully.", svc.name, pid)
        except subprocess.TimeoutExpired:
            svc.process.kill()
            logger.warning("Force-killed %s (pid=%d).", svc.name, pid)
    except OSError:
        pass
    svc.process = None


_STARTUP_GRACE_SECONDS = 45  # give slow services time to load models


def _check_and_restart(
    svc: ChildService,
    state: SidecarState,
    max_crashes: int,
    backoff_base: float,
    event_log: Any | None = None,
) -> None:
    """Health-check a child service, restart if needed."""
    now = time.time()

    healthy = _health_check(svc.port)

    if healthy:
        svc.last_healthy = now
        # Reset crash count after 10 minutes of stability
        if svc.crash_count > 0 and (now - svc.last_crash) > 600:
            logger.info(
                "%s stable for 10min — resetting crash count (was %d).",
                svc.name, svc.crash_count,
            )
            svc.crash_count = 0
        state.update_service(
            svc.name, status="healthy", pid=svc.process.pid if svc.process else None,
            last_check=now, crash_count=svc.crash_count,
        )
        return

    # Not healthy — but if the process is alive and was recently
    # (re)started, give it a grace period to finish loading.
    if svc.process and svc.process.poll() is None:
        age = now - svc.last_crash if svc.last_crash else now - state.started_at
        if age < _STARTUP_GRACE_SECONDS:
            state.update_service(svc.name, status="starting", last_check=now)
            return  # Still booting — don't restart yet

    # Not healthy
    if svc.crash_count >= max_crashes:
        state.update_service(svc.name, status="crashed", last_check=now)
        if event_log:
            event_log.emit(
                "service_crashed", svc.name,
                f"{svc.name} crashed (max {max_crashes} restarts reached)",
                level="error",
            )
        return  # Give up — too many crashes

    if now < svc.backoff_until:
        state.update_service(svc.name, status="unhealthy", last_check=now)
        return  # In backoff period

    # Restart
    svc.crash_count += 1
    svc.last_crash = now
    # Exponential backoff: base * 2^(crashes-1), capped at 5 min
    svc.backoff_until = now + min(backoff_base * (2 ** (svc.crash_count - 1)), 300)

    logger.warning(
        "%s unhealthy — restarting (crash #%d, backoff %.0fs).",
        svc.name, svc.crash_count, svc.backoff_until - now,
    )
    if event_log:
        event_log.emit(
            "service_restart", svc.name,
            f"{svc.name} restarting (crash #{svc.crash_count})",
            level="warn",
        )
    _stop_child(svc)
    _start_child(svc)
    state.update_service(
        svc.name, status="starting",
        pid=svc.process.pid if svc.process else None,
        last_check=now, crash_count=svc.crash_count, last_crash=svc.last_crash,
    )


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _signal_handler(signum: int, _frame: Any) -> None:
    global _shutdown_requested
    logger.info("Received signal %d — shutting down.", signum)
    _shutdown_requested = True


def run(foreground: bool = True) -> None:
    """Main daemon entry point.

    Args:
        foreground: If True, run in the current process (blocking).
    """
    global _shutdown_requested

    cfg = load_config()
    sidecar_cfg = cfg.get("sidecar", {})

    # --- Check for existing daemon — if one's alive, take it over ---
    # We enforce single-instance by replacement, not refusal: the user
    # may be intentionally restarting in a visible terminal to regain
    # control of a sidecar launched silently at login.
    existing = check_existing_daemon()
    if existing:
        if not takeover_existing_daemon(existing):
            logger.error(
                "Sidecar already running (pid=%d) and could not be "
                "terminated. Aborting.", existing,
            )
            sys.exit(1)

    # --- Write PID file + register signal handlers ---
    write_pid_file()
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # --- Build child service list ---
    services_cfg = sidecar_cfg.get("services", {})
    children: list[ChildService] = []
    for name, svc_cfg in services_cfg.items():
        if not svc_cfg.get("enabled", True):
            continue
        children.append(ChildService(
            name=name,
            module=svc_cfg["module"],
            port=svc_cfg["port"],
            args=svc_cfg.get("args", []),
        ))

    # --- Initialize state ---
    state = SidecarState(
        started_at=time.time(),
        pid=os.getpid(),
    )
    for child in children:
        state.services[child.name] = ServiceHealth(
            name=child.name, port=child.port, status="starting",
        )

    # --- Start all children in parallel ---
    for child in children:
        _start_child(child)

    # Wait for all services to become healthy (parallel polling).
    # Each service gets up to 60s. We poll all of them each tick
    # rather than waiting for one at a time sequentially.
    logger.info("Waiting for services to become healthy...")
    deadline = time.time() + 60
    pending = set(c.name for c in children)
    while pending and time.time() < deadline:
        for child in children:
            if child.name not in pending:
                continue
            if _health_check(child.port):
                logger.info("%s is healthy.", child.name)
                child.last_healthy = time.time()
                state.update_service(child.name, status="healthy",
                                     pid=child.process.pid if child.process else None)
                pending.discard(child.name)
        if pending:
            time.sleep(2)
    for name in pending:
        logger.warning("%s did not become healthy within timeout.", name)
        state.update_service(name, status="unhealthy")

    # --- Initialize event log ---
    from work_buddy.sidecar.event_log import EventLog

    event_log = EventLog(max_size=200)

    # --- Initialize scheduler ---
    from work_buddy.sidecar.scheduler.engine import Scheduler

    scheduler = Scheduler(cfg, event_log=event_log)
    scheduler.start()

    # --- Initialize message poller ---
    from work_buddy.sidecar.dispatch.router import MessagePoller

    poller = MessagePoller(cfg)

    # --- Initialize retry sweep ---
    from work_buddy.sidecar.retry_sweep import RetrySweep

    retry_sweep = RetrySweep(config=cfg, event_log=event_log)

    # --- Save initial state ---
    state.last_tick_at = time.time()
    scheduler.update_state(state)
    save_state(state)

    health_interval = sidecar_cfg.get("health_check_interval", 30)
    max_crashes = sidecar_cfg.get("max_service_crashes", 5)
    backoff_base = sidecar_cfg.get("restart_backoff_base", 5)

    logger.info(
        "Sidecar daemon started (pid=%d, services=%d, jobs=%d).",
        os.getpid(), len(children), len(scheduler.jobs),
    )
    event_log.emit(
        "daemon_start", "daemon",
        f"Started (pid={os.getpid()}, {len(children)} services, {len(scheduler.jobs)} jobs)",
    )

    # --- Main loop ---
    try:
        while not _shutdown_requested:
            tick_start = time.time()

            try:
                # 1. Health-check children
                for child in children:
                    if child.enabled:
                        _check_and_restart(child, state, max_crashes, backoff_base, event_log)

                # 2. Scheduler tick (cron + heartbeat + hot-reload)
                scheduler.tick()
                scheduler.update_state(state)

                # 3. Message polling
                poller.poll()

                # 4. Retry sweep — process queued-for-retry operations
                try:
                    retry_sweep.sweep()
                except Exception as sweep_exc:
                    logger.error("Retry sweep error (non-fatal): %s", sweep_exc, exc_info=True)

            except Exception as exc:
                # Log but don't crash the daemon — individual tick failures
                # are recoverable. The next tick will retry.
                logger.error("Tick error (non-fatal): %s", exc, exc_info=True)

            # 5. Persist event log snapshot + write state
            state.events = event_log.recent(50)
            state.last_tick_at = time.time()
            save_state(state)

            # Sleep until next tick (target: health_interval seconds)
            elapsed = time.time() - tick_start
            sleep_time = max(1, health_interval - elapsed)
            _interruptible_sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down.")
    finally:
        event_log.emit("daemon_stop", "daemon", "Sidecar shutting down")
        state.events = event_log.recent(50)
        _shutdown(children, state)


def _interruptible_sleep(seconds: float) -> None:
    """Sleep in 1-second increments so signals can interrupt."""
    end = time.time() + seconds
    while time.time() < end and not _shutdown_requested:
        time.sleep(min(1.0, end - time.time()))


def _shutdown(children: list[ChildService], state: SidecarState) -> None:
    """Graceful shutdown: stop children, write final state, cleanup."""
    logger.info("Shutting down sidecar...")

    for child in children:
        _stop_child(child)

    # Write final state showing all services stopped
    for name in state.services:
        state.update_service(name, status="stopped", pid=None)
    try:
        save_state(state)
    except PermissionError as exc:
        # On Windows, os.replace / write can fail with WinError 5 when
        # another process holds a handle on the state file.  Not worth
        # crashing the shutdown sequence for a stale status file.
        state_path = str(STATE_FILE)
        if (
            (exc.filename and state_path in str(Path(exc.filename).resolve()))
            or (exc.filename2 and state_path in str(Path(exc.filename2).resolve()))
        ):
            logger.warning("Failed to write final state (non-fatal): %s", exc)
        else:
            raise

    cleanup_pid_file()
    # Don't remove state file — leave it for observability (shows "stopped")
    logger.info("Sidecar shutdown complete.")
