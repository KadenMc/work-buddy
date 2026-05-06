"""Tests for JobsWatcher — the filesystem watcher on jobs directories.

The watcher signals the scheduler's ``jobs_reload_pending`` Event when a
``.md`` file appears, changes, is renamed away, or is deleted under a
watched dir. Non-``.md`` files are ignored. Missing directories are
skipped with a warning rather than raising.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-jobs-watcher")

from work_buddy.sidecar.scheduler.watcher import JobsWatcher


class _FakeScheduler:
    """Stand-in for Scheduler — only provides what JobsWatcher reads."""

    def __init__(self, jobs_dirs: list[tuple[Path, str]]) -> None:
        self._jobs_dirs = jobs_dirs
        self.jobs_reload_pending = threading.Event()


def _wait_for_event(event: threading.Event, *, timeout: float = 2.0) -> bool:
    """Wait up to ``timeout`` seconds; return True iff the event fired."""
    return event.wait(timeout=timeout)


# --- Setup / teardown plumbing ---

def test_start_skips_missing_dirs(tmp_path, caplog):
    """A non-existent dir logs a warning and the watcher still starts on the rest."""
    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "missing"  # never created

    sched = _FakeScheduler([(missing, "system"), (real, "user")])
    watcher = JobsWatcher(sched)
    with caplog.at_level("WARNING", logger="work_buddy.sidecar.scheduler.watcher"):
        watcher.start()
    try:
        assert any(
            "skipping non-existent path" in r.getMessage().lower() and "missing" in r.getMessage()
            for r in caplog.records
        ), f"Expected skip warning, got: {[r.getMessage() for r in caplog.records]}"
    finally:
        watcher.stop()


def test_start_no_dirs_falls_back_quietly(tmp_path, caplog):
    """When NO dir exists, watcher logs a fallback notice and stays inert."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    sched = _FakeScheduler([(a, "system"), (b, "user")])
    watcher = JobsWatcher(sched)
    with caplog.at_level("WARNING", logger="work_buddy.sidecar.scheduler.watcher"):
        watcher.start()
    try:
        assert any(
            "no jobs directories exist" in r.getMessage().lower()
            for r in caplog.records
        )
    finally:
        watcher.stop()


def test_stop_is_idempotent(tmp_path):
    sched = _FakeScheduler([(tmp_path, "user")])
    watcher = JobsWatcher(sched)
    watcher.start()
    watcher.stop()
    watcher.stop()  # no raise


# --- Event-firing behavior ---

def test_create_md_sets_event(tmp_path):
    sched = _FakeScheduler([(tmp_path, "user")])
    watcher = JobsWatcher(sched)
    watcher.start()
    try:
        # Tiny delay so the observer thread is fully wired before we touch
        # the dir; without it the very first event sometimes races on slow
        # CI boxes. Local NTFS is much faster but this keeps it portable.
        time.sleep(0.05)
        sched.jobs_reload_pending.clear()

        target = tmp_path / "newjob.md"
        target.write_text('---\nschedule: "* * * * *"\n---\n', encoding="utf-8")

        assert _wait_for_event(sched.jobs_reload_pending), \
            "Expected jobs_reload_pending to fire after .md create"
    finally:
        watcher.stop()


def test_modify_md_sets_event(tmp_path):
    target = tmp_path / "existing.md"
    target.write_text("---\nschedule: \"0 9 * * *\"\n---\n", encoding="utf-8")

    sched = _FakeScheduler([(tmp_path, "user")])
    watcher = JobsWatcher(sched)
    watcher.start()
    try:
        time.sleep(0.05)
        sched.jobs_reload_pending.clear()

        target.write_text("---\nschedule: \"*/5 * * * *\"\n---\n", encoding="utf-8")

        assert _wait_for_event(sched.jobs_reload_pending), \
            "Expected jobs_reload_pending to fire after .md modify"
    finally:
        watcher.stop()


def test_delete_md_sets_event(tmp_path):
    target = tmp_path / "doomed.md"
    target.write_text('---\nschedule: "0 9 * * *"\n---\n', encoding="utf-8")

    sched = _FakeScheduler([(tmp_path, "user")])
    watcher = JobsWatcher(sched)
    watcher.start()
    try:
        time.sleep(0.05)
        sched.jobs_reload_pending.clear()

        target.unlink()

        assert _wait_for_event(sched.jobs_reload_pending), \
            "Expected jobs_reload_pending to fire after .md delete"
    finally:
        watcher.stop()


def test_non_md_file_ignored(tmp_path):
    """Creating .txt / .json must NOT trigger a reload."""
    sched = _FakeScheduler([(tmp_path, "user")])
    watcher = JobsWatcher(sched)
    watcher.start()
    try:
        time.sleep(0.05)
        sched.jobs_reload_pending.clear()

        (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")

        # Wait briefly to give a hypothetical false-positive a chance to fire.
        # If nothing happens within 500ms, the filter is doing its job.
        assert not _wait_for_event(sched.jobs_reload_pending, timeout=0.5), \
            "Non-.md file change must NOT trigger jobs_reload_pending"
    finally:
        watcher.stop()


def test_multiple_dirs_each_trigger(tmp_path):
    """Both watched dirs feed the same event."""
    sys_dir = tmp_path / "sidecar_jobs"
    usr_dir = tmp_path / "user_jobs"
    sys_dir.mkdir()
    usr_dir.mkdir()

    sched = _FakeScheduler([(sys_dir, "system"), (usr_dir, "user")])
    watcher = JobsWatcher(sched)
    watcher.start()
    try:
        time.sleep(0.05)

        sched.jobs_reload_pending.clear()
        (sys_dir / "in-system.md").write_text("---\nschedule: \"0 9 * * *\"\n---\n")
        assert _wait_for_event(sched.jobs_reload_pending)

        sched.jobs_reload_pending.clear()
        (usr_dir / "in-user.md").write_text("---\nschedule: \"0 9 * * *\"\n---\n")
        assert _wait_for_event(sched.jobs_reload_pending)
    finally:
        watcher.stop()
