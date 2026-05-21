"""Tests for the resilience strategy library (stage B1).

Each strategy is exercised in isolation against a controllable ``nxt``
(next-in-chain). Async bodies are wrapped in ``asyncio.run`` so the file is
self-contained.
"""

from __future__ import annotations

import asyncio

import pytest

from work_buddy.resilience import (
    Deadline,
    InMemoryMetrics,
    Outcome,
    OutcomeKind,
    ResilienceContext,
    register_listener,
)
from work_buddy.resilience.strategies import (
    BulkheadStrategy,
    CircuitBreakerStrategy,
    CircuitState,
    FallbackStrategy,
    RateLimiterStrategy,
    RetryStrategy,
    TimeoutStrategy,
)
from work_buddy.resilience.telemetry import _reset_listeners_for_tests


@pytest.fixture(autouse=True)
def _reset_listeners():
    _reset_listeners_for_tests()
    yield
    _reset_listeners_for_tests()


def _ctx(deadline: Deadline | None = None) -> ResilienceContext:
    return ResilienceContext(
        operation_key="t", deadline=deadline or Deadline.never(),
    )


class _CountingNxt:
    """A next-in-chain that counts calls and returns scripted outcomes.

    ``outcomes`` may be a single Outcome (always returned) or a list (the
    n-th call returns the n-th entry; the last entry repeats).
    """

    def __init__(self, outcomes):
        self._outcomes = outcomes
        self.calls = 0

    async def __call__(self, ctx):
        self.calls += 1
        if isinstance(self._outcomes, list):
            idx = min(self.calls - 1, len(self._outcomes) - 1)
            return self._outcomes[idx]
        return self._outcomes


# ===========================================================================
# TimeoutStrategy
# ===========================================================================


def test_timeout_passes_through_a_fast_call():
    strat = TimeoutStrategy(timeout_s=5.0)
    nxt = _CountingNxt(Outcome.success(42))
    outcome = asyncio.run(strat.execute(nxt, _ctx()))
    assert outcome.unwrap() == 42


def test_timeout_fires_on_a_slow_call():
    async def _slow(ctx):
        await asyncio.sleep(0.5)
        return Outcome.success(1)

    strat = TimeoutStrategy(timeout_s=0.05)
    outcome = asyncio.run(strat.execute(_slow, _ctx()))
    assert outcome.kind is OutcomeKind.TIMEOUT


def test_timeout_is_clamped_by_the_deadline():
    async def _slow(ctx):
        await asyncio.sleep(0.5)
        return Outcome.success(1)

    # configured 100s, but the deadline only allows ~0.05s
    strat = TimeoutStrategy(timeout_s=100.0)
    outcome = asyncio.run(strat.execute(_slow, _ctx(Deadline.after(0.05))))
    assert outcome.kind is OutcomeKind.TIMEOUT


def test_timeout_rejects_when_no_budget_remains():
    nxt = _CountingNxt(Outcome.success(1))
    strat = TimeoutStrategy(timeout_s=5.0)
    outcome = asyncio.run(strat.execute(nxt, _ctx(Deadline.after(-1.0))))
    assert outcome.kind is OutcomeKind.TIMEOUT
    assert nxt.calls == 0  # never invoked


# ===========================================================================
# RetryStrategy
# ===========================================================================


def test_retry_returns_first_success_without_retrying():
    nxt = _CountingNxt(Outcome.success("ok"))
    strat = RetryStrategy(max_attempts=3, base_delay_s=0.01)
    outcome = asyncio.run(strat.execute(nxt, _ctx()))
    assert outcome.is_success
    assert nxt.calls == 1


def test_retry_recovers_after_transient_failures():
    nxt = _CountingNxt([
        Outcome.failure(OutcomeKind.TRANSIENT_FAILURE),
        Outcome.failure(OutcomeKind.TRANSIENT_FAILURE),
        Outcome.success("recovered"),
    ])
    strat = RetryStrategy(max_attempts=3, base_delay_s=0.01)
    outcome = asyncio.run(strat.execute(nxt, _ctx()))
    assert outcome.is_success
    assert nxt.calls == 3


def test_retry_exhausts_at_max_attempts():
    nxt = _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE))
    strat = RetryStrategy(max_attempts=3, base_delay_s=0.01)
    outcome = asyncio.run(strat.execute(nxt, _ctx()))
    assert outcome.kind is OutcomeKind.TRANSIENT_FAILURE
    assert nxt.calls == 3


def test_retry_does_not_retry_terminal_failure():
    nxt = _CountingNxt(Outcome.failure(OutcomeKind.TERMINAL_FAILURE))
    strat = RetryStrategy(max_attempts=5, base_delay_s=0.01)
    outcome = asyncio.run(strat.execute(nxt, _ctx()))
    assert outcome.kind is OutcomeKind.TERMINAL_FAILURE
    assert nxt.calls == 1


