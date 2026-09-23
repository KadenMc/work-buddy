"""Sidecar state persistence — writes ``sidecar_state.json``.

The state file is the primary observability surface for the sidecar.
MCP skills, statusline scripts, and dashboards can read this
file to see what the sidecar is doing without querying it over HTTP.
"""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve

logger = get_logger(__name__)

STATE_FILE = resolve("runtime/sidecar-state")


@dataclass
class ServiceHealth:
    """Health snapshot for a supervised child service."""

    name: str
    port: int
    status: str = "stopped"  # stopped | starting | healthy | unhealthy | crashed
    pid: int | None = None
    last_check: float = 0.0  # epoch seconds
    crash_count: int = 0
    last_crash: float = 0.0


@dataclass
class JobState:
    """Scheduling state for a single job."""

    name: str
    schedule: str
    next_at: float = 0.0  # epoch seconds — raw cron eligibility (no jitter)
    last_run_at: float = 0.0
    last_result: str = ""  # ok | error | skipped
    last_error: str = ""  # human-readable error reason
    source: str = "system"  # "system" (sidecar_jobs/) | "user" (<data_root>/user_jobs/)
    enabled: bool = True
    # Jitter observability: 0 means no jitter applied. ``effective_at`` is
    # the timestamp the scheduler will actually fire at — equal to
    # ``next_at + stable_offset`` for non-pending jobs, or to the queued
    # pending due time when a fire is already deferred.
    jitter_seconds: int = 0
    effective_at: float = 0.0


@dataclass
class HostRecord:
    """The runtime context the daemon runs with, recorded once at boot.

    Written from ``work_buddy.process.describe_host_context``. ``console`` is
    ``allocated`` (the daemon created a console with no window), ``hidden``
    (it inherited one), ``attached`` (it runs in a terminal), ``none``, or
    ``not_applicable`` (POSIX). ``mechanism`` says how the console came
    about: ``inherited``, ``allocate_api``, ``relaunch``, ``foreground`` or
    ``none``. ``child_python`` and ``child_image`` are what the daemon's
    services run on.
    """

    role: str = ""
    executable: str = ""
    image: str = ""
    console: str = ""
    mechanism: str = ""
    child_python: str = ""
    child_image: str = ""
    pin: str | None = None
    pin_outside_project: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "HostRecord | None":
        """Rebuild a record, ignoring keys a newer daemon may have added."""
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def window_risks(self) -> list[str]:
        """Drift that makes console programs open visible windows."""
        risks = []
        if self.console == "none":
            risks.append(
                "the sidecar has no console, so console programs it starts "
                "without the no-window flag open windows"
            )
        if self.child_image == "gui":
            risks.append(
                "services would run on pythonw.exe, which has no console to "
                "pass to the programs they start"
            )
        return risks

    def warnings(self) -> list[str]:
        """Everything ``wbuddy status`` warns about."""
        warnings = self.window_risks()
        if self.pin_outside_project:
            warnings.append(
                f"sidecar.python_executable points outside the project ({self.pin}), "
                "so services may run different code than the sidecar"
            )
        return warnings


@dataclass
class SidecarState:
    """Top-level sidecar state written to ``sidecar_state.json``.

    Thread ownership: the daemon's supervisor thread owns ``services``,
    ``events``, ``last_tick_at``, and ``dispatch_job``. The dispatch
    thread owns ``jobs``, ``exclusion_active``, ``last_dispatch_at``,
    ``dispatch_phase``, and ``dispatch_phase_since``. Cross-thread
    visibility relies on each field being written by exactly one thread
    with atomic replacement (fresh lists, scalar assignments), never
    in-place mutation of a shared container.
    """

    started_at: float = 0.0
    pid: int = 0
    services: dict[str, ServiceHealth] = field(default_factory=dict)
    jobs: list[JobState] = field(default_factory=list)
    last_tick_at: float = 0.0
    exclusion_active: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    # Dispatch-loop observability. ``last_tick_at`` above proves the
    # supervisor is alive (it is the freshness signal ``wbuddy status``
    # gates on). These fields say what the dispatch loop is doing right
    # now (jobs, message dispatch, and retry sweeps execute inline
    # there and may block for minutes). ``dispatch_phase`` is one of
    # ``scheduler_tick | message_poll | retry_sweep | idle`` (empty
    # before the first cycle). ``dispatch_job`` names the job currently
    # executing inline in the scheduler, if any.
    last_dispatch_at: float = 0.0
    dispatch_phase: str = ""
    dispatch_phase_since: float = 0.0
    dispatch_job: str = ""
    # The daemon's runtime context, recorded once before its loops start.
    # ``None`` in state files written by a daemon that predates the record.
    host: HostRecord | None = None

    def update_service(self, name: str, **kwargs: Any) -> None:
        if name in self.services:
            for k, v in kwargs.items():
                setattr(self.services[name], k, v)

    def set_job_states(self, job_states: list[JobState]) -> None:
        self.jobs = job_states


