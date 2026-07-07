"""Tests for daemon helpers that keep a misconfiguration or a sustained
fault from silently taking down the sidecar.

``safe_port`` rejects an invalid ``sidecar.services.<svc>.port`` so the
daemon skips that one service instead of crashing. ``TickFailureTracker``
turns a run of failing ticks — otherwise caught as "non-fatal" and
retried forever — into a single loud signal. The dispatch-cycle tests
cover the split between the supervisor loop (publishes state, restarts
children) and the dispatch loop (executes jobs/polls/sweeps inline and
may block for minutes): phase markers, stall classification, and
per-cycle failure containment.
"""

from __future__ import annotations

import logging
from pathlib import Path

from work_buddy.sidecar.daemon import (
    TickFailureTracker,
    _dispatch_stall_message,
    _run_dispatch_cycle,
    safe_port,
)
from work_buddy.sidecar.event_log import EventLog
from work_buddy.sidecar.scheduler.engine import Scheduler
from work_buddy.sidecar.scheduler.jobs import Job
from work_buddy.sidecar.state import SidecarState


class TestSafePort:
    def test_valid_int_port(self):
        assert safe_port(5127, service_name="dashboard") == 5127

    def test_numeric_string_is_coerced(self):
        assert safe_port("5123", service_name="messaging") == 5123

    def test_non_numeric_is_rejected(self, caplog):
        with caplog.at_level(logging.ERROR, logger="work_buddy.sidecar.daemon"):
            assert safe_port("not-a-port", service_name="messaging") is None
        assert any("messaging" in r.message for r in caplog.records)

    def test_out_of_range_is_rejected(self, caplog):
        with caplog.at_level(logging.ERROR, logger="work_buddy.sidecar.daemon"):
            assert safe_port(99999, service_name="embedding") is None
            assert safe_port(0, service_name="embedding") is None

    def test_none_is_rejected(self):
        assert safe_port(None, service_name="telegram") is None


class TestTickFailureTracker:
    def test_successes_never_escalate(self):
        tracker = TickFailureTracker(threshold=3)
        for _ in range(10):
            assert tracker.record_success() is None

    def test_escalates_once_at_threshold(self):
        tracker = TickFailureTracker(threshold=3)
        exc = RuntimeError("boom")
        assert tracker.record_failure(exc) is None   # 1
        assert tracker.record_failure(exc) is None   # 2
        msg = tracker.record_failure(exc)            # 3 — crosses threshold
        assert msg is not None and "3" in msg and "boom" in msg
        # A continuing failure run does not re-escalate.
        assert tracker.record_failure(exc) is None   # 4

    def test_recovery_emits_once_after_escalation(self):
        tracker = TickFailureTracker(threshold=2)
        tracker.record_failure(RuntimeError("x"))
        assert tracker.record_failure(RuntimeError("x")) is not None  # escalated
        recovery = tracker.record_success()
        assert recovery is not None and "recovered" in recovery.lower()
        # Recovery is announced exactly once.
        assert tracker.record_success() is None

    def test_no_recovery_message_without_prior_escalation(self):
        tracker = TickFailureTracker(threshold=3)
        tracker.record_failure(RuntimeError("x"))   # 1, below threshold
        assert tracker.record_success() is None     # never escalated → no message

    def test_failure_run_resets_on_success(self):
        tracker = TickFailureTracker(threshold=3)
        tracker.record_failure(RuntimeError("x"))   # 1
        tracker.record_failure(RuntimeError("x"))   # 2
        tracker.record_success()                    # resets the run
        # Counter restarted — two more failures must not escalate yet.
        assert tracker.record_failure(RuntimeError("x")) is None  # 1
        assert tracker.record_failure(RuntimeError("x")) is None  # 2


# ---------------------------------------------------------------------------
# Dispatch stall classification
# ---------------------------------------------------------------------------

def _state_in_phase(phase: str, since: float, job: str = "") -> SidecarState:
    st = SidecarState()
    st.dispatch_phase = phase
    st.dispatch_phase_since = since
    st.dispatch_job = job
    return st


class TestDispatchStallMessage:
    def test_idle_is_never_a_stall(self):
        st = _state_in_phase("idle", since=0.0)
        assert _dispatch_stall_message(st, now=10_000.0, threshold_seconds=1) is None

    def test_pre_first_cycle_is_quiet(self):
        # A daemon that has not started dispatching yet (or a state file
        # from a daemon without dispatch fields) has phase "" / since 0.
        st = _state_in_phase("", since=0.0)
        assert _dispatch_stall_message(st, now=10_000.0, threshold_seconds=1) is None

    def test_under_threshold_is_quiet(self):
        st = _state_in_phase("scheduler_tick", since=1_000.0)
        assert _dispatch_stall_message(
            st, now=1_000.0 + 599, threshold_seconds=600,
        ) is None

    def test_over_threshold_names_phase_duration_and_job(self):
        st = _state_in_phase(
            "scheduler_tick", since=1_000.0, job="ir-index-rebuild",
        )
        msg = _dispatch_stall_message(st, now=1_000.0 + 700, threshold_seconds=600)
        assert msg is not None
        assert "scheduler_tick" in msg
        assert "ir-index-rebuild" in msg
        assert "700" in msg

    def test_no_job_omits_job_clause(self):
        st = _state_in_phase("retry_sweep", since=1_000.0)
        msg = _dispatch_stall_message(st, now=1_000.0 + 700, threshold_seconds=600)
        assert msg is not None
        assert "(job " not in msg


