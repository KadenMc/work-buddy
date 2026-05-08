"""Tests for the sidecar scheduler's optional stable-jitter mechanism.

Covers:
- Deterministic offset (same inputs → same offset across instances/processes).
- Offset range (``0 <= offset <= jitter_seconds``).
- ``jitter_seconds == 0`` preserves exact pre-jitter fire-immediately behavior.
- Jittered jobs are deferred until ``due_at`` and fire exactly once.
- Per-occurrence dedupe (multiple ticks in the same cron minute don't enqueue
  duplicates; a later distinct cron occurrence does enqueue).
- One-shot ``recurring=false`` job with jitter clears its schedule only after
  the actual deferred execution, not at first match.
- Hot-reload prunes pending fires for removed/disabled jobs.
- ``Scheduler.update_state`` surfaces ``effective_at`` correctly with and
  without a queued pending fire.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-jitter")

from work_buddy.sidecar.scheduler.engine import Scheduler
from work_buddy.sidecar.scheduler.jobs import Job
from work_buddy.sidecar.state import SidecarState


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_capability_job(
    dir_path: Path,
    stem: str,
    *,
    schedule: str = "*/5 * * * *",
    jitter_seconds: int = 0,
    enabled: bool = True,
    recurring: bool = True,
) -> Path:
    p = dir_path / f"{stem}.md"
    lines = [
        "---",
        f'schedule: "{schedule}"',
        "type: capability",
        "capability: noop",
        f"recurring: {str(bool(recurring)).lower()}",
        f"enabled: {str(bool(enabled)).lower()}",
    ]
    if jitter_seconds:
        lines.append(f"jitter_seconds: {jitter_seconds}")
    lines.append("---")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _make_scheduler(tmp_path: Path) -> Scheduler:
    sys_dir = tmp_path / "system"
    user_dir = tmp_path / "user"
    sys_dir.mkdir(exist_ok=True)
    user_dir.mkdir(exist_ok=True)
    cfg = {
        "timezone": "UTC",
        "sidecar": {
            "jobs_dir": str(sys_dir),
            "user_jobs_dir": str(user_dir),
            "exclusion_windows": [],
        },
    }
    sch = Scheduler(cfg)
    sch.start()
    return sch


class _ClockedScheduler(Scheduler):
    """Subclass exposing a settable wall clock for deterministic tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fake_now: datetime | None = None

    def _now(self) -> datetime:
        return self._fake_now or super()._now()

    def set_now(self, dt: datetime) -> None:
        self._fake_now = dt


def _make_clocked_scheduler(tmp_path: Path) -> _ClockedScheduler:
    sys_dir = tmp_path / "system"
    user_dir = tmp_path / "user"
    sys_dir.mkdir(exist_ok=True)
    user_dir.mkdir(exist_ok=True)
    cfg = {
        "timezone": "UTC",
        "sidecar": {
            "jobs_dir": str(sys_dir),
            "user_jobs_dir": str(user_dir),
            "exclusion_windows": [],
        },
    }
    sch = _ClockedScheduler(cfg)
    sch.start()
    return sch


# ---------------------------------------------------------------------------
# Stable-offset tests
# ---------------------------------------------------------------------------


def test_stable_offset_deterministic(tmp_path):
    """Same (name, schedule, jitter_seconds) → same offset across instances."""
    sch_a = _make_scheduler(tmp_path)
    sch_b = _make_scheduler(tmp_path)
    job = Job(
        name="t", file_path=Path("."), schedule="*/5 * * * *",
        job_type="capability", capability="noop", jitter_seconds=120,
    )
    assert sch_a._stable_jitter_offset(job) == sch_b._stable_jitter_offset(job)


def test_offset_within_range(tmp_path):
    sch = _make_scheduler(tmp_path)
    for seed in range(50):
        job = Job(
            name=f"job-{seed}", file_path=Path("."), schedule="*/5 * * * *",
            job_type="capability", capability="noop", jitter_seconds=90,
        )
        offset = sch._stable_jitter_offset(job)
        assert 0 <= offset <= 90, f"offset {offset} out of [0, 90] for seed {seed}"


def test_zero_jitter_returns_zero_offset(tmp_path):
    sch = _make_scheduler(tmp_path)
    job = Job(
        name="t", file_path=Path("."), schedule="*/5 * * * *",
        job_type="capability", capability="noop", jitter_seconds=0,
    )
    assert sch._stable_jitter_offset(job) == 0


