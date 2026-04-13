"""Tests for sidecar core modules: PID, state, jobs, heartbeat."""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-sidecar")

from work_buddy.sidecar.state import (
    SidecarState,
    ServiceHealth,
    JobState,
    save_state,
    load_state,
    STATE_FILE,
)
from work_buddy.sidecar.pid import (
    PID_FILE,
    _is_process_alive,
    check_existing_daemon,
    write_pid_file,
    cleanup_pid_file,
)
from work_buddy.sidecar.scheduler.jobs import (
    Job,
    load_jobs,
    _parse_job_file,
    job_fingerprint,
)
from work_buddy.sidecar.scheduler.heartbeat import (
    ExclusionWindow,
    is_excluded,
    parse_exclusion_windows,
)


# --- PID tests ---

def test_is_process_alive_self():
    assert _is_process_alive(os.getpid()) is True


def test_is_process_alive_bogus():
    assert _is_process_alive(99999999) is False


def test_pid_write_read_cleanup():
    # Ensure clean state
    cleanup_pid_file()
    assert not PID_FILE.exists()

    write_pid_file()
    assert PID_FILE.exists()
    content = PID_FILE.read_text().strip()
    assert content == str(os.getpid())

    cleanup_pid_file()
    assert not PID_FILE.exists()


def test_check_existing_daemon_none():
    cleanup_pid_file()
    assert check_existing_daemon() is None


def test_check_existing_daemon_stale():
    # Write a bogus PID
    PID_FILE.write_text("99999999\n")
    result = check_existing_daemon()
    assert result is None
    assert not PID_FILE.exists()  # Should auto-clean stale


# --- State tests ---

def test_state_round_trip():
    state = SidecarState(
        started_at=1712345678.0,
        pid=12345,
    )
    state.services["messaging"] = ServiceHealth(
        name="messaging", port=5123, status="healthy", pid=99,
    )
    state.jobs.append(JobState(
        name="daily-briefing", schedule="0 9 * * 1-5", next_at=1712400000.0,
    ))
    save_state(state)
    assert STATE_FILE.exists()

    loaded = load_state()
    assert loaded is not None
    assert loaded.pid == 12345
    assert "messaging" in loaded.services
    assert loaded.services["messaging"].status == "healthy"
    assert len(loaded.jobs) == 1
    assert loaded.jobs[0].name == "daily-briefing"

    # Cleanup
    STATE_FILE.unlink(missing_ok=True)


# --- Job tests ---

def test_job_parsing():
    with tempfile.TemporaryDirectory() as tmpdir:
        job_file = Path(tmpdir) / "test-job.md"
        job_file.write_text("""---
schedule: "*/5 * * * *"
recurring: true
type: capability
capability: task_briefing
description: "Test job"
---
""")
        job = _parse_job_file(job_file)
        assert job is not None
        assert job.name == "test-job"
        assert job.schedule == "*/5 * * * *"
        assert job.job_type == "capability"
        assert job.capability == "task_briefing"
        assert job.recurring is True


def test_job_no_schedule():
    with tempfile.TemporaryDirectory() as tmpdir:
        job_file = Path(tmpdir) / "no-schedule.md"
        job_file.write_text("""---
type: capability
capability: something
---
""")
        job = _parse_job_file(job_file)
        assert job is None  # No schedule = not a valid job


def test_load_jobs_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create two job files
        (Path(tmpdir) / "job-a.md").write_text("""---
schedule: "0 9 * * *"
type: capability
capability: task_briefing
---
""")
        (Path(tmpdir) / "job-b.md").write_text("""---
schedule: "30 17 * * 1-5"
type: prompt
description: "End of day review"
---
What did I accomplish today?
""")
        # And one non-job file (no schedule)
        (Path(tmpdir) / "not-a-job.md").write_text("# Just a note\n")

        jobs = load_jobs(Path(tmpdir))
        assert len(jobs) == 2
        names = {j.name for j in jobs}
        assert "job-a" in names
        assert "job-b" in names


def test_job_fingerprint():
    job = Job(
        name="test", file_path=Path("."), schedule="0 9 * * *",
        job_type="capability", capability="task_briefing",
    )
    fp = job_fingerprint(job)
    assert "test" in fp
    assert "task_briefing" in fp


# --- Heartbeat/exclusion tests ---

def test_exclusion_window_simple():
    windows = [ExclusionWindow(start="23:00", end="08:00")]
    # 3am should be excluded (overnight window)
    dt_3am = datetime(2026, 4, 6, 3, 0, 0, tzinfo=timezone.utc)
    assert is_excluded(dt_3am, windows) is True

    # 10am should NOT be excluded
    dt_10am = datetime(2026, 4, 6, 10, 0, 0, tzinfo=timezone.utc)
    assert is_excluded(dt_10am, windows) is False


def test_exclusion_window_daytime():
    windows = [ExclusionWindow(start="12:00", end="13:00")]
    dt_noon = datetime(2026, 4, 6, 12, 30, 0, tzinfo=timezone.utc)
    assert is_excluded(dt_noon, windows) is True

    dt_2pm = datetime(2026, 4, 6, 14, 0, 0, tzinfo=timezone.utc)
    assert is_excluded(dt_2pm, windows) is False


def test_exclusion_window_with_days():
    # Only exclude weekends (Sat=5, Sun=6)
    windows = [ExclusionWindow(start="00:00", end="23:59", days=[5, 6])]
    # Monday (0) should NOT be excluded
    dt_mon = datetime(2026, 4, 6, 12, 0, 0, tzinfo=timezone.utc)
    assert is_excluded(dt_mon, windows) is False


def test_parse_exclusion_config():
    cfg = [
        {"start": "23:00", "end": "08:00"},
        {"start": "invalid", "end": "08:00"},  # Should be skipped
    ]
    windows = parse_exclusion_windows(cfg)
    assert len(windows) == 1


def test_no_exclusion_windows():
    dt = datetime(2026, 4, 6, 3, 0, 0, tzinfo=timezone.utc)
    assert is_excluded(dt, []) is False


if __name__ == "__main__":
    test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in test_funcs:
        try:
            fn()
            print(f"  PASS: {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
    sys.exit(1 if failed else 0)