# ---------------------------------------------------------------------------
# Dispatch cycle: ordering, phase markers, failure containment
# ---------------------------------------------------------------------------

class _FakeScheduler:
    def __init__(self, calls: list[str], fail: bool = False):
        self._calls = calls
        self._fail = fail

    def tick(self) -> None:
        if self._fail:
            raise RuntimeError("tick boom")
        self._calls.append("tick")

    def update_state(self, state: SidecarState) -> None:
        self._calls.append("update_state")


class _FakePoller:
    def __init__(self, calls: list[str]):
        self._calls = calls

    def poll(self) -> None:
        self._calls.append("poll")


class _FakeSweep:
    def __init__(self, calls: list[str], fail: bool = False):
        self._calls = calls
        self._fail = fail

    def sweep(self) -> None:
        if self._fail:
            raise RuntimeError("sweep boom")
        self._calls.append("sweep")


class TestRunDispatchCycle:
    def test_phases_run_in_order_and_land_idle(self):
        calls: list[str] = []
        st = SidecarState()
        _run_dispatch_cycle(
            st, _FakeScheduler(calls), _FakePoller(calls), _FakeSweep(calls),
            EventLog(), None, TickFailureTracker(),
        )
        assert calls == ["tick", "update_state", "poll", "sweep"]
        assert st.dispatch_phase == "idle"
        assert st.last_dispatch_at > 0

    def test_scheduler_failure_is_contained_and_escalated(self):
        calls: list[str] = []
        st = SidecarState()
        event_log = EventLog()
        # threshold=1: the first failure escalates immediately.
        _run_dispatch_cycle(
            st, _FakeScheduler(calls, fail=True), _FakePoller(calls),
            _FakeSweep(calls), event_log, None, TickFailureTracker(threshold=1),
        )
        # Later phases are skipped this cycle, but nothing propagates and
        # the phase marker still lands on idle.
        assert calls == []
        assert st.dispatch_phase == "idle"
        assert st.last_dispatch_at > 0
        assert any(e["kind"] == "tick_failures" for e in event_log.recent(10))

    def test_sweep_failure_does_not_fail_the_cycle(self):
        calls: list[str] = []
        st = SidecarState()
        event_log = EventLog()
        _run_dispatch_cycle(
            st, _FakeScheduler(calls), _FakePoller(calls),
            _FakeSweep(calls, fail=True), event_log, None,
            TickFailureTracker(threshold=1),
        )
        # Sweep errors are contained by their own handler, so the cycle
        # counts as a success (no escalation event).
        assert calls == ["tick", "update_state", "poll"]
        assert st.dispatch_phase == "idle"
        assert not any(e["kind"] == "tick_failures" for e in event_log.recent(10))

    def test_event_tick_failure_is_swallowed(self):
        calls: list[str] = []
        st = SidecarState()

        def _boom(_scheduler) -> None:
            raise RuntimeError("event tick boom")

        _run_dispatch_cycle(
            st, _FakeScheduler(calls), _FakePoller(calls), _FakeSweep(calls),
            EventLog(), _boom, TickFailureTracker(threshold=1),
        )
        assert calls == ["tick", "update_state", "poll", "sweep"]
        assert st.dispatch_phase == "idle"


# ---------------------------------------------------------------------------
# Scheduler.current_job: stall attribution for inline job execution
# ---------------------------------------------------------------------------

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
    return Scheduler(cfg)


class TestSchedulerCurrentJob:
    def test_set_during_execution_and_cleared_after(self, tmp_path, monkeypatch):
        sch = _make_scheduler(tmp_path)
        seen: list[str] = []

        def _fake_execute(job):
            seen.append(sch.current_job)
            return {"status": "ok", "result": "done"}

        monkeypatch.setattr(
            "work_buddy.sidecar.dispatch.executor.execute_job", _fake_execute,
        )
        job = Job(
            name="probe-job", file_path=Path("."), schedule="*/5 * * * *",
            job_type="capability", capability="noop",
        )
        assert sch.current_job == ""
        sch._fire_job(job)
        assert seen == ["probe-job"]
        assert sch.current_job == ""

    def test_cleared_even_when_execution_raises(self, tmp_path, monkeypatch):
        sch = _make_scheduler(tmp_path)

        def _fake_execute(job):
            raise RuntimeError("job boom")

        monkeypatch.setattr(
            "work_buddy.sidecar.dispatch.executor.execute_job", _fake_execute,
        )
        job = Job(
            name="probe-job", file_path=Path("."), schedule="*/5 * * * *",
            job_type="capability", capability="noop",
        )
        sch._fire_job(job)
        assert sch.current_job == ""
        assert job.last_result == "error"