def test_retry_does_not_retry_rejected():
    nxt = _CountingNxt(Outcome.failure(OutcomeKind.REJECTED))
    strat = RetryStrategy(max_attempts=5, base_delay_s=0.01)
    outcome = asyncio.run(strat.execute(nxt, _ctx()))
    assert nxt.calls == 1


def test_retry_stops_when_deadline_expired():
    nxt = _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE))
    strat = RetryStrategy(max_attempts=10, base_delay_s=0.01)
    # deadline already gone -> first call happens, then no retry
    outcome = asyncio.run(strat.execute(nxt, _ctx(Deadline.after(-1.0))))
    assert nxt.calls == 1
    assert not outcome.is_success


# ===========================================================================
# CircuitBreakerStrategy
# ===========================================================================


def test_circuit_opens_after_threshold_failures():
    cb = CircuitBreakerStrategy(failure_threshold=3, reset_timeout_s=10.0)
    nxt = _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE))

    async def _drive():
        for _ in range(3):
            await cb.execute(nxt, _ctx())

    asyncio.run(_drive())
    assert cb.state is CircuitState.OPEN


def test_open_circuit_sheds_without_calling_nxt():
    cb = CircuitBreakerStrategy(failure_threshold=1, reset_timeout_s=10.0)
    fail = _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE))
    asyncio.run(cb.execute(fail, _ctx()))  # trips it
    assert cb.state is CircuitState.OPEN

    shed_nxt = _CountingNxt(Outcome.success(1))
    outcome = asyncio.run(cb.execute(shed_nxt, _ctx()))
    assert outcome.kind is OutcomeKind.REJECTED
    assert shed_nxt.calls == 0  # the inner chain was never invoked


def test_circuit_half_open_probe_success_closes():
    cb = CircuitBreakerStrategy(failure_threshold=1, reset_timeout_s=0.05)
    asyncio.run(cb.execute(
        _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE)), _ctx(),
    ))
    assert cb.state is CircuitState.OPEN

    async def _probe():
        await asyncio.sleep(0.07)  # let the cooldown elapse
        return await cb.execute(_CountingNxt(Outcome.success("up")), _ctx())

    outcome = asyncio.run(_probe())
    assert outcome.is_success
    assert cb.state is CircuitState.CLOSED


def test_circuit_half_open_probe_failure_reopens():
    cb = CircuitBreakerStrategy(failure_threshold=1, reset_timeout_s=0.05)
    asyncio.run(cb.execute(
        _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE)), _ctx(),
    ))

    async def _probe():
        await asyncio.sleep(0.07)
        return await cb.execute(
            _CountingNxt(Outcome.failure(OutcomeKind.TIMEOUT)), _ctx(),
        )

    asyncio.run(_probe())
    assert cb.state is CircuitState.OPEN


def test_circuit_success_resets_consecutive_count():
    cb = CircuitBreakerStrategy(failure_threshold=3, reset_timeout_s=10.0)

    async def _drive():
        await cb.execute(
            _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE)),
            _ctx(),
        )
        await cb.execute(
            _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE)),
            _ctx(),
        )
        # a success resets the streak ...
        await cb.execute(_CountingNxt(Outcome.success(1)), _ctx())
        # ... so two more failures do not reach the threshold of 3
        await cb.execute(
            _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE)),
            _ctx(),
        )
        await cb.execute(
            _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE)),
            _ctx(),
        )

    asyncio.run(_drive())
    assert cb.state is CircuitState.CLOSED


def test_circuit_terminal_failure_does_not_trip():
    cb = CircuitBreakerStrategy(failure_threshold=2, reset_timeout_s=10.0)

    async def _drive():
        for _ in range(5):
            await cb.execute(
                _CountingNxt(Outcome.failure(OutcomeKind.TERMINAL_FAILURE)),
                _ctx(),
            )

    asyncio.run(_drive())
    # terminal failures are not dependency-unhealth — they must not trip it
    assert cb.state is CircuitState.CLOSED


def test_circuit_emits_state_change_telemetry():
    m = InMemoryMetrics()
    register_listener(m)
    cb = CircuitBreakerStrategy(failure_threshold=1, reset_timeout_s=10.0)
    asyncio.run(cb.execute(
        _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE)), _ctx(),
    ))
    snap = m.snapshot()
    assert snap["circuit_transitions"] >= 1
    assert snap["shed_by_reason"] == {}  # no shed yet — only one call


# ===========================================================================
# BulkheadStrategy
# ===========================================================================


