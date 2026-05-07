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
    create_user_job_file,
    load_jobs,
    load_jobs_from_many,
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


def _write_job(dir_path: Path, stem: str, schedule: str = "0 9 * * *") -> Path:
    p = dir_path / f"{stem}.md"
    p.write_text(
        f"---\nschedule: \"{schedule}\"\ntype: capability\ncapability: noop\n---\n",
        encoding="utf-8",
    )
    return p


def test_load_jobs_tags_source():
    with tempfile.TemporaryDirectory() as tmp:
        _write_job(Path(tmp), "alpha")
        jobs = load_jobs(Path(tmp), source="user")
        assert len(jobs) == 1
        assert jobs[0].source == "user"


def test_load_jobs_from_many_no_collision():
    with tempfile.TemporaryDirectory() as sys_dir, tempfile.TemporaryDirectory() as usr_dir:
        _write_job(Path(sys_dir), "system-only")
        _write_job(Path(usr_dir), "user-only")

        jobs = load_jobs_from_many([
            (Path(sys_dir), "system"),
            (Path(usr_dir), "user"),
        ])
        names = {j.name: j.source for j in jobs}
        assert names == {"system-only": "system", "user-only": "user"}


def test_load_jobs_from_many_user_overrides_system(caplog):
    with tempfile.TemporaryDirectory() as sys_dir, tempfile.TemporaryDirectory() as usr_dir:
        sys_path = _write_job(Path(sys_dir), "shared", schedule="0 9 * * *")
        usr_path = _write_job(Path(usr_dir), "shared", schedule="*/5 * * * *")

        with caplog.at_level("WARNING", logger="work_buddy.sidecar.scheduler.jobs"):
            jobs = load_jobs_from_many([
                (Path(sys_dir), "system"),
                (Path(usr_dir), "user"),
            ])

        assert len(jobs) == 1
        assert jobs[0].source == "user"
        assert jobs[0].schedule == "*/5 * * * *"
        assert jobs[0].file_path == usr_path

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("collision" in r.getMessage().lower() for r in warnings), (
            f"Expected a collision warning, got: {[r.getMessage() for r in warnings]}"
        )
        # The warning should name both files so the user can locate the loser.
        msg = next(r.getMessage() for r in warnings if "collision" in r.getMessage().lower())
        assert str(sys_path) in msg
        assert str(usr_path) in msg


def test_job_source_round_trips_through_jobstate():
    job = Job(
        name="rt", file_path=Path("."), schedule="0 9 * * *",
        source="user", job_type="prompt",
    )
    js = JobState(
        name=job.name, schedule=job.schedule, source=job.source,
    )
    # Round-trip through asdict + JobState(**dict) (mirrors save_state/load_state).
    from dataclasses import asdict
    rebuilt = JobState(**asdict(js))
    assert rebuilt.source == "user"


def test_jobstate_default_source_is_system():
    """Old state files written without a source field must still load."""
    js = JobState(name="legacy", schedule="0 9 * * *")
    assert js.source == "system"


def test_create_user_job_file_writes_loadable_prompt_job(tmp_path):
    res = create_user_job_file(
        tmp_path,
        name="hello-prompt", schedule="*/5 * * * *", job_type="prompt",
        prompt="Say hello.",
    )
    assert res["success"] is True
    assert res["file_path"].endswith("hello-prompt.md")

    # File must round-trip through load_jobs as a real Job
    jobs = load_jobs(tmp_path, source="user")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.name == "hello-prompt"
    assert j.job_type == "prompt"
    assert j.schedule == "*/5 * * * *"
    assert j.prompt == "Say hello."
    assert j.source == "user"
    assert j.enabled is True


