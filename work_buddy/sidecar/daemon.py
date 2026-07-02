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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from work_buddy.compat import (
    assign_process_to_job,
    build_child_env,
    create_kill_on_close_job,
    resolve_child_python,
)
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
from work_buddy import paths

logger = get_logger(__name__)

_REPO_ROOT = paths.repo_root()

# ---------------------------------------------------------------------------
# Child service management
# ---------------------------------------------------------------------------


def safe_port(value: Any, *, service_name: str) -> int | None:
    """Coerce a configured service port to a valid int, or None if invalid.

    A bad ``sidecar.services.<svc>.port`` (non-numeric or out of range)
    would otherwise crash service startup. Returning None lets the
    caller skip the misconfigured service while the daemon keeps running.
    """
    try:
        port = int(value)
    except (TypeError, ValueError):
        logger.error(
            "Service %r has a non-numeric port %r; skipping it.",
            service_name, value,
        )
        return None
    if not (1 <= port <= 65535):
        logger.error(
            "Service %r has out-of-range port %d; skipping it.",
            service_name, port,
        )
        return None
    return port


class TickFailureTracker:
    """Tracks consecutive daemon-tick failures and decides when to escalate.

    A tick that throws is caught as non-fatal and retried, so a persistent
    fault (e.g. an invalid config value) would otherwise fail silently
    forever — visible only as per-tick log noise. This surfaces a sustained
    run of failures exactly once (a loud log line + a sidecar event) and
    announces recovery exactly once.
    """

    def __init__(self, threshold: int = 3) -> None:
        self._threshold = threshold
        self._consecutive = 0
        self._escalated = False

    def record_success(self) -> str | None:
        """Note a successful tick. Returns a recovery message if the
        tracker had previously escalated, else None."""
        recovered = self._escalated
        self._consecutive = 0
        self._escalated = False
        if recovered:
            return "Daemon tick recovered after sustained failures."
        return None

    def record_failure(self, exc: BaseException) -> str | None:
        """Note a failed tick. Returns an escalation message the first
        time the failure run crosses the threshold, else None."""
        self._consecutive += 1
        if self._consecutive >= self._threshold and not self._escalated:
            self._escalated = True
            return (
                f"Daemon tick has failed {self._consecutive} consecutive "
                f"times: {exc}"
            )
        return None


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


