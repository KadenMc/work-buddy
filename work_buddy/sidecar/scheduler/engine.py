"""Scheduler engine — cron tick, heartbeat, and hot-reload.

Called by the daemon's main loop on every tick. The engine:
1. Checks if any cron jobs match the current minute
2. Fires matched jobs via the dispatch router
3. Periodically reloads config and job files (hot-reload)
4. Manages heartbeat timing

Adapted from ClaudeClaw's setInterval loops in start.ts, but
runs synchronously within the daemon's tick cycle rather than
as independent intervals.
"""

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.paths import data_dir
from work_buddy.sidecar.scheduler.cron import cron_matches, next_cron_match
from work_buddy.sidecar.scheduler.heartbeat import (
    ExclusionWindow,
    is_excluded,
    parse_exclusion_windows,
)
from work_buddy.sidecar.scheduler.jobs import (
    Job,
    clear_job_schedule,
    job_fingerprint,
    load_jobs_from_many,
)
from work_buddy.sidecar.state import JobState, SidecarState

logger = get_logger(__name__)


def _summarize_job_result(status: str, detail: Any) -> str:
    """Produce a compact one-line summary, expanding only on anomalies."""
    import json as _json

    # Parse JSON string results into dicts for structured summarization
    if isinstance(detail, str) and detail.strip().startswith("{"):
        try:
            detail = _json.loads(detail)
        except (ValueError, TypeError):
            pass

    if not detail:
        return status

    if not isinstance(detail, dict):
        s = str(detail)
        if len(s) > 80:
            return f"{status} — {s[:80]}…"
        return f"{status} — {s}"

    # --- Heartbeat / sidecar_status: summarize services ---
    services = detail.get("services")
    if isinstance(services, dict):
        total = len(services)
        unhealthy = [n for n, i in services.items()
                     if isinstance(i, dict) and i.get("status") != "healthy"]
        if unhealthy:
            return f"{status} — UNHEALTHY: {', '.join(unhealthy)}"
        return f"{status} — {total}/{total} healthy"

    # --- IR index rebuild ---
    if "docs_inserted" in detail or "items_discovered" in detail:
        inserted = detail.get("docs_inserted", 0)
        changed = detail.get("items_changed", 0)
        total = detail.get("docs_total", 0)
        if inserted == 0 and changed == 0:
            return f"{status} — nothing to index"
        parts = []
        if inserted:
            parts.append(f"{inserted} docs indexed")
        if changed:
            parts.append(f"{changed} changed")
        if total:
            parts.append(f"{total} total")
        return f"{status} — {', '.join(parts)}"

    # --- Task briefing / generic with a count ---
    if "count" in detail:
        return f"{status} — {detail['count']} items"

    # --- Fallback: status + compact key summary ---
    return status