def save_state(state: SidecarState, *, _retries: int = 4) -> None:
    """Atomically write the state to disk.

    On Windows, ``os.replace`` can fail with ``PermissionError`` when
    another process (antivirus, file indexer, dashboard reader) holds a
    handle on the target file.  We retry with a short back-off before
    giving up.
    """
    data = asdict(state)

    fd, tmp_path = tempfile.mkstemp(
        dir=STATE_FILE.parent, prefix=".sidecar_state_", suffix=".tmp"
    )
    try:
        os.write(fd, json.dumps(data, indent=2).encode())
        os.close(fd)
        fd = -1  # mark closed so the except branch doesn't double-close

        last_exc: Exception | None = None
        for attempt in range(_retries + 1):
            try:
                os.replace(tmp_path, STATE_FILE)
                return  # success
            except PermissionError as exc:
                last_exc = exc
                if attempt < _retries:
                    time.sleep(min(3.0, 0.15 * 3**attempt))  # 0.15, 0.45, 1.35, 3.0s

        # All retries exhausted — fall back to non-atomic overwrite so the
        # sidecar doesn't crash on a transient file lock.
        try:
            STATE_FILE.write_bytes(Path(tmp_path).read_bytes())
            os.unlink(tmp_path)
            logger.debug(
                "save_state: os.replace failed after %d retries, "
                "used non-atomic fallback",
                _retries,
            )
            return
        except Exception:
            pass  # if even the fallback fails, raise the original error

        # Clean up tmp and propagate
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise last_exc  # type: ignore[misc]

    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_READ_RETRY_DELAYS_S = (0.02, 0.05, 0.1, 0.2)


def _read_state_data() -> Any:
    """Read and parse the state file, riding out the daemon's own writes.

    The daemon replaces the file atomically, several times a minute. On
    Windows, a read that lands while the replacement is in flight fails with
    ``PermissionError``, and the non-atomic fallback in :func:`save_state` can
    briefly expose a partial file. Both clear within milliseconds, so a read
    retries them before giving up. A reader that gave up at once would see no
    state and could classify a healthy daemon as wedged.
    """
    for delay in (*_READ_RETRY_DELAYS_S, None):
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError):
            if delay is None:
                raise
            time.sleep(delay)
    return None  # pragma: no cover - the loop always returns or raises


def load_state() -> SidecarState | None:
    """Load state from disk, or return None if not present."""
    if not STATE_FILE.exists():
        return None
    try:
        data = _read_state_data()
        state = SidecarState(
            started_at=data.get("started_at", 0),
            pid=data.get("pid", 0),
            last_tick_at=data.get("last_tick_at", 0),
            exclusion_active=data.get("exclusion_active", False),
            last_dispatch_at=data.get("last_dispatch_at", 0.0),
            dispatch_phase=data.get("dispatch_phase", ""),
            dispatch_phase_since=data.get("dispatch_phase_since", 0.0),
            dispatch_job=data.get("dispatch_job", ""),
        )
        for name, svc in data.get("services", {}).items():
            state.services[name] = ServiceHealth(**svc)
        for j in data.get("jobs", []):
            state.jobs.append(JobState(**j))
        state.events = data.get("events", [])
        state.host = HostRecord.from_dict(data.get("host"))
        return state
    except FileNotFoundError:
        # Removed between the existence check and the read: the daemon
        # cleans the file up on shutdown. That is "no state", not an error.
        return None
    except Exception as exc:
        logger.warning("Failed to load sidecar state: %s", exc)
        return None


def cleanup_state_file() -> None:
    """Remove the state file on shutdown."""
    try:
        STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