def test_create_user_job_file_capability_with_params(tmp_path, monkeypatch):
    # Hermetic — don't depend on whether the registry has been built in
    # this process (CI builds lazily and may not have ``task_briefing``
    # registered when this unit test runs). The validator already
    # degrades to lenient behavior on a registry-fetch failure; here we
    # force the "registered" branch with a stub.
    from work_buddy.sidecar.scheduler import jobs as jobs_mod
    monkeypatch.setattr(
        jobs_mod, "_registry_names",
        lambda kind: ["task_briefing"] if kind == "capability" else [],
    )
    res = create_user_job_file(
        tmp_path,
        name="briefing", schedule="0 9 * * 1-5", job_type="capability",
        capability="task_briefing", params={"same_day": True},
    )
    assert res["success"] is True, res.get("error")
    job = load_jobs(tmp_path)[0]
    assert job.job_type == "capability"
    assert job.capability == "task_briefing"
    assert job.params == {"same_day": True}


def test_create_user_job_file_rejects_bad_name(tmp_path):
    for bad in ("", "  ", "../escape", "with spaces", "trailing.dot",
                "-leading-hyphen", "_leading_underscore"):
        res = create_user_job_file(
            tmp_path, name=bad, schedule="0 9 * * *",
            job_type="prompt", prompt="x",
        )
        assert res["success"] is False, f"expected reject for {bad!r}"
        assert "name" in res["error"].lower()


def test_create_user_job_file_rejects_bad_schedule(tmp_path):
    for bad in ("", "every minute", "0 9 * *", "0 9 * * * *",
                "60 * * * *", "* 24 * * *", "* * 32 * *", "* * * 13 *",
                "* * * * 7"):
        res = create_user_job_file(
            tmp_path, name=f"test-{abs(hash(bad)) % 9999}",
            schedule=bad, job_type="prompt", prompt="x",
        )
        assert res["success"] is False, f"expected reject for schedule {bad!r}"


def test_create_user_job_file_requires_type_specific_field(tmp_path):
    res = create_user_job_file(
        tmp_path, name="missing-cap", schedule="0 9 * * *",
        job_type="capability",
    )
    assert res["success"] is False
    assert "capability" in res["error"].lower()

    res = create_user_job_file(
        tmp_path, name="missing-wf", schedule="0 9 * * *",
        job_type="workflow",
    )
    assert res["success"] is False
    assert "workflow" in res["error"].lower()

    res = create_user_job_file(
        tmp_path, name="missing-prompt", schedule="0 9 * * *",
        job_type="prompt",
    )
    assert res["success"] is False
    assert "prompt" in res["error"].lower()


def test_create_user_job_file_refuses_overwrite(tmp_path):
    res1 = create_user_job_file(
        tmp_path, name="dupe", schedule="0 9 * * *",
        job_type="prompt", prompt="first",
    )
    assert res1["success"] is True

    res2 = create_user_job_file(
        tmp_path, name="dupe", schedule="0 10 * * *",
        job_type="prompt", prompt="second",
    )
    assert res2["success"] is False
    assert "already exists" in res2["error"].lower()
    # Original file content must be preserved
    job = load_jobs(tmp_path)[0]
    assert job.prompt == "first"


def test_scheduler_init_and_start_smoke(tmp_path):
    """Constructing Scheduler with a realistic config must not raise.

    Catches refactor regressions (e.g. accidentally referencing a removed
    local variable inside __init__) that unit-testing only the loader
    misses, since loader tests bypass the constructor entirely.
    """
    from work_buddy.sidecar.scheduler.engine import Scheduler

    cfg = {
        "timezone": "UTC",
        "sidecar": {
            "jobs_dir": str(tmp_path / "system"),
            "user_jobs_dir": str(tmp_path / "user"),
            "exclusion_windows": [],
        },
    }
    (tmp_path / "system").mkdir()
    (tmp_path / "user").mkdir()
    _write_job(tmp_path / "user", "smoke")

    sch = Scheduler(cfg)
    sch.start()

    assert any(j.name == "smoke" and j.source == "user" for j in sch.jobs)


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
