"""Tests for the broker resilience adapter (stage A2).

Exercises ``guarded_broker_call`` against a real ``LocalInferenceBroker``:
deadline clamping of the broker budgets, broker-error mapping onto the
outcome taxonomy, and ``guard.*`` telemetry emission. The broker's own
scheduling internals and metrics ring are not modified by the adapter.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from work_buddy.inference.broker import (
    InferenceTimeout,
    LocalInferenceBroker,
    Priority,
    ProfileConfig,
)
from work_buddy.inference.resilient_broker import (
    _clamp_budget,
    classify_broker_error,
    guarded_broker_call,
)
from work_buddy.resilience import Deadline, InMemoryMetrics, OutcomeKind, register_listener
from work_buddy.resilience.telemetry import _reset_listeners_for_tests


@pytest.fixture(autouse=True)
def _reset_listeners():
    _reset_listeners_for_tests()
    yield
    _reset_listeners_for_tests()


def _make_broker(**kwargs) -> LocalInferenceBroker:
    b = LocalInferenceBroker()
    b.configure_profile(ProfileConfig(name="test", **kwargs))
    return b


# ---------------------------------------------------------------------------
# classify_broker_error
# ---------------------------------------------------------------------------


def test_classify_broker_error_mapping():
    from work_buddy.inference.broker import QueueFull, QueueWaitTimeout

    assert classify_broker_error(
        QueueFull("x", kind="queue_full")
    ) is OutcomeKind.REJECTED
    assert classify_broker_error(
        QueueWaitTimeout("x", kind="queue_wait_timeout")
    ) is OutcomeKind.REJECTED
    assert classify_broker_error(
        InferenceTimeout("x", kind="inference_timeout")
    ) is OutcomeKind.TIMEOUT
    # A non-broker exception falls through to the framework default.
    assert classify_broker_error(ValueError("x")) is OutcomeKind.TRANSIENT_FAILURE
    assert classify_broker_error(TimeoutError()) is OutcomeKind.TIMEOUT


# ---------------------------------------------------------------------------
# _clamp_budget
# ---------------------------------------------------------------------------


def test_clamp_budget_unbounded_deadline_passes_through():
    never = Deadline.never()
    assert _clamp_budget(never, None) is None        # broker default preserved
    assert _clamp_budget(never, 60.0) == 60.0        # request preserved


def test_clamp_budget_bounded_deadline_clamps():
    dl = Deadline.after(10.0)
    # request larger than remaining -> clamped to remaining
    assert _clamp_budget(dl, 100.0) == pytest.approx(10.0, abs=0.2)
    # request smaller than remaining -> request preserved
    assert _clamp_budget(dl, 3.0) == pytest.approx(3.0, abs=0.2)
    # no request, bounded deadline -> the remaining budget IS the value
    assert _clamp_budget(dl, None) == pytest.approx(10.0, abs=0.2)
    # expired deadline -> floored at 0
    assert _clamp_budget(Deadline.after(-1.0), 5.0) == 0.0


# ---------------------------------------------------------------------------
# guarded_broker_call — happy path
# ---------------------------------------------------------------------------


def test_guarded_broker_call_success():
    b = _make_broker(max_concurrent=1)

    def _fn(ticket):
        ticket.mark_started_http()
        return "inference-result"

    outcome = asyncio.run(guarded_broker_call(
        _fn, profile="test", broker=b,
    ))
    assert outcome.is_success
    assert outcome.unwrap() == "inference-result"

    # The broker's own metrics ring still recorded the call.
    assert b.snapshot_metrics()[-1]["status"] == "ok"


def test_guarded_broker_call_emits_unified_telemetry():
    b = _make_broker(max_concurrent=1)
    metrics = InMemoryMetrics()
    register_listener(metrics)

    asyncio.run(guarded_broker_call(
        lambda t: 1, profile="test", operation_key="inference:test", broker=b,
    ))
    snap = metrics.snapshot()
    assert snap["call_count"] == 1
    assert snap["counts_by_operation_outcome"]["inference:test/success"] == 1


# ---------------------------------------------------------------------------
# Deadline clamping reaches the worker thread
# ---------------------------------------------------------------------------


def test_deadline_clamps_inference_budget_observed_by_fn():
    """The propagating deadline is clamped onto ``ticket.inference_s`` after
    admission. fn runs in a worker thread — so this also proves the
    resilience context propagated across the asyncio.to_thread boundary.
    """
    b = _make_broker(max_concurrent=1, default_inference_s=120.0)
    seen: dict = {}

    def _fn(ticket):
        seen["inference_s"] = ticket.inference_s
        return "ok"

    asyncio.run(guarded_broker_call(
        _fn, profile="test", deadline=Deadline.after(8.0), broker=b,
    ))
    # 120s profile default clamped down to the ~8s remaining budget.
    assert seen["inference_s"] == pytest.approx(8.0, abs=1.0)


def test_no_deadline_leaves_broker_default_inference_budget():
    b = _make_broker(max_concurrent=1, default_inference_s=55.0)
    seen: dict = {}

    def _fn(ticket):
        seen["inference_s"] = ticket.inference_s
        return "ok"

    asyncio.run(guarded_broker_call(_fn, profile="test", broker=b))
    # No deadline -> unbounded -> the broker's profile default is untouched.
    assert seen["inference_s"] == 55.0


# ---------------------------------------------------------------------------
# Broker-error mapping onto the taxonomy
# ---------------------------------------------------------------------------


def test_queue_wait_timeout_maps_to_rejected():
    """A ticket that can't get a slot within its (deadline-clamped) queue
    budget yields a REJECTED outcome — not a raised exception."""
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

    outcome = asyncio.run(guarded_broker_call(
        lambda tk: "never runs", profile="test", queue_wait_s=0.2, broker=b,
    ))
    release.set()
    t.join(timeout=3.0)

    assert not outcome.is_success
    assert outcome.kind is OutcomeKind.REJECTED


def test_inference_timeout_raised_in_fn_maps_to_timeout():
    b = _make_broker(max_concurrent=1)

    def _fn(ticket):
        raise InferenceTimeout(
            "downstream call exceeded budget", kind="inference_timeout",
        )

    outcome = asyncio.run(guarded_broker_call(
        _fn, profile="test", broker=b,
    ))
    assert not outcome.is_success
    assert outcome.kind is OutcomeKind.TIMEOUT
    assert isinstance(outcome.error, InferenceTimeout)


def test_generic_exception_in_fn_maps_to_transient_failure():
    b = _make_broker(max_concurrent=1)

    def _fn(ticket):
        raise ValueError("inference blew up")

    outcome = asyncio.run(guarded_broker_call(
        _fn, profile="test", broker=b,
    ))
    assert outcome.kind is OutcomeKind.TRANSIENT_FAILURE
    assert isinstance(outcome.error, ValueError)


def test_priority_is_forwarded_to_the_broker():
    """The adapter passes the priority through; an INTERACTIVE call lands
    on the broker as INTERACTIVE."""
    b = _make_broker(max_concurrent=1)
    asyncio.run(guarded_broker_call(
        lambda tk: 1, profile="test", priority=Priority.INTERACTIVE, broker=b,
    ))
    assert b.snapshot_metrics()[-1]["priority"] == "INTERACTIVE"