def test_bulkhead_serializes_beyond_max_concurrent():
    bh = BulkheadStrategy(max_concurrent=1)
    gate = asyncio.Event()
    order: list[str] = []

    async def _impl():
        async def _slow(ctx):
            order.append("enter")
            await gate.wait()
            order.append("exit")
            return Outcome.success(1)

        async def _fast(ctx):
            order.append("fast")
            return Outcome.success(2)

        t1 = asyncio.create_task(bh.execute(_slow, _ctx()))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(bh.execute(_fast, _ctx()))
        await asyncio.sleep(0.02)
        # the fast call is blocked behind the single slot
        assert order == ["enter"]
        gate.set()
        await asyncio.gather(t1, t2)

    asyncio.run(_impl())
    assert order == ["enter", "exit", "fast"]


def test_bulkhead_sheds_when_queue_full():
    bh = BulkheadStrategy(max_concurrent=1, max_queued=1)
    gate = asyncio.Event()

    async def _impl():
        async def _slow(ctx):
            await gate.wait()
            return Outcome.success(1)

        async def _waiter(ctx):
            return Outcome.success(2)

        t_hold = asyncio.create_task(bh.execute(_slow, _ctx()))
        await asyncio.sleep(0.02)              # t_hold occupies the slot
        t_wait = asyncio.create_task(bh.execute(_waiter, _ctx()))
        await asyncio.sleep(0.02)              # t_wait occupies the queue
        # the third call exceeds max_queued -> shed immediately
        shed = await bh.execute(_waiter, _ctx())
        assert shed.kind is OutcomeKind.REJECTED
        gate.set()
        await asyncio.gather(t_hold, t_wait)

    asyncio.run(_impl())


def test_bulkhead_sheds_on_slot_wait_timeout():
    bh = BulkheadStrategy(max_concurrent=1, max_queued=8)
    gate = asyncio.Event()

    async def _impl():
        async def _slow(ctx):
            await gate.wait()
            return Outcome.success(1)

        t_hold = asyncio.create_task(bh.execute(_slow, _ctx()))
        await asyncio.sleep(0.02)
        # a call with a short deadline cannot get the slot in time
        outcome = await bh.execute(
            _CountingNxt(Outcome.success(2)), _ctx(Deadline.after(0.05)),
        )
        assert outcome.kind is OutcomeKind.REJECTED
        gate.set()
        await t_hold

    asyncio.run(_impl())


# ===========================================================================
# RateLimiterStrategy
# ===========================================================================


def test_rate_limiter_allows_burst_then_sheds():
    rl = RateLimiterStrategy(rate_per_s=1.0, burst=2)
    nxt = _CountingNxt(Outcome.success(1))

    async def _impl():
        a = await rl.execute(nxt, _ctx())
        b = await rl.execute(nxt, _ctx())
        c = await rl.execute(nxt, _ctx())
        return a, b, c

    a, b, c = asyncio.run(_impl())
    assert a.is_success and b.is_success
    assert c.kind is OutcomeKind.REJECTED
    assert nxt.calls == 2  # the shed call never reached nxt


def test_rate_limiter_refills_over_time():
    rl = RateLimiterStrategy(rate_per_s=100.0, burst=1)
    nxt = _CountingNxt(Outcome.success(1))

    async def _impl():
        first = await rl.execute(nxt, _ctx())
        shed = await rl.execute(nxt, _ctx())
        await asyncio.sleep(0.05)  # 0.05s * 100/s = 5 tokens accrue (capped 1)
        after = await rl.execute(nxt, _ctx())
        return first, shed, after

    first, shed, after = asyncio.run(_impl())
    assert first.is_success
    assert shed.kind is OutcomeKind.REJECTED
    assert after.is_success


# ===========================================================================
# FallbackStrategy
# ===========================================================================


def test_fallback_passes_through_success():
    fb = FallbackStrategy(lambda: "fallback")
    outcome = asyncio.run(fb.execute(_CountingNxt(Outcome.success("real")), _ctx()))
    assert outcome.unwrap() == "real"


def test_fallback_substitutes_on_failure():
    fb = FallbackStrategy(lambda: "fallback-value")
    outcome = asyncio.run(fb.execute(
        _CountingNxt(Outcome.failure(OutcomeKind.TRANSIENT_FAILURE)), _ctx(),
    ))
    assert outcome.is_success
    assert outcome.unwrap() == "fallback-value"


def test_fallback_passes_through_rejected():
    """A shed call is not a fault to paper over — fallback must not fire."""
    fb = FallbackStrategy(lambda: "fallback")
    outcome = asyncio.run(fb.execute(
        _CountingNxt(Outcome.failure(OutcomeKind.REJECTED)), _ctx(),
    ))
    assert outcome.kind is OutcomeKind.REJECTED


def test_fallback_handles_its_own_failure():
    def _boom():
        raise RuntimeError("fallback itself failed")

    fb = FallbackStrategy(_boom)
    outcome = asyncio.run(fb.execute(
        _CountingNxt(Outcome.failure(OutcomeKind.TIMEOUT)), _ctx(),
    ))
    assert outcome.kind is OutcomeKind.TRANSIENT_FAILURE
    assert isinstance(outcome.error, RuntimeError)