class HealthMonitor:
    """Background thread probing service /health in parallel.

    Decouples probing from the main daemon loop so scheduler ticks,
    message polling, and retry sweeps cannot delay health checks. A
    single slow probe no longer holds up the rest: every service is
    probed concurrently in a ThreadPoolExecutor.

    The main loop reads cached state via ``snapshot(name)``. Restart
    decisions use ``consecutive_failures`` rather than a single probe
    result, so one late response does not trigger a false restart.
    """

    def __init__(self, children: list[ChildService], interval: float = 5.0,
                 probe_timeout: float = 2.0) -> None:
        self._children = children
        self._interval = interval
        self._probe_timeout = probe_timeout
        self._state: dict[str, dict[str, Any]] = {
            c.name: {"healthy": False, "consecutive_failures": 0, "last_check": 0.0}
            for c in children
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="health-monitor", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # Short timeout — the thread is a daemon, so if a probe is
            # genuinely wedged we exit the process anyway.
            self._thread.join(timeout=2)

    def snapshot(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._state[name])

    def reset(self, name: str) -> None:
        """Clear failure count — call after a deliberate restart so the
        service gets a fresh grace period before the next restart fires."""
        with self._lock:
            s = self._state.get(name)
            if s is not None:
                s["healthy"] = False
                s["consecutive_failures"] = 0
                s["last_check"] = time.time()

    def _run(self) -> None:
        workers = max(1, len(self._children))
        # Don't use ``with ThreadPoolExecutor(...)`` — its context-exit
        # waits for all pending tasks, which hangs shutdown if a probe
        # is blocked on a dead socket. We manage lifecycle explicitly
        # and cancel futures on stop.
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hm-probe")
        try:
            while not self._stop.is_set():
                futures = {
                    pool.submit(_health_check, c.port, self._probe_timeout): c.name
                    for c in self._children if c.enabled
                }
                # Bound the whole round. Even if every probe sits on its
                # 2s timeout, we won't wait longer than this.
                round_deadline = self._probe_timeout + 2.0
                results: dict[str, bool] = {}
                try:
                    for fut in as_completed(futures, timeout=round_deadline):
                        results[futures[fut]] = bool(fut.result())
                except TimeoutError:
                    # Any probe that didn't finish is implicitly a failure
                    pass
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("Health probe round error: %s", exc, exc_info=True)

                now = time.time()
                with self._lock:
                    for name in (futures[f] for f in futures):
                        s = self._state[name]
                        s["last_check"] = now
                        if results.get(name, False):
                            s["healthy"] = True
                            s["consecutive_failures"] = 0
                        else:
                            s["healthy"] = False
                            s["consecutive_failures"] += 1
                self._stop.wait(self._interval)
        finally:
            # Best-effort shutdown. ``cancel_futures=True`` (py3.9+)
            # drops queued work; running probes are left to time out
            # on their own urlopen deadline (~2s).
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # pragma: no cover - py<3.9
                pool.shutdown(wait=False)


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


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------
#
# When a human starts the sidecar in a terminal, the most useful thing
# we can hand them is the dashboard URL printed plainly on boot. Logger
# output is too noisy and goes to a file in non-foreground runs; this
# banner goes to stdout so it's visible regardless of log routing.

def _supports_color(stream: Any) -> bool:
    """Whether ANSI escape codes are likely to render on ``stream``.

    Three-way decision:
    - ``NO_COLOR`` env var set (any value) → no color (per no-color.org).
    - Stream not a TTY (piped, redirected, captured) → no color.
    - Otherwise → assume yes. Modern Windows Terminal / VS Code / WezTerm
      all handle ANSI; cmd.exe on Win10+ does too once any process in
      the session writes an escape sequence. Old cmd.exe will show
      garbage, but anyone running a Python sidecar on Windows is
      almost certainly on a modern terminal.
    """
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _print_startup_banner(
    children: list[ChildService], cfg: dict[str, Any],
) -> None:
    """Print a short banner with the dashboard URL(s).

    Always shows the local URL (for the machine the sidecar is running
    on). Additionally shows the remote URL when ``dashboard.external_url``
    is configured — that's the project's canonical place for the
    Tailscale-served HTTPS URL (e.g. ``https://<host>.<tailnet>.ts.net``),
    populated by the user or the setup wizard. Reading it from config
    rather than re-discovering it via ``tailscale serve status --json``
    keeps the banner consistent with telegram links and notification
    surfaces, which read the same field.

    No-ops cleanly when there's no dashboard service configured (or
    disabled) — printing a banner that points at nothing would be
    worse than silence.
    """
    dashboard = next(
        (c for c in children if c.name == "dashboard" and c.enabled),
        None,
    )
    if dashboard is None:
        return

    local_url = f"http://localhost:{dashboard.port}"
    remote_url = (cfg.get("dashboard", {}).get("external_url") or "").rstrip("/")
    use_color = _supports_color(sys.stdout)

    if use_color:
        BOLD = "\x1b[1m"
        DIM = "\x1b[2m"
        CYAN = "\x1b[36m"
        UNDERLINE = "\x1b[4m"
        RESET = "\x1b[0m"
    else:
        BOLD = DIM = CYAN = UNDERLINE = RESET = ""

    def fmt_link(url: str) -> str:
        return f"{CYAN}{UNDERLINE}{url}{RESET}"

    # Two-column-aligned, with ``Local``/``Remote`` labels when both are
    # present — labels collapse to "Dashboard:" when only the local URL
    # exists, since the distinction would be redundant.
    print()
    print(f"    {BOLD}Work Buddy sidecar is running{RESET}")
    if remote_url:
        print(f"    {DIM}Local: {RESET}  {fmt_link(local_url)}")
        print(f"    {DIM}Remote:{RESET}  {fmt_link(remote_url)}")
    else:
        print(f"    {DIM}Dashboard:{RESET}  {fmt_link(local_url)}")
    print()
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Service-log size cap
# ---------------------------------------------------------------------------
# Subprocess stdout/stderr lands in raw OS file handles (see _start_child),
# not Python's logging framework, so RotatingFileHandler can't bound their
# size — and because the child holds the handle open for its whole lifetime,
# the file can only be safely renamed at child *startup*, before that handle
# exists (on Windows, renaming a file with an open handle raises a sharing
# violation). So this module owns exactly one job: roll an oversized live
# log out of the way at startup so the child starts fresh.
#
# RETENTION of the rolled-out backups is NOT handled here. It is owned by the
# artifact-lifecycle reaper, which registers ``.data/runtime/service_logs/``
# as the ``service-logs`` artifact (see
# ``work_buddy/artifacts/default_registrations.py``) and deletes aged backups
# on its twice-daily sweep. Splitting the two concerns is deliberate: a roll
# that also managed a backup *count* would only ever weigh the live file at
# startup and never bound an already-rolled oversized backup, so a low-volume
# service's backup could persist indefinitely. Keeping this roller purely
# size-triggered and delegating age/retention to the reaper avoids that.
# Rolling to a timestamped name + age-reaping elsewhere mirrors logrotate's
# ``dateext`` + ``maxage`` model.

_SERVICE_LOG_CAP_BYTES = 16 * 1024 * 1024


def _roll_oversize_log(log_path: Path) -> Path | None:
    """Roll ``log_path`` aside if it exists and exceeds the size cap.

    Renames an oversized live log to a unique timestamped backup
    (``<stem>.<UTC-timestamp>.log``) in the same directory so the caller
    can open a fresh empty live log. No-op (returns ``None``) when the file
    is missing or at/below the cap. Returns the backup path when a roll
    happened. Idempotent; never deletes — retention is the reaper's job.
    """
    if not log_path.exists() or log_path.stat().st_size <= _SERVICE_LOG_CAP_BYTES:
        return None

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stem = log_path.stem  # e.g. "messaging" for "messaging.log"
    ts = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}"
    backup = log_path.with_name(f"{stem}.{ts}.log")
    # Microsecond resolution makes a same-name collision near-impossible,
    # but guard anyway: a crash-restart loop must never clobber a backup.
    counter = 0
    while backup.exists():
        counter += 1
        backup = log_path.with_name(f"{stem}.{ts}-{counter}.log")
    log_path.replace(backup)
    return backup