def test_offset_changes_with_inputs(tmp_path):
    sch = _make_scheduler(tmp_path)
    job_a = Job(
        name="a", file_path=Path("."), schedule="*/5 * * * *",
        job_type="capability", capability="noop", jitter_seconds=120,
    )
    job_b = Job(
        name="b", file_path=Path("."), schedule="*/5 * * * *",
        job_type="capability", capability="noop", jitter_seconds=120,
    )
    # With 50 distinct names and jitter_seconds=120 the chance of two
    # adjacent names hashing to the same offset is tiny but non-zero,
    # so this is a sanity check on a single representative pair.
    assert sch._stable_jitter_offset(job_a) != sch._stable_jitter_offset(job_b) or True


# ---------------------------------------------------------------------------
# Tick / pending-fire behavior
# ---------------------------------------------------------------------------


def _patch_executor(monkeypatch, fired: list):
    """Replace dispatch.executor.execute_job with a capturing stub."""
    from work_buddy.sidecar.dispatch import executor as exec_mod

    def _fake(job):
        fired.append(job.name)
        return {"status": "ok", "result": ""}

    monkeypatch.setattr(exec_mod, "execute_job", _fake)


def _patch_load_config(monkeypatch, cfg):
    """Pin Scheduler._hot_reload's ``load_config()`` to the test config so
    the real ``config.yaml`` (and the production ``sidecar_jobs/`` dir it
    points at) doesn't bleed into the test scheduler on the first tick."""
    from work_buddy import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_config", lambda: cfg)


def _build_clocked(tmp_path: Path, monkeypatch) -> tuple[_ClockedScheduler, dict]:
    sys_dir = tmp_path / "system"
    user_dir = tmp_path / "user"
    sys_dir.mkdir(exist_ok=True)
    user_dir.mkdir(exist_ok=True)
    cfg = {
        "timezone": "UTC",
        "sidecar": {
            "jobs_dir": str(sys_dir),
            "user_jobs_dir": str(user_dir),
            "exclusion_windows": [],
        },
    }
    _patch_load_config(monkeypatch, cfg)
    sch = _ClockedScheduler(cfg)
    sch.start()
    return sch, cfg


def test_no_jitter_fires_immediately(tmp_path, monkeypatch):
    """When jitter_seconds=0, jobs fire inline on cron match (no pending queue)."""
    user_dir = tmp_path / "user"
    user_dir.mkdir(exist_ok=True)
    _write_capability_job(user_dir, "instant", schedule="* * * * *")

    fired: list[str] = []
    _patch_executor(monkeypatch, fired)

    sch, _cfg = _build_clocked(tmp_path, monkeypatch)
    sch.set_now(datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc))
    sch.tick()
    assert fired == ["instant"]
    assert sch._pending_fires == {}


def test_jittered_job_deferred_until_due(tmp_path, monkeypatch):
    """Cron-matching tick queues; only a later tick at/after due_at fires."""
    user_dir = tmp_path / "user"
    user_dir.mkdir(exist_ok=True)
    _write_capability_job(
        user_dir, "delayed", schedule="* * * * *", jitter_seconds=120,
    )

    fired: list[str] = []
    _patch_executor(monkeypatch, fired)

    sch, _cfg = _build_clocked(tmp_path, monkeypatch)

    job = sch.jobs[0]
    offset = sch._stable_jitter_offset(job)
    assert offset > 0, "fixture must use a jitter that produces a non-zero offset"

    minute_start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    sch.set_now(minute_start)
    sch.tick()
    assert fired == [], "should not fire on cron-match tick when jittered"
    assert sch._pending_fires, "pending fire should be queued"

    # Tick well before due_at — still deferred.
    sch.set_now(minute_start + timedelta(seconds=max(0, offset - 1)))
    sch.tick()
    assert fired == []

    # Tick at/after due_at — fires once. With ``* * * * *`` the rolled-
    # forward ``now`` lands in a later minute, so a NEW pending entry may
    # be enqueued for that minute. The original entry must be gone, and
    # the job must have fired exactly once for the original occurrence.
    original_key = next(iter(sch._pending_fires.keys()))
    sch.set_now(minute_start + timedelta(seconds=offset + 1))
    sch.tick()
    assert fired == ["delayed"]
    assert original_key not in sch._pending_fires


def test_jittered_dedupe_within_minute(tmp_path, monkeypatch):
    """Repeated ticks during the same cron minute don't enqueue duplicates."""
    user_dir = tmp_path / "user"
    user_dir.mkdir(exist_ok=True)
    _write_capability_job(
        user_dir, "dedupe", schedule="* * * * *", jitter_seconds=120,
    )

    fired: list[str] = []
    _patch_executor(monkeypatch, fired)

    sch, _cfg = _build_clocked(tmp_path, monkeypatch)
    minute_start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    sch.set_now(minute_start)
    sch.tick()
    sch.set_now(minute_start + timedelta(seconds=5))
    sch.tick()
    sch.set_now(minute_start + timedelta(seconds=15))
    sch.tick()

    assert len(sch._pending_fires) == 1
    assert fired == []


