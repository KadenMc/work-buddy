"""Sidecar state persistence — writes ``sidecar_state.json``.

The state file is the primary observability surface for the sidecar.
MCP capabilities, statusline scripts, and dashboards can read this
file to see what the sidecar is doing without querying it over HTTP.
"""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
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
    next_at: float = 0.0  # epoch seconds
    last_run_at: float = 0.0
    last_result: str = ""  # ok | error | skipped
    last_error: str = ""  # human-readable error reason


@dataclass
class SidecarState:
    """Top-level sidecar state written to ``sidecar_state.json``."""

    started_at: float = 0.0
    pid: int = 0
    services: dict[str, ServiceHealth] = field(default_factory=dict)
    jobs: list[JobState] = field(default_factory=list)
    last_tick_at: float = 0.0
    exclusion_active: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

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


def load_state() -> SidecarState | None:
    """Load state from disk, or return None if not present."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        state = SidecarState(
            started_at=data.get("started_at", 0),
            pid=data.get("pid", 0),
            last_tick_at=data.get("last_tick_at", 0),
            exclusion_active=data.get("exclusion_active", False),
        )
        for name, svc in data.get("services", {}).items():
            state.services[name] = ServiceHealth(**svc)
        for j in data.get("jobs", []):
            state.jobs.append(JobState(**j))
        state.events = data.get("events", [])
        return state
    except Exception as exc:
        logger.warning("Failed to load sidecar state: %s", exc)
        return None


def cleanup_state_file() -> None:
    """Remove the state file on shutdown."""
    try:
        STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