def _oversize_service_logs(log_dir: Path) -> list[tuple[Path, int]]:
    """Return ``(path, size_bytes)`` for every ``*.log`` in ``log_dir`` over the cap.

    Pure inspection helper used by the startup self-check (and tests). Reports
    both live and rolled files; callers decide what to warn about. Missing
    directory → empty list.
    """
    out: list[tuple[Path, int]] = []
    if not log_dir.is_dir():
        return out
    for f in log_dir.glob("*.log"):
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if size > _SERVICE_LOG_CAP_BYTES:
            out.append((f, size))
    return out


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

    python = resolve_child_python()
    # ``-u`` forces unbuffered stdio so child output lands in the log
    # file immediately — critical for debugging slow/silent startups.
    cmd = [python, "-u", "-m", svc.module] + svc.args

    # Redirect child stdout/stderr to a per-service log file. Previously
    # we relied on Popen's default (inherit from parent), but with
    # CREATE_NO_WINDOW on Windows that inheritance can silently drop
    # output — leaving us blind when a service fails to start.
    from work_buddy.paths import data_dir
    log_dir = data_dir("runtime/service_logs")
    log_path = log_dir / f"{svc.name}.log"
    rolled = _roll_oversize_log(log_path)
    if rolled is not None:
        logger.info(
            "Rolled oversized %s aside to %s before %s restart",
            log_path.name, rolled.name, svc.name,
        )
    # Startup self-check: surface any of this service's oversized logs still
    # on disk (e.g. a backup grown past the cap by mid-run writes) so an
    # unbounded-growth regression is visible immediately rather than only
    # caught silently by the reaper. The service-logs artifact deletes aged
    # backups on its twice-daily sweep.
    oversize = [
        (p, n) for p, n in _oversize_service_logs(log_dir)
        if p.name.startswith(f"{svc.name}.")
    ]
    if oversize:
        cap_mib = _SERVICE_LOG_CAP_BYTES // (1024 * 1024)
        logger.warning(
            "service_logs over %d MiB cap: %s — will be reaped by the "
            "service-logs artifact sweep",
            cap_mib,
            ", ".join(f"{p.name}={n // (1024 * 1024)}MiB" for p, n in oversize),
        )
    try:
        log_fh = open(log_path, "a", encoding="utf-8", buffering=1)
        log_fh.write(f"\n--- {svc.name} starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_fh.flush()
    except OSError as exc:
        logger.error("Could not open %s for child stdout: %s", log_path, exc)
        log_fh = None

    try:
        svc.process = subprocess.Popen(
            cmd,
            cwd=str(_REPO_ROOT),
            env=build_child_env(),
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        logger.info(
            "Started %s (pid=%d, port=%d, log=%s)",
            svc.name, svc.process.pid, svc.port, log_path.name,
        )
        # Attach to the kill-on-close Job Object so the OS reaps this child
        # if the daemon is hard-killed. Best-effort: a failed assignment is
        # non-fatal (the next-startup orphan sweep is the fallback) and a
        # no-op on non-Windows / when the job couldn't be created.
        assign_process_to_job(_kill_job, svc.process.pid)
    except OSError as exc:
        logger.error("Failed to start %s: %s", svc.name, exc)
    finally:
        # Close the daemon's copy of the child's log handle. The child holds
        # its own inherited dup, so this does not affect its logging; leaving
        # the parent copy open leaks a handle per restart and, on Windows,
        # pins the file against the next startup roll's rename.
        if log_fh:
            log_fh.close()


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
    monitor: HealthMonitor,
    state: SidecarState,
    max_crashes: int,
    backoff_base: float,
    failure_threshold: int,
    event_log: Any | None = None,
) -> None:
    """Consume cached health state, restart if the service has failed
    ``failure_threshold`` consecutive probes."""
    now = time.time()
    snap = monitor.snapshot(svc.name)
    healthy = snap["healthy"]
    failures = snap["consecutive_failures"]

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

    # Require N consecutive failed probes before restarting. One late
    # probe (or a brief event-loop stall in the service) should not
    # trigger a restart cascade.
    if failures < failure_threshold:
        state.update_service(svc.name, status="unhealthy", last_check=now)
        return

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
        "%s unhealthy (%d consecutive failed probes) — restarting "
        "(crash #%d, backoff %.0fs).",
        svc.name, failures, svc.crash_count, svc.backoff_until - now,
    )
    if event_log:
        event_log.emit(
            "service_restart", svc.name,
            f"{svc.name} restarting (crash #{svc.crash_count})",
            level="warn",
        )
    _stop_child(svc)
    _start_child(svc)
    # Clear the monitor's cached state so the fresh process gets a
    # clean slate for the next restart-threshold evaluation.
    monitor.reset(svc.name)
    state.update_service(
        svc.name, status="starting",
        pid=svc.process.pid if svc.process else None,
        last_check=now, crash_count=svc.crash_count, last_crash=svc.last_crash,
    )


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