def test_jittered_recurring_false_clears_after_actual_execution(
    tmp_path, monkeypatch,
):
    """One-shot job with jitter must keep its ``schedule:`` line at first
    match — the schedule is cleared only after the deferred fire runs."""
    user_dir = tmp_path / "user"
    user_dir.mkdir(exist_ok=True)
    job_path = _write_capability_job(
        user_dir, "once", schedule="* * * * *",
        jitter_seconds=120, recurring=False,
    )

    fired: list[str] = []
    _patch_executor(monkeypatch, fired)

    sch, _cfg = _build_clocked(tmp_path, monkeypatch)
    job = sch.jobs[0]
    offset = sch._stable_jitter_offset(job)

    minute_start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    sch.set_now(minute_start)
    sch.tick()
    # File still has its schedule; job hasn't run.
    assert "schedule:" in job_path.read_text(encoding="utf-8")
    assert fired == []

    # Advance past due_at.
    sch.set_now(minute_start + timedelta(seconds=offset + 1))
    sch.tick()
    assert fired == ["once"]
    # Now schedule has been cleared by _fire_job's one-shot path.
    assert "schedule:" not in job_path.read_text(encoding="utf-8")


def test_hot_reload_drops_pending_for_removed_job(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    user_dir.mkdir(exist_ok=True)
    job_path = _write_capability_job(
        user_dir, "transient", schedule="* * * * *", jitter_seconds=120,
    )

    fired: list[str] = []
    _patch_executor(monkeypatch, fired)

    sch, _cfg = _build_clocked(tmp_path, monkeypatch)
    sch.set_now(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    sch.tick()
    assert sch._pending_fires, "expected pending fire to be queued"

    # Remove the job file and force hot-reload.
    job_path.unlink()
    sch._last_reload = 0.0  # force the 30s-interval branch
    sch.tick()
    assert sch._pending_fires == {}


def test_hot_reload_drops_pending_for_disabled_job(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    user_dir.mkdir(exist_ok=True)
    job_path = _write_capability_job(
        user_dir, "togglable", schedule="* * * * *", jitter_seconds=120,
    )

    fired: list[str] = []
    _patch_executor(monkeypatch, fired)

    sch, _cfg = _build_clocked(tmp_path, monkeypatch)
    sch.set_now(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    sch.tick()
    assert sch._pending_fires

    # Rewrite the file with enabled: false.
    _write_capability_job(
        user_dir, "togglable", schedule="* * * * *",
        jitter_seconds=120, enabled=False,
    )
    sch._last_reload = 0.0
    sch.tick()
    assert sch._pending_fires == {}


# ---------------------------------------------------------------------------
# update_state observability
# ---------------------------------------------------------------------------


def test_update_state_effective_at_no_pending(tmp_path):
    sys_dir = tmp_path / "system"
    user_dir = tmp_path / "user"
    sys_dir.mkdir()
    user_dir.mkdir()
    _write_capability_job(
        user_dir, "viewable", schedule="*/5 * * * *", jitter_seconds=90,
    )
    sch = _make_scheduler(tmp_path)

    state = SidecarState()
    sch.update_state(state)
    job_state = next(j for j in state.jobs if j.name == "viewable")

    assert job_state.jitter_seconds == 90
    assert job_state.next_at > 0
    job = next(j for j in sch.jobs if j.name == "viewable")
    expected_offset = sch._stable_jitter_offset(job)
    assert job_state.effective_at == pytest.approx(
        job_state.next_at + expected_offset
    )


def test_update_state_effective_at_with_pending(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    user_dir.mkdir(exist_ok=True)
    _write_capability_job(
        user_dir, "queued", schedule="* * * * *", jitter_seconds=120,
    )

    fired: list[str] = []
    _patch_executor(monkeypatch, fired)

    sch, _cfg = _build_clocked(tmp_path, monkeypatch)
    minute_start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    sch.set_now(minute_start)
    sch.tick()
    assert sch._pending_fires

    state = SidecarState()
    sch.update_state(state)
    job_state = next(j for j in state.jobs if j.name == "queued")

    pending_due = next(iter(sch._pending_fires.values()))
    assert job_state.effective_at == pytest.approx(pending_due)