class Scheduler:
    """Cron + heartbeat scheduler for the sidecar daemon.

    Not a standalone loop — the daemon calls ``tick()`` on each
    iteration of its main loop.
    """

    def __init__(self, config: dict[str, Any], event_log: Any | None = None) -> None:
        self._config = config
        self._timezone: str = config.get("timezone", "America/New_York")
        self._event_log = event_log

        self._jobs_dirs: list[tuple[Path, str]] = self._resolve_jobs_dirs(config)

        # Exclusion windows (quiet hours — no jobs fire during these periods)
        sidecar_cfg = config.get("sidecar", {})
        self._exclusion_windows: list[ExclusionWindow] = parse_exclusion_windows(
            sidecar_cfg.get("exclusion_windows", [])
        )

        # Job state
        self.jobs: list[Job] = []
        self._job_fingerprints: str = ""

        # Hot-reload tracking
        self._last_reload: float = 0.0
        self._reload_interval: float = 30.0  # seconds

        # Set by JobsWatcher (filesystem watcher) when a .md file changes
        # in any of self._jobs_dirs. The next tick checks this and calls
        # _hot_reload() immediately, bypassing the 30s interval. Cleared
        # after the reload runs. Doubles as a wake-up signal for the
        # daemon's main-loop sleep so a watcher event jumps the queue.
        self.jobs_reload_pending: threading.Event = threading.Event()

        # Deduplication: track which jobs fired this minute
        self._last_cron_minute: int = -1
        self._fired_this_minute: set[str] = set()

    @staticmethod
    def _resolve_jobs_dirs(config: dict[str, Any]) -> list[tuple[Path, str]]:
        """Resolve the (system, user) job directory pair from config.

        System dir: ``sidecar.jobs_dir`` relative to ``repos_root``'s parent,
        falling back to a repo-relative path so ``Scheduler`` works in
        development checkouts where ``repos_root`` is unset.

        User dir: ``sidecar.user_jobs_dir`` if set; otherwise
        ``<data_root>/user_jobs/`` (gitignored, configurable via
        ``paths.data_root``).
        """
        sidecar_cfg = config.get("sidecar", {})
        repos_parent = Path(config.get("repos_root", ".")).parent
        repo_root = Path(__file__).parent.parent.parent.parent

        # System dir
        system_dir = repos_parent / sidecar_cfg.get("jobs_dir", "sidecar_jobs")
        if not system_dir.is_dir():
            system_dir = repo_root / sidecar_cfg.get("jobs_dir", "sidecar_jobs")

        # User dir — explicit override wins; otherwise default under data_root
        user_override = sidecar_cfg.get("user_jobs_dir") or ""
        if user_override:
            user_path = Path(user_override)
            user_dir = user_path if user_path.is_absolute() else repo_root / user_path
        else:
            user_dir = data_dir("user_jobs")

        return [(system_dir, "system"), (user_dir, "user")]

    def start(self) -> None:
        """Initial load of jobs."""
        self.jobs = load_jobs_from_many(self._jobs_dirs)
        self._job_fingerprints = self._compute_fingerprints()

        logger.info(
            "Scheduler started: %d jobs loaded from %d source(s).",
            len(self.jobs), len(self._jobs_dirs),
        )

    def tick(self) -> None:
        """Called on every daemon tick. Checks cron, reload."""
        now = datetime.now(timezone.utc)

        # --- Exclusion window check ---
        if is_excluded(now, self._exclusion_windows, self._timezone):
            return

        # --- Hot-reload ---
        # Two triggers: the 30s polling interval (safety net) AND the
        # JobsWatcher's filesystem-event flag (instant on file change).
        # After reload the flag is cleared so the next tick doesn't
        # double-reload.
        if (
            self.jobs_reload_pending.is_set()
            or time.time() - self._last_reload >= self._reload_interval
        ):
            self._hot_reload()
            self.jobs_reload_pending.clear()

        # --- Cron tick ---
        current_minute = now.minute + now.hour * 60 + now.day * 1440
        if current_minute != self._last_cron_minute:
            self._last_cron_minute = current_minute
            self._fired_this_minute.clear()

        for job in self.jobs:
            if not job.enabled or not job.schedule:
                continue
            if job.name in self._fired_this_minute:
                continue
            if cron_matches(job.schedule, now, self._timezone):
                self._fire_job(job)
                self._fired_this_minute.add(job.name)

    def _fire_job(self, job: Job) -> None:
        """Execute a job via the dispatch router."""
        if self._event_log:
            self._event_log.emit(
                "job_fired", job.name, f"Firing {job.name}",
                detail=f"type={job.job_type}, schedule={job.schedule}",
            )

        try:
            from work_buddy.sidecar.dispatch.executor import execute_job

            result = execute_job(job)
            status = result.get("status", "unknown")
            detail = result.get("result", "")
            summary = _summarize_job_result(status, detail)
            job.last_run_at = time.time()
            job.last_result = status
            job.last_error = result.get("error", "") if status == "error" else ""
            if self._event_log:
                self._event_log.emit("job_completed", job.name, summary)
        except Exception as exc:
            job.last_run_at = time.time()
            job.last_result = "error"
            job.last_error = str(exc)
            if self._event_log:
                self._event_log.emit(
                    "job_failed", job.name, f"Failed: {exc}",
                    detail=f"Exception: {exc!r}", level="error",
                )

        # Clear schedule for one-shot jobs
        if not job.recurring:
            clear_job_schedule(job)
            job.schedule = ""  # Prevent re-firing before reload

    def _hot_reload(self) -> None:
        """Reload config and jobs from disk if changed."""
        self._last_reload = time.time()

        # Reload config first so a changed user_jobs_dir takes effect this tick
        from work_buddy.config import load_config

        cfg = load_config()
        sidecar_cfg = cfg.get("sidecar", {})

        new_windows = parse_exclusion_windows(sidecar_cfg.get("exclusion_windows", []))
        self._exclusion_windows = new_windows
        self._config = cfg
        self._timezone = cfg.get("timezone", "America/New_York")
        self._jobs_dirs = self._resolve_jobs_dirs(cfg)

        # Reload jobs from all configured directories
        new_jobs = load_jobs_from_many(self._jobs_dirs)
        new_fps = "\n".join(sorted(job_fingerprint(j) for j in new_jobs))

        if new_fps != self._job_fingerprints:
            old_count = len(self.jobs)
            # Carry forward runtime state from old jobs
            old_state = {j.name: (j.last_run_at, j.last_result, j.last_error) for j in self.jobs}
            for job in new_jobs:
                if job.name in old_state:
                    job.last_run_at, job.last_result, job.last_error = old_state[job.name]
            self.jobs = new_jobs
            self._job_fingerprints = new_fps
            if self._event_log:
                self._event_log.emit(
                    "hot_reload", "scheduler",
                    f"Jobs reloaded: {old_count} \u2192 {len(new_jobs)}",
                )
            # Publish to the dashboard event bus so the Jobs tab can refresh
            # without polling. publish_auto routes via the messaging-service
            # bridge from the sidecar process; failures are swallowed.
            try:
                from work_buddy.dashboard.events import publish_auto
                publish_auto("cron.hot_reload", {
                    "old_count": old_count,
                    "new_count": len(new_jobs),
                })
            except Exception:
                logger.debug("cron.hot_reload publish failed (non-fatal)", exc_info=True)

    def _compute_fingerprints(self) -> str:
        return "\n".join(sorted(job_fingerprint(j) for j in self.jobs))

    def update_state(self, state: SidecarState) -> None:
        """Push scheduler info into the shared SidecarState."""
        now = datetime.now(timezone.utc)

        job_states = []
        for job in self.jobs:
            next_at = 0.0
            if job.schedule:
                nxt = next_cron_match(job.schedule, now, self._timezone)
                if nxt:
                    next_at = nxt.timestamp()
            job_states.append(JobState(
                name=job.name,
                schedule=job.schedule,
                next_at=next_at,
                last_run_at=job.last_run_at,
                last_result=job.last_result,
                last_error=job.last_error,
                source=job.source,
            ))

        state.set_job_states(job_states)
        state.exclusion_active = is_excluded(
            now, self._exclusion_windows, self._timezone
        )