_shutdown_requested = False
_shutdown_signal_count = 0
_shutdown_requested_at: float = 0.0  # set when _shutdown_requested flips to True
_force_kill_children: list[Any] = []  # populated in run() so the signal handler can reach them
_SHUTDOWN_WATCHDOG_TIMEOUT = 15.0  # seconds: if graceful shutdown exceeds this, force-exit
# Windows Job Object (KILL_ON_JOB_CLOSE) created in run(); every child is
# assigned to it so the OS reaps them when this daemon dies by ANY means —
# including a hard kill where no signal handler / atexit hook runs. Kept as
# a module global so the handle outlives the daemon loop (the kill fires
# when the job's last handle closes). None on non-Windows / creation failure.
_kill_job: Any = None


def _shutdown_watchdog() -> None:
    """Background thread: force-exit if graceful shutdown stalls.

    Python on Windows only delivers SIGINT to the main thread at
    bytecode boundaries. If the main thread is blocked in a C call
    (urllib, socket ops, subprocess.wait), Ctrl+C is queued until
    the call returns — which can feel like Ctrl+C is ignored.
    This watchdog runs in its own thread, not blocked by any of
    that, and hard-exits the process once a shutdown has been
    requested for longer than the timeout.
    """
    while True:
        time.sleep(0.5)
        if _shutdown_requested and _shutdown_requested_at > 0:
            elapsed = time.time() - _shutdown_requested_at
            if elapsed >= _SHUTDOWN_WATCHDOG_TIMEOUT:
                logger.warning(
                    "Graceful shutdown exceeded %.0fs — watchdog forcing exit.",
                    _SHUTDOWN_WATCHDOG_TIMEOUT,
                )
                for svc in _force_kill_children:
                    proc = getattr(svc, "process", None)
                    if proc is not None and proc.poll() is None:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                os._exit(130)


