"""Tests for daemon helpers that keep a misconfiguration or a sustained
fault from silently taking down the sidecar.

``safe_port`` rejects an invalid ``sidecar.services.<svc>.port`` so the
daemon skips that one service instead of crashing. ``TickFailureTracker``
turns a run of failing ticks — otherwise caught as "non-fatal" and
retried forever — into a single loud signal.
"""

from __future__ import annotations

import logging

from work_buddy.sidecar.daemon import TickFailureTracker, safe_port


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
