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

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
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
    load_jobs,
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

        sidecar_cfg = config.get("sidecar", {})
        self._jobs_dir = Path(config.get("repos_root", ".")).parent / sidecar_cfg.get(
            "jobs_dir", "sidecar_jobs"
        )
        # Fallback: try repo-relative path
        repo_root = Path(__file__).parent.parent.parent.parent
        if not self._jobs_dir.is_dir():
            self._jobs_dir = repo_root / sidecar_cfg.get("jobs_dir", "sidecar_jobs")

        # Exclusion windows (quiet hours — no jobs fire during these periods)
        self._exclusion_windows: list[ExclusionWindow] = parse_exclusion_windows(
            sidecar_cfg.get("exclusion_windows", [])
        )

        # Job state
        self.jobs: list[Job] = []
        self._job_fingerprints: str = ""

        # Hot-reload tracking
        self._last_reload: float = 0.0
        self._reload_interval: float = 30.0  # seconds

        # Deduplication: track which jobs fired this minute
        self._last_cron_minute: int = -1
        self._fired_this_minute: set[str] = set()

    def start(self) -> None:
        """Initial load of jobs."""
        self.jobs = load_jobs(self._jobs_dir)
        self._job_fingerprints = self._compute_fingerprints()

        logger.info("Scheduler started: %d jobs loaded.", len(self.jobs))

    def tick(self) -> None:
        """Called on every daemon tick. Checks cron, reload."""
        now = datetime.now(timezone.utc)

        # --- Exclusion window check ---
        if is_excluded(now, self._exclusion_windows, self._timezone):
            return

        # --- Hot-reload (every 30s) ---
        if time.time() - self._last_reload >= self._reload_interval:
            self._hot_reload()

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
            if self._event_log:
                self._event_log.emit("job_completed", job.name, summary)
        except Exception as exc:
            job.last_run_at = time.time()
            job.last_result = "error"
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

        # Reload jobs
        new_jobs = load_jobs(self._jobs_dir)
        new_fps = "\n".join(sorted(job_fingerprint(j) for j in new_jobs))

        if new_fps != self._job_fingerprints:
            old_count = len(self.jobs)
            # Carry forward runtime state from old jobs
            old_state = {j.name: (j.last_run_at, j.last_result) for j in self.jobs}
            for job in new_jobs:
                if job.name in old_state:
                    job.last_run_at, job.last_result = old_state[job.name]
            self.jobs = new_jobs
            self._job_fingerprints = new_fps
            if self._event_log:
                self._event_log.emit(
                    "hot_reload", "scheduler",
                    f"Jobs reloaded: {old_count} \u2192 {len(new_jobs)}",
                )

        # Reload config (timezone, exclusion windows)
        from work_buddy.config import load_config

        cfg = load_config()
        sidecar_cfg = cfg.get("sidecar", {})

        new_windows = parse_exclusion_windows(sidecar_cfg.get("exclusion_windows", []))
        self._exclusion_windows = new_windows
        self._config = cfg
        self._timezone = cfg.get("timezone", "America/New_York")

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
            ))

        state.set_job_states(job_states)
        state.exclusion_active = is_excluded(
            now, self._exclusion_windows, self._timezone
        )