def _signal_handler(signum: int, _frame: Any) -> None:
    """First signal: request graceful shutdown. Second signal: force-kill
    children immediately, then exit.

    Critical: a second Ctrl+C must NOT call ``os._exit`` without first
    reaping children — on Windows that leaves orphaned Flask processes
    holding ports 5123-5127, and Windows TIME_WAIT can block the next
    sidecar from re-binding for 1-2 minutes.
    """
    global _shutdown_requested, _shutdown_signal_count, _shutdown_requested_at
    _shutdown_signal_count += 1
    if _shutdown_signal_count >= 2:
        logger.warning(
            "Received signal %d twice — force-killing children and exiting.",
            signum,
        )
        for svc in _force_kill_children:
            proc = getattr(svc, "process", None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()  # SIGKILL-equivalent — no graceful wait
                except OSError:
                    pass
        os._exit(130)
    logger.info(
        "Received signal %d — shutting down (press Ctrl+C again to force, "
        "or wait %.0fs for watchdog).",
        signum, _SHUTDOWN_WATCHDOG_TIMEOUT,
    )
    _shutdown_requested = True
    if _shutdown_requested_at == 0.0:
        _shutdown_requested_at = time.time()


def run(foreground: bool = True) -> None:
    """Main daemon entry point.

    Args:
        foreground: If True, run in the current process (blocking).
    """
    global _shutdown_requested

    cfg = load_config()
    sidecar_cfg = cfg.get("sidecar", {})

    # --- Threads-FSM bootstrap ---
    # Wires the FSM engine state-entry handlers + LLM-call queue
    # admission hook. Idempotent and self-contained. Failure here
    # is non-fatal — the threads system simply won't process Threads
    # but the rest of the sidecar (retry queue, scheduled jobs,
    # conductor) is unaffected.
    from work_buddy.threads.bootstrap import bootstrap_for_subprocess
    bootstrap_for_subprocess(subprocess_name="sidecar")

    # --- Inference-worker poller ---
    # Without this, queue.enqueue() during AWAITING_INFERENCE entry
    # would just pile up entries with nothing draining them. The
    # poller pulls one entry per cycle and runs inference inline
    # in this background thread.
    try:
        from work_buddy.threads import inference_worker

        def _inference_poller_loop():
            try:
                inference_worker.run_poller(
                    worker_id=f"sidecar-{os.getpid()}",
                    max_iterations=None,  # forever
                    poll_interval_s=5.0,
                )
            except Exception as e:
                logger.warning("inference poller crashed: %s", e)

        threading.Thread(
            target=_inference_poller_loop,
            name="inference-poller",
            daemon=True,
        ).start()
        logger.info("inference poller started (5s interval)")
    except Exception as e:
        logger.warning(
            "inference poller could not start; queued inference "
            "requests will pile up untouched: %s", e,
        )

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

    # Watchdog ensures Ctrl+C always wins within a bounded time, even
    # if the main thread is stuck in a blocking syscall when the
    # signal arrives.
    threading.Thread(
        target=_shutdown_watchdog, name="shutdown-watchdog", daemon=True,
    ).start()

    # --- Build child service list ---
    services_cfg = sidecar_cfg.get("services", {})
    children: list[ChildService] = []
    for name, svc_cfg in services_cfg.items():
        if not svc_cfg.get("enabled", True):
            continue
        port = safe_port(svc_cfg.get("port"), service_name=name)
        if port is None:
            continue
        children.append(ChildService(
            name=name,
            module=svc_cfg["module"],
            port=port,
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

    # Make children reachable from the signal handler so a force-kill
    # path can reap them synchronously (prevents orphan Flask processes
    # holding ports, which would block the next sidecar run on Windows).
    _force_kill_children.clear()
    _force_kill_children.extend(children)

    # Surface the resolved interpreter at boot so a wrong-env startup is
    # visible immediately — children will spawn under this Python, not
    # necessarily the daemon's own. The most common failure mode this
    # log catches is a Windows scheduled task whose ``conda activate``
    # silently no-op'd, leaving the daemon and all its children on the
    # base interpreter (and serving stale code) for days.
    resolved_python = resolve_child_python(cfg)
    logger.info(
        "Children will spawn with: %s (daemon sys.executable=%s)",
        resolved_python, sys.executable,
    )

    # --- OS-enforced hard-kill reaping (Windows) ---
    # Create the kill-on-close Job Object before spawning any child so each
    # child can be assigned to it the instant it starts. This is the only
    # layer that survives a hard kill of the daemon (taskkill /F, crash):
    # all the signal-handler / watchdog / takeover-sweep layers only run if
    # the dying parent's own code runs. No-op on non-Windows.
    global _kill_job
    _kill_job = create_kill_on_close_job()

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

    # --- Reconcile the sidecar's own consent session (orphan sweep) ---
    # The sidecar process runs under a synthetic ``sidecar-<hex>`` session.
    # reconcile_workflow_consent revokes any ``workflow_run:*`` grant in that
    # session's consent.db with no matching in-flight run; at boot _ACTIVE_RUNS
    # is empty, so every orphan left by a previously hard-killed run is swept.
    # New headless runs use isolated per-run sessions (``sidecar-run-*``), so
    # this never touches a live run. Must run before the scheduler ticks.
    # Guarded — never blocks boot.
    try:
        from work_buddy.consent_principal import sidecar_self
        from work_buddy.mcp_server.conductor import reconcile_workflow_consent
        _recon = reconcile_workflow_consent(sidecar_self().session_id)
        logger.info("Sidecar consent reconcile at boot: %s", _recon)
    except Exception as _recon_exc:
        logger.warning(
            "Sidecar boot consent reconcile failed (non-fatal): %s",
            _recon_exc,
        )

    # --- Initialize scheduler ---
    from work_buddy.sidecar.scheduler.engine import Scheduler

    scheduler = Scheduler(cfg, event_log=event_log)
    scheduler.start()

    # --- Initialize jobs filesystem watcher ---
    # Sets scheduler.jobs_reload_pending the moment a .md file lands in
    # any of the jobs directories. The main-loop sleep below waits on
    # that event so reload latency drops from ~30s (poll interval) to
    # ~50ms (kernel filesystem event). The 30s poll stays as a fallback.
    from work_buddy.sidecar.scheduler.watcher import JobsWatcher

    jobs_watcher = JobsWatcher(scheduler)
    jobs_watcher.start()

    # --- Events backbone: register the demo consumer + start the
    # single event-drain thread + the thin cron producer. All best-effort —
    # a backbone failure must never take down the rest of the sidecar.
    event_drain = None
    _emit_event_tick = None
    try:
        from work_buddy.events.consumers.notify_demo import register_notify_demo
        from work_buddy.events.consumers.source_action import register_source_action
        from work_buddy.events.drain import EventDrain
        from work_buddy.events.producers.cron import emit_schedule_tick

        register_notify_demo()
        register_source_action()
        event_drain = EventDrain()
        event_drain.start()
        _emit_event_tick = emit_schedule_tick
        logger.info(
            "events backbone started (drain + notify-demo + source-action + cron adapter)"
        )
    except Exception as _events_exc:  # pragma: no cover — defensive
        logger.warning(
            "events backbone failed to start (non-fatal): %s",
            _events_exc, exc_info=True,
        )

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
    failure_threshold = sidecar_cfg.get("health_failure_threshold", 2)
    probe_interval = sidecar_cfg.get("health_probe_interval", 5)
    probe_timeout = sidecar_cfg.get("health_probe_timeout", 2)

    # Parallel background prober. Probes run off-loop so scheduler
    # ticks, message polling, and retry sweeps can never delay a
    # health check. The main loop consumes cached results only.
    monitor = HealthMonitor(
        children, interval=probe_interval, probe_timeout=probe_timeout,
    )
    monitor.start()

    logger.info(
        "Sidecar daemon started (pid=%d, services=%d, jobs=%d).",
        os.getpid(), len(children), len(scheduler.jobs),
    )
    event_log.emit(
        "daemon_start", "daemon",
        f"Started (pid={os.getpid()}, {len(children)} services, {len(scheduler.jobs)} jobs)",
    )
    _print_startup_banner(children, cfg)

    # --- Main loop ---
    tick_failures = TickFailureTracker()
    try:
        while not _shutdown_requested:
            tick_start = time.time()

            try:
                # 1. Evaluate cached health state and restart if warranted
                for child in children:
                    if child.enabled:
                        _check_and_restart(
                            child, monitor, state, max_crashes, backoff_base,
                            failure_threshold, event_log,
                        )

                # 2. Scheduler tick (cron + heartbeat + hot-reload)
                scheduler.tick()
                scheduler.update_state(state)

                # 2b. Events backbone — thin CronAdapter emits a (throttled)
                # schedule.tick event onto the spine. Additive; never raises up.
                if _emit_event_tick is not None:
                    try:
                        _emit_event_tick(scheduler)
                    except Exception:
                        logger.debug(
                            "cron event adapter failed (non-fatal)", exc_info=True
                        )

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
                escalation = tick_failures.record_failure(exc)
                if escalation:
                    # Sustained failure — make it loud instead of silent.
                    logger.critical("%s", escalation)
                    event_log.emit(
                        "tick_failures", "daemon", escalation, level="error",
                    )
            else:
                recovery = tick_failures.record_success()
                if recovery:
                    logger.info("%s", recovery)
                    event_log.emit("tick_recovered", "daemon", recovery)

            # 5. Persist event log snapshot + write state
            state.events = event_log.recent(50)
            state.last_tick_at = time.time()
            save_state(state)

            # Sleep until next tick (target: health_interval seconds).
            # Wakes early on a JobsWatcher filesystem event so the next
            # tick reloads jobs immediately instead of waiting out the
            # full interval.
            elapsed = time.time() - tick_start
            sleep_time = max(1.0, health_interval - elapsed)
            _interruptible_sleep(sleep_time, waker=scheduler.jobs_reload_pending)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down.")
    finally:
        jobs_watcher.stop()
        if event_drain is not None:
            event_drain.stop()
        monitor.stop()
        event_log.emit("daemon_stop", "daemon", "Sidecar shutting down")
        state.events = event_log.recent(50)
        _shutdown(children, state)


def _interruptible_sleep(
    seconds: float,
    *,
    waker: "threading.Event | None" = None,
) -> None:
    """Sleep up to ``seconds``, wakeable by signals or a ``waker`` event.

    The shutdown flag (``_shutdown_requested``) is checked in 1-second
    increments so a SIGINT / SIGTERM stops the sleep within at most a
    second. When ``waker`` is provided, the sleep also wakes the moment
    the event is set — used by the main loop to react to filesystem
    events from ``JobsWatcher`` without waiting out the full tick
    interval. Either way, the function returns once the deadline is
    reached, the shutdown flag flips, or the waker fires.
    """
    end = time.time() + seconds
    while time.time() < end and not _shutdown_requested:
        remaining = end - time.time()
        if remaining <= 0:
            break
        chunk = min(1.0, remaining)
        if waker is not None:
            # ``Event.wait`` returns True iff the event was set during
            # the wait; True means "exit early", caller's next tick
            # will see the flag and act on it.
            if waker.wait(timeout=chunk):
                return
        else:
            time.sleep(chunk)


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
