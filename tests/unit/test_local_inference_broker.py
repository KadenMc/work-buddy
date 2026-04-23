"""Invariants for ``work_buddy.inference.broker``.

Covers:

* Slot admission under concurrency (respects ``max_concurrent``).
* Priority ordering (higher priority admits ahead of queued lower).
* Queue capacity — ``QueueFull`` raised synchronously when
  ``max_queued`` exceeded at a priority.
* Queue-wait timeout — ``QueueWaitTimeout`` raised (not
  ``InferenceTimeout``) when admission takes too long.
* Metrics — queued/admitted/finished timestamps, status transitions,
  ring-buffer capacity.
* Reconfigure — bumping ``max_concurrent`` wakes waiters.

Uses short sleeps in a few places (milliseconds) to give contested
threads time to park on conditions before an assertion. That's
unavoidable for broker concurrency testing; each sleep is justified
in-line.
"""

from __future__ import annotations

import threading
import time

import pytest

from work_buddy.inference import broker as bmod
from work_buddy.inference.broker import (
    LocalInferenceBroker,
    Priority,
    ProfileConfig,
    QueueFull,
    QueueWaitTimeout,
    _reset_broker_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test gets a fresh broker singleton."""
    _reset_broker_for_tests()
    yield
    _reset_broker_for_tests()


def _make_broker(**kwargs) -> LocalInferenceBroker:
    """Broker with a single 'test' profile configured to the args."""
    b = LocalInferenceBroker()
    b.configure_profile(ProfileConfig(name="test", **kwargs))
    return b


# ---------------------------------------------------------------------------
# Slot admission
# ---------------------------------------------------------------------------


def test_slot_admits_immediately_when_idle():
    """With no contention, the first caller admits with minimal latency."""
    b = _make_broker(max_concurrent=1)
    t0 = time.monotonic()
    with b.slot(profile="test") as ticket:
        assert ticket.id
    total = (time.monotonic() - t0) * 1000
    assert total < 50, f"uncontended slot took {total:.1f}ms (too slow)"

    metrics = b.snapshot_metrics()
    assert len(metrics) == 1
    m = metrics[0]
    assert m["status"] == "ok"
    assert m["queue_wait_ms"] is not None
    assert m["queue_wait_ms"] < 20.0
    assert m["service_time_ms"] is not None


def test_concurrent_slots_serialize_when_max_concurrent_is_1():
    """Two callers, one slot: the second waits for the first to finish."""
    b = _make_broker(max_concurrent=1)
    order: list[str] = []
    lock = threading.Lock()

    def _worker(name: str, hold_s: float):
        with b.slot(profile="test", queue_wait_s=5.0):
            with lock:
                order.append(f"{name}:admit")
            time.sleep(hold_s)
            with lock:
                order.append(f"{name}:done")

    t1 = threading.Thread(target=_worker, args=("A", 0.15))
    t2 = threading.Thread(target=_worker, args=("B", 0.05))
    t1.start()
    # Small gap so A reliably admits first.
    time.sleep(0.02)
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert order == ["A:admit", "A:done", "B:admit", "B:done"], (
        f"Expected serialized order, got {order}"
    )


def test_max_concurrent_gte_2_admits_in_parallel():
    """With max_concurrent=2, both admit before either finishes."""
    b = _make_broker(max_concurrent=2)
    admit_order: list[str] = []
    done_order: list[str] = []
    lock = threading.Lock()

    def _worker(name: str):
        with b.slot(profile="test"):
            with lock:
                admit_order.append(name)
            time.sleep(0.08)
            with lock:
                done_order.append(name)

    threads = [threading.Thread(target=_worker, args=(c,)) for c in "AB"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3.0)

    # Both admit before either finishes.
    assert len(admit_order) == 2
    assert len(done_order) == 2
    # admit_order doesn't strictly precede done_order; we check overlap
    # via metrics timestamps below for a stronger assertion.
    metrics = b.snapshot_metrics()
    # Use <= because time.monotonic() on Windows has ~16ms resolution;
    # two admits inside the same tick are legitimate parallelism.
    a, b_m = sorted(metrics, key=lambda m: m["admitted_at"])
    assert a["admitted_at"] <= b_m["admitted_at"] < a["finished_at"], (
        "Second admit should have happened before first finished "
        "(parallel execution not observed)"
    )


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


def test_higher_priority_admits_ahead_of_queued_lower():
    """An INTERACTIVE ticket added while a BACKGROUND ticket is running
    must admit before another BACKGROUND ticket that's been waiting.
    """
    b = _make_broker(max_concurrent=1)
    admit_log: list[str] = []
    log_lock = threading.Lock()
    barrier_running = threading.Event()
    can_release_running = threading.Event()

    def _worker(name: str, priority: Priority, hold_s: float = 0.05,
                signal_running: bool = False):
        with b.slot(profile="test", priority=priority, queue_wait_s=5.0):
            with log_lock:
                admit_log.append(name)
            if signal_running:
                barrier_running.set()
                can_release_running.wait(timeout=5.0)
            else:
                time.sleep(hold_s)

    # Holder occupies the single slot.
    t_hold = threading.Thread(target=_worker,
                              args=("hold-BG", Priority.BACKGROUND),
                              kwargs={"signal_running": True})
    t_hold.start()
    assert barrier_running.wait(timeout=2.0)

    # Now enqueue: first a BACKGROUND (entered first), then INTERACTIVE.
    t_bg = threading.Thread(target=_worker, args=("queued-BG", Priority.BACKGROUND))
    t_int = threading.Thread(target=_worker, args=("queued-INT", Priority.INTERACTIVE))
    t_bg.start()
    time.sleep(0.05)  # give t_bg time to enter the wait
    t_int.start()
    time.sleep(0.05)  # give t_int time to enter the wait

    # Release the holder. INTERACTIVE should admit next, then BACKGROUND.
    can_release_running.set()

    for t in (t_hold, t_bg, t_int):
        t.join(timeout=3.0)
        assert not t.is_alive()

    # hold-BG admits first (was alone); then INTERACTIVE (priority); then BG.
    assert admit_log[0] == "hold-BG"
    assert admit_log[1] == "queued-INT", (
        f"INTERACTIVE should have admitted before queued BACKGROUND; "
        f"got order {admit_log}"
    )
    assert admit_log[2] == "queued-BG"


# ---------------------------------------------------------------------------
# Queue capacity
# ---------------------------------------------------------------------------


def test_queue_full_raises_synchronously_when_max_queued_exceeded():
    """The (max_queued + 1)-th ticket at the same priority should
    see ``QueueFull`` without waiting."""
    b = _make_broker(max_concurrent=1, max_queued=2)
    barrier_running = threading.Event()
    can_release = threading.Event()

    def _holder():
        with b.slot(profile="test"):
            barrier_running.set()
            can_release.wait(timeout=5.0)

    t_hold = threading.Thread(target=_holder)
    t_hold.start()
    assert barrier_running.wait(timeout=2.0)

    # Fill the queue to capacity (2 waiters).
    waiters: list[threading.Thread] = []
    for i in range(2):
        def _waiter(_i=i):
            try:
                with b.slot(profile="test", queue_wait_s=5.0):
                    pass
            except Exception:
                pass
        t = threading.Thread(target=_waiter)
        t.start()
        waiters.append(t)
        time.sleep(0.05)

    # The next ticket should raise QueueFull immediately.
    with pytest.raises(QueueFull) as excinfo:
        with b.slot(profile="test", queue_wait_s=5.0):
            pass
    assert excinfo.value.kind == "queue_full"
    assert "test" in str(excinfo.value)

    # Drain.
    can_release.set()
    for t in (t_hold, *waiters):
        t.join(timeout=3.0)

    # Metrics for the overflow ticket should reflect queue_full.
    metrics = b.snapshot_metrics()
    overflow = [m for m in metrics if m["status"] == "queue_full"]
    assert len(overflow) == 1


# ---------------------------------------------------------------------------
# Queue-wait timeout (distinct from inference timeout)
# ---------------------------------------------------------------------------


def test_queue_wait_timeout_raises_distinct_error_kind():
    """A ticket that can't admit within queue_wait_s should raise
    QueueWaitTimeout — NOT propagate as a generic failure or
    InferenceTimeout."""
    b = _make_broker(max_concurrent=1)
    barrier = threading.Event()
    release = threading.Event()

    def _holder():
        with b.slot(profile="test"):
            barrier.set()
            release.wait(timeout=5.0)

    t = threading.Thread(target=_holder)
    t.start()
    assert barrier.wait(timeout=2.0)

    # Short queue_wait_s — should time out.
    t0 = time.monotonic()
    with pytest.raises(QueueWaitTimeout) as excinfo:
        with b.slot(profile="test", queue_wait_s=0.2):
            pass
    elapsed = time.monotonic() - t0
    assert 0.15 < elapsed < 0.6, (
        f"Expected ~0.2s wait, got {elapsed:.2f}s"
    )
    assert excinfo.value.kind == "queue_wait_timeout"

    release.set()
    t.join(timeout=3.0)

    metrics = b.snapshot_metrics()
    kinds = [m["status"] for m in metrics]
    assert "queue_wait_timeout" in kinds


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_track_timestamps_and_latency_splits():
    b = _make_broker(max_concurrent=1)
    with b.slot(profile="test") as ticket:
        ticket.mark_started_http()
        time.sleep(0.02)
        ticket.mark_first_token()

    m = b.snapshot_metrics()[-1]
    # <= throughout: time.monotonic() resolution on Windows is ~16ms,
    # so adjacent calls inside the uncontended fast path can share a tick.
    assert m["queued_at"] <= m["admitted_at"] <= m["started_http_at"]
    assert m["started_http_at"] <= m["first_token_at"] <= m["finished_at"]
    assert m["queue_wait_ms"] is not None and m["queue_wait_ms"] >= 0
    assert m["service_time_ms"] is not None and m["service_time_ms"] >= 0
    assert m["total_latency_ms"] >= m["service_time_ms"]


def test_metrics_ring_buffer_caps_at_configured_size():
    b = _make_broker(max_concurrent=10)
    # Artificially shrink the ring for a faster test.
    b._METRICS_RING_SIZE = 5
    for _ in range(12):
        with b.slot(profile="test"):
            pass
    metrics = b.snapshot_metrics()
    assert len(metrics) == 5, (
        f"ring buffer should cap at 5, got {len(metrics)}"
    )


def test_exception_in_slot_body_recorded_as_error():
    b = _make_broker(max_concurrent=1)

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with b.slot(profile="test"):
            raise _Boom("kaboom")

    m = b.snapshot_metrics()[-1]
    assert m["status"] == "error"
    assert m["error_kind"] == "_Boom"
    assert "kaboom" in (m["error_detail"] or "")
    # Slot released — a follow-up admission is immediate.
    with b.slot(profile="test"):
        pass


# ---------------------------------------------------------------------------
# Reconfigure + auto-register
# ---------------------------------------------------------------------------


def test_reconfigure_bumping_max_concurrent_wakes_waiter():
    b = _make_broker(max_concurrent=1)
    started = threading.Event()
    release_first = threading.Event()
    second_admitted = threading.Event()

    def _first():
        with b.slot(profile="test"):
            started.set()
            release_first.wait(timeout=5.0)

    def _second():
        with b.slot(profile="test", queue_wait_s=5.0):
            second_admitted.set()

    t1 = threading.Thread(target=_first)
    t1.start()
    assert started.wait(timeout=2.0)

    t2 = threading.Thread(target=_second)
    t2.start()
    time.sleep(0.05)
    assert not second_admitted.is_set(), "second must be queued initially"

    # Bump capacity — second should now admit without first releasing.
    b.configure_profile(ProfileConfig(name="test", max_concurrent=2))
    assert second_admitted.wait(timeout=2.0), (
        "Reconfigure did not wake the queued waiter to re-check admission"
    )

    release_first.set()
    for t in (t1, t2):
        t.join(timeout=3.0)


def test_unknown_profile_auto_registers_with_defaults():
    b = LocalInferenceBroker()
    with b.slot(profile="never-configured", queue_wait_s=1.0):
        pass
    assert "never-configured" in b.profile_status()
    status = b.profile_status()["never-configured"]
    assert status["max_concurrent"] == 1  # default


# ---------------------------------------------------------------------------
# Profile status snapshot shape
# ---------------------------------------------------------------------------


def test_profile_status_reports_in_flight_and_waiting():
    b = _make_broker(max_concurrent=1)
    release = threading.Event()

    def _hold():
        with b.slot(profile="test"):
            release.wait(timeout=5.0)

    t = threading.Thread(target=_hold)
    t.start()
    time.sleep(0.05)

    # Kick off a waiter.
    def _wait():
        try:
            with b.slot(profile="test", queue_wait_s=5.0,
                        priority=Priority.INTERACTIVE):
                pass
        except Exception:
            pass

    w = threading.Thread(target=_wait)
    w.start()
    time.sleep(0.05)

    status = b.profile_status()["test"]
    assert status["in_flight"] == 1
    assert status["waiting"]["INTERACTIVE"] == 1
    assert status["max_concurrent"] == 1

    release.set()
    for th in (t, w):
        th.join(timeout=3.0)
