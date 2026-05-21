"""Tests for the resilience-framework spine (stage A1).

Covers the outcome taxonomy, the Deadline, the ResilienceContext + its
ContextVar carrier, the telemetry surface, and the execution seam /
guarded-call runner. All new code; touches no existing system.

Async tests wrap their body in ``asyncio.run`` rather than relying on a
pytest-asyncio plugin/config, so the file is self-contained.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from work_buddy.resilience import (
    CallCompleted,
    Deadline,
    InMemoryMetrics,
    Outcome,
    OutcomeError,
    OutcomeKind,
    ResilienceContext,
    TypedKey,
    current_context,
    default_classify,
    guarded_call,
    guarded_call_sync,
    register_listener,
    use_context,
)
from work_buddy.resilience.telemetry import GuardEvent, emit, _reset_listeners_for_tests


@pytest.fixture(autouse=True)
def _reset_listeners():
    _reset_listeners_for_tests()
    yield
    _reset_listeners_for_tests()


# ===========================================================================
# Outcome taxonomy
# ===========================================================================


def test_outcome_kind_classification():
    assert OutcomeKind.SUCCESS.is_success
    assert not OutcomeKind.TIMEOUT.is_success

    # is_failure: the three failure kinds only.
    assert OutcomeKind.TIMEOUT.is_failure
    assert OutcomeKind.TRANSIENT_FAILURE.is_failure
    assert OutcomeKind.TERMINAL_FAILURE.is_failure
    assert not OutcomeKind.REJECTED.is_failure  # shed, never ran
    assert not OutcomeKind.PARTIAL.is_failure   # qualified success
    assert not OutcomeKind.SUCCESS.is_failure

    # is_retryable: timeout + transient only.
    assert OutcomeKind.TIMEOUT.is_retryable
    assert OutcomeKind.TRANSIENT_FAILURE.is_retryable
    assert not OutcomeKind.TERMINAL_FAILURE.is_retryable
    assert not OutcomeKind.REJECTED.is_retryable

    # counts_toward_circuit_trip: timeout + transient only.
    assert OutcomeKind.TIMEOUT.counts_toward_circuit_trip
    assert OutcomeKind.TRANSIENT_FAILURE.counts_toward_circuit_trip
    assert not OutcomeKind.TERMINAL_FAILURE.counts_toward_circuit_trip
    assert not OutcomeKind.REJECTED.counts_toward_circuit_trip


def test_outcome_success_and_unwrap():
    o = Outcome.success(42)
    assert o.is_success
    assert o.kind is OutcomeKind.SUCCESS
    assert o.unwrap() == 42

    # None is a legitimate success value.
    n = Outcome.success(None)
    assert n.is_success
    assert n.unwrap() is None


def test_outcome_failure_unwrap_reraises_error():
    err = ValueError("boom")
    o = Outcome.failure(OutcomeKind.TRANSIENT_FAILURE, error=err, detail="d")
    assert not o.is_success
    assert o.error is err
    with pytest.raises(ValueError, match="boom"):
        o.unwrap()


def test_outcome_failure_without_error_raises_outcome_error():
    o = Outcome.failure(OutcomeKind.REJECTED, detail="shed")
    with pytest.raises(OutcomeError) as ei:
        o.unwrap()
    assert ei.value.kind is OutcomeKind.REJECTED


def test_outcome_failure_rejects_success_kind():
    with pytest.raises(ValueError):
        Outcome.failure(OutcomeKind.SUCCESS)


# ===========================================================================
# Deadline
# ===========================================================================


def test_deadline_after_and_remaining():
    d = Deadline.after(10.0)
    assert 9.0 < d.remaining() <= 10.0
    assert not d.expired()


def test_deadline_expired():
    d = Deadline.after(-1.0)
    assert d.expired()
    assert d.remaining() < 0


def test_deadline_never_is_unbounded():
    d = Deadline.never()
    assert d.remaining() == float("inf")
    assert not d.expired()
    assert d.clamp(None) == float("inf")
    assert d.clamp(30.0) == 30.0  # a finite local timeout wins


def test_deadline_clamp():
    d = Deadline.after(100.0)
    # local timeout smaller than remaining -> local wins
    assert d.clamp(5.0) == pytest.approx(5.0, abs=0.1)
    # local timeout larger than remaining -> remaining wins
    assert d.clamp(500.0) == pytest.approx(d.remaining(), abs=0.1)
    # expired deadline -> floored at 0
    assert Deadline.after(-1.0).clamp(10.0) == 0.0


def test_deadline_derive_attempt_never_extends_parent():
    parent = Deadline.after(100.0)
    # small per-attempt budget -> the attempt deadline is tighter
    short = parent.derive_attempt(5.0)
    assert short.at < parent.at
    # huge per-attempt budget -> clamped to the parent, never beyond
    long = parent.derive_attempt(5000.0)
    assert long.at == parent.at


# ===========================================================================
# ResilienceContext
# ===========================================================================


def test_context_typed_property_bag():
    key: TypedKey[int] = TypedKey("attempt")
    ctx = ResilienceContext(operation_key="op", deadline=Deadline.never())
    assert ctx.get(key) is None
    assert ctx.get(key, 7) == 7
    ctx.set(key, 3)
    assert ctx.get(key) == 3


def test_context_derive_child_links_parent_and_propagates_deadline():
    dl = Deadline.after(50.0)
    parent = ResilienceContext(operation_key="parent", deadline=dl)
    key: TypedKey[str] = TypedKey("k")
    parent.set(key, "inherited")

    child = parent.derive_child(operation_key="child")
    assert child.operation_key == "child"
    assert child.parent_call_id == parent.call_id
    assert child.call_id != parent.call_id
    assert child.deadline is dl  # propagated unchanged
    assert child.get(key) == "inherited"  # properties shallow-copied

    # child writes do not leak upward
    child.set(key, "mutated")
    assert parent.get(key) == "inherited"


def test_context_derive_child_can_tighten_deadline():
    parent = ResilienceContext(operation_key="p", deadline=Deadline.after(99))
    tighter = Deadline.after(1.0)
    child = parent.derive_child(deadline=tighter)
    assert child.deadline is tighter


def test_current_context_and_use_context():
    assert current_context() is None
    ctx = ResilienceContext(operation_key="op", deadline=Deadline.never())
    with use_context(ctx):
        assert current_context() is ctx
    assert current_context() is None  # reset on exit


def test_context_survives_copy_context():
    """copy_context() is the mechanism asyncio.to_thread uses to carry the
    context into a worker thread — the deadline must survive that copy."""
    ctx = ResilienceContext(operation_key="op", deadline=Deadline.never())
    with use_context(ctx):
        snapshot = contextvars.copy_context()
    # Run a reader inside the snapshotted context (as a worker thread would).
    seen = snapshot.run(current_context)
    assert seen is ctx


# ===========================================================================
# Telemetry surface
# ===========================================================================


def test_in_memory_metrics_records_call_completed():
    m = InMemoryMetrics()
    m.on_event(CallCompleted(
        operation_key="op", call_id="c1", duration_s=0.5,
        outcome=OutcomeKind.SUCCESS,
    ))
    m.on_event(CallCompleted(
        operation_key="op", call_id="c2", duration_s=1.5,
        outcome=OutcomeKind.TIMEOUT, error_type="TimeoutError",
    ))
    snap = m.snapshot()
    assert snap["call_count"] == 2
    assert snap["counts_by_operation_outcome"]["op/success"] == 1
    assert snap["counts_by_operation_outcome"]["op/timeout"] == 1
    assert snap["duration_s"]["min"] == 0.5
    assert snap["duration_s"]["max"] == 1.5
    assert snap["duration_s"]["mean"] == 1.0


def test_in_memory_metrics_ignores_non_call_events():
    m = InMemoryMetrics()
    m.on_event(GuardEvent(operation_key="op", call_id="c1"))
    assert m.snapshot()["call_count"] == 0


def test_in_memory_metrics_ring_caps_and_reset():
    m = InMemoryMetrics(ring_size=3)
    for i in range(10):
        m.on_event(CallCompleted(
            operation_key="op", call_id=f"c{i}", duration_s=0.1,
            outcome=OutcomeKind.SUCCESS,
        ))
    assert m.snapshot()["call_count"] == 3
    m.reset()
    assert m.snapshot()["call_count"] == 0


def test_emit_delivers_to_registered_listeners():
    m = InMemoryMetrics()
    register_listener(m)
    emit(CallCompleted(
        operation_key="op", call_id="c1", duration_s=0.2,
        outcome=OutcomeKind.SUCCESS,
    ))
    assert m.snapshot()["call_count"] == 1


def test_emit_isolates_a_misbehaving_listener():
    class _Bad:
        def on_event(self, event):
            raise RuntimeError("listener bug")

    good = InMemoryMetrics()
    register_listener(_Bad())
    register_listener(good)
    # emit must not propagate the bad listener's exception.
    emit(CallCompleted(
        operation_key="op", call_id="c1", duration_s=0.1,
        outcome=OutcomeKind.SUCCESS,
    ))
    assert good.snapshot()["call_count"] == 1


# ===========================================================================
# Execution seam — guarded_call
# ===========================================================================


def test_guarded_call_success():
    outcome = asyncio.run(guarded_call("op", lambda: 99))
    assert outcome.is_success
    assert outcome.unwrap() == 99


def test_guarded_call_classifies_failure_without_raising():
    def _boom():
        raise ValueError("nope")

    outcome = asyncio.run(guarded_call("op", _boom))
    assert not outcome.is_success
    assert outcome.kind is OutcomeKind.TRANSIENT_FAILURE
    assert isinstance(outcome.error, ValueError)


def test_guarded_call_timeout_classification():
    def _slow():
        raise TimeoutError("too slow")

    outcome = asyncio.run(guarded_call("op", _slow))
    assert outcome.kind is OutcomeKind.TIMEOUT


def test_guarded_call_custom_classifier():
    def _boom():
        raise ValueError("x")

    outcome = asyncio.run(guarded_call(
        "op", _boom, classify=lambda exc: OutcomeKind.TERMINAL_FAILURE,
    ))
    assert outcome.kind is OutcomeKind.TERMINAL_FAILURE


def test_guarded_call_rejects_when_deadline_already_expired():
    ran = {"called": False}

    def _fn():
        ran["called"] = True
        return 1

    outcome = asyncio.run(guarded_call(
        "op", _fn, deadline=Deadline.after(-1.0),
    ))
    assert outcome.kind is OutcomeKind.REJECTED
    assert ran["called"] is False  # fn never ran


def test_guarded_call_supports_async_fn():
    async def _afn():
        return "async-result"

    outcome = asyncio.run(guarded_call("op", _afn))
    assert outcome.unwrap() == "async-result"


def test_guarded_call_emits_call_completed():
    m = InMemoryMetrics()
    register_listener(m)
    asyncio.run(guarded_call("op", lambda: 1))
    snap = m.snapshot()
    assert snap["call_count"] == 1
    assert snap["counts_by_operation_outcome"]["op/success"] == 1


def test_guarded_call_nesting_links_contexts():
    captured: dict = {}

    async def _outer():
        async def _inner_fn():
            captured["inner_ctx"] = current_context()
            return "ok"

        await guarded_call("inner_op", _inner_fn, deadline=None)
        captured["outer_ctx"] = current_context()
        return "outer-done"

    asyncio.run(guarded_call(
        "outer_op", _outer, deadline=Deadline.after(50.0),
    ))

    outer_ctx = captured["outer_ctx"]
    inner_ctx = captured["inner_ctx"]
    assert outer_ctx.operation_key == "outer_op"
    assert inner_ctx.operation_key == "inner_op"
    # the inner call is a child of the outer call
    assert inner_ctx.parent_call_id == outer_ctx.call_id
    # the deadline propagated into the nested call
    assert inner_ctx.deadline is outer_ctx.deadline


# ===========================================================================
# Strategy composition (no concrete strategies yet — verify the chain works)
# ===========================================================================


def test_strategy_chain_invokes_pass_through_strategy():
    class _Recording:
        def __init__(self):
            self.called = False

        async def execute(self, nxt, ctx):
            self.called = True
            return await nxt(ctx)

    strat = _Recording()
    outcome = asyncio.run(guarded_call("op", lambda: 5, strategies=[strat]))
    assert strat.called
    assert outcome.unwrap() == 5


def test_strategy_can_short_circuit_before_fn_runs():
    ran = {"called": False}

    class _ShortCircuit:
        async def execute(self, nxt, ctx):
            return Outcome.failure(OutcomeKind.REJECTED, detail="shorted")

    def _fn():
        ran["called"] = True
        return 1

    outcome = asyncio.run(guarded_call(
        "op", _fn, strategies=[_ShortCircuit()],
    ))
    assert outcome.kind is OutcomeKind.REJECTED
    assert ran["called"] is False  # strategy never called nxt


def test_strategy_chain_executes_outermost_first():
    order: list[str] = []

    class _Marker:
        def __init__(self, name):
            self.name = name

        async def execute(self, nxt, ctx):
            order.append(f"enter:{self.name}")
            result = await nxt(ctx)
            order.append(f"exit:{self.name}")
            return result

    asyncio.run(guarded_call(
        "op", lambda: 1, strategies=[_Marker("A"), _Marker("B")],
    ))
    # A is declared first => outermost => enters first, exits last.
    assert order == ["enter:A", "enter:B", "exit:B", "exit:A"]


# ===========================================================================
# Sync entry point
# ===========================================================================


def test_guarded_call_sync_runs_without_a_loop():
    outcome = guarded_call_sync("op", lambda: 7)
    assert outcome.unwrap() == 7


def test_guarded_call_sync_raises_inside_a_running_loop():
    async def _inner():
        with pytest.raises(RuntimeError, match="running event loop"):
            guarded_call_sync("op", lambda: 1)

    asyncio.run(_inner())


def test_default_classify():
    assert default_classify(TimeoutError()) is OutcomeKind.TIMEOUT
    assert default_classify(asyncio.TimeoutError()) is OutcomeKind.TIMEOUT
    assert default_classify(ValueError()) is OutcomeKind.TRANSIENT_FAILURE
