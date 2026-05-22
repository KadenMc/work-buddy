"""Resilience strategies — the composable fault-mitigation primitives.

Each strategy implements the ``ResilienceStrategy`` protocol (``seam.py``): it receives
the next-in-chain as ``nxt``, decides whether and when to call it, and
returns an ``Outcome``. Strategies compose into a pipeline in the canonical
order: overall Timeout → RateLimiter / Bulkhead → Retry → CircuitBreaker →
per-attempt Timeout → call.

Strategy instances hold mutable state (circuit-breaker counters, bulkhead
slots, rate-limiter tokens) and are reused across calls. They run in the
asyncio event loop — concurrent guarded calls interleave only at ``await``
points, so plain attribute mutation between awaits is safe without a lock.
The ``Bulkhead`` / ``RateLimiter`` strategies hold loop-bound state and
assume a single, stable event loop (the gateway's).

See ``.data/designs/resilience-framework/DESIGN.md`` §7.
"""

from __future__ import annotations

import asyncio
import enum
import random
import time
from typing import Callable, TypeVar

from work_buddy.resilience.context import ResilienceContext
from work_buddy.resilience.outcome import Outcome, OutcomeKind
from work_buddy.resilience.seam import GuardedFn
from work_buddy.resilience.telemetry import CircuitStateChanged, LoadShed, emit

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TimeoutStrategy:
    """Cap the wall-time of the inner call.

    The effective timeout is ``min(timeout_s, ctx.deadline.remaining())`` —
    the strategy never waits longer than the propagating deadline allows. On
    expiry it returns a ``TIMEOUT`` outcome.

    ``asyncio.timeout`` frees the event loop when it fires, but cannot kill
    synchronous work running in a worker thread — that work runs to
    completion in the background (DESIGN §8).
    """

    def __init__(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("TimeoutStrategy timeout_s must be positive")
        self.timeout_s = timeout_s

    async def execute(
        self, nxt: GuardedFn, ctx: ResilienceContext,
    ) -> Outcome:
        effective = ctx.deadline.clamp(self.timeout_s)
        if effective <= 0:
            return Outcome.failure(
                OutcomeKind.TIMEOUT,
                detail="no time budget remaining for the call",
            )
        try:
            async with asyncio.timeout(effective):
                return await nxt(ctx)
        except asyncio.TimeoutError:
            return Outcome.failure(
                OutcomeKind.TIMEOUT,
                detail=f"call exceeded {effective:.2f}s timeout",
            )


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class RetryStrategy:
    """Re-attempt the inner call on a retryable outcome.

    Retries on ``TIMEOUT`` / ``TRANSIENT_FAILURE`` (``OutcomeKind.is_retryable``),
    up to ``max_attempts`` total attempts, with exponential backoff + full
    jitter. Stops early when the propagating deadline would not accommodate
    another attempt. Does not retry ``REJECTED`` (shed) or ``TERMINAL_FAILURE``.

    Per the one-retry-layer rule, a pipeline must contain at most one
    ``RetryStrategy`` per failure domain.
    """

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay_s: float = 0.5,
        max_delay_s: float = 30.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter (AWS / Google SRE)."""
        ceiling = min(
            self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s,
        )
        return random.uniform(0.0, ceiling)

    async def execute(
        self, nxt: GuardedFn, ctx: ResilienceContext,
    ) -> Outcome:
        outcome = await nxt(ctx)
        attempt = 1
        while attempt < self.max_attempts and outcome.kind.is_retryable:
            if ctx.deadline.expired():
                break
            delay = self._backoff(attempt)
            if delay >= ctx.deadline.remaining():
                break  # the backoff alone would blow the budget
            await asyncio.sleep(delay)
            outcome = await nxt(ctx)
            attempt += 1
        return outcome


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerStrategy:
    """Stop calling a failing dependency until it has had time to recover.

    Counts consecutive trip-worthy outcomes (``OutcomeKind.counts_toward_
    circuit_trip`` — ``TIMEOUT`` / ``TRANSIENT_FAILURE``). After
    ``failure_threshold`` in a row the circuit OPENS and calls are shed
    (``REJECTED``) without invoking the inner chain. After ``reset_timeout_s``
    it goes HALF_OPEN and admits exactly one probe; a non-failure closes it,
    a trip-worthy failure re-opens it.

    State transitions emit ``CircuitStateChanged``; sheds emit ``LoadShed``.
    """

    def __init__(
        self,
        *,
        name: str = "circuit",
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        return self._state

    def _transition(self, new: CircuitState, ctx: ResilienceContext) -> None:
        if new is self._state:
            return
        old = self._state
        self._state = new
        emit(CircuitStateChanged(
            operation_key=ctx.operation_key,
            call_id=ctx.call_id,
            name=self.name,
            from_state=old.value,
            to_state=new.value,
        ))

    def _open(self, ctx: ResilienceContext) -> None:
        self._opened_at = time.monotonic()
        self._consecutive_failures = 0
        self._transition(CircuitState.OPEN, ctx)

    def _close(self, ctx: ResilienceContext) -> None:
        self._consecutive_failures = 0
        self._transition(CircuitState.CLOSED, ctx)

    def _shed(self, ctx: ResilienceContext, detail: str) -> Outcome:
        emit(LoadShed(
            operation_key=ctx.operation_key, call_id=ctx.call_id,
            name=self.name, reason="circuit_open",
        ))
        return Outcome.failure(OutcomeKind.REJECTED, detail=detail)

    async def execute(
        self, nxt: GuardedFn, ctx: ResilienceContext,
    ) -> Outcome:
        # OPEN: shed, unless the cooldown has elapsed -> move to HALF_OPEN.
        if self._state is CircuitState.OPEN:
            if time.monotonic() - self._opened_at < self.reset_timeout_s:
                return self._shed(ctx, f"circuit '{self.name}' is open")
            self._transition(CircuitState.HALF_OPEN, ctx)

        # HALF_OPEN: admit exactly one probe; shed the rest.
        if self._state is CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                return self._shed(
                    ctx, f"circuit '{self.name}' half-open; probe in flight",
                )
            self._probe_in_flight = True
            try:
                outcome = await nxt(ctx)
            finally:
                self._probe_in_flight = False
            if outcome.kind.counts_toward_circuit_trip:
                self._open(ctx)
            elif not outcome.kind.is_failure:
                self._close(ctx)
            return outcome

        # CLOSED.
        outcome = await nxt(ctx)
        if outcome.kind.counts_toward_circuit_trip:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._open(ctx)
        elif not outcome.kind.is_failure:
            self._consecutive_failures = 0
        return outcome


# ---------------------------------------------------------------------------
# Bulkhead
# ---------------------------------------------------------------------------


class BulkheadStrategy:
    """Limit concurrent calls to isolate a resource pool.

    Admits up to ``max_concurrent`` calls at once; up to ``max_queued`` more
    may wait for a slot. A call that would exceed the queue, or that cannot
    get a slot within the propagating deadline, is shed (``REJECTED``)
    without invoking the inner chain.

    Holds an ``asyncio.Semaphore`` — assumes a single, stable event loop.
    """

    def __init__(
        self,
        *,
        name: str = "bulkhead",
        max_concurrent: int = 1,
        max_queued: int = 32,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.name = name
        self.max_concurrent = max_concurrent
        self.max_queued = max_queued
        self._sem = asyncio.Semaphore(max_concurrent)
        self._waiting = 0

    def _shed(self, ctx: ResilienceContext, detail: str) -> Outcome:
        emit(LoadShed(
            operation_key=ctx.operation_key, call_id=ctx.call_id,
            name=self.name, reason="bulkhead_full",
        ))
        return Outcome.failure(OutcomeKind.REJECTED, detail=detail)

    async def execute(
        self, nxt: GuardedFn, ctx: ResilienceContext,
    ) -> Outcome:
        if self._waiting >= self.max_queued:
            return self._shed(
                ctx, f"bulkhead '{self.name}' queue is full",
            )

        budget = ctx.deadline.remaining()
        self._waiting += 1
        try:
            if budget == float("inf"):
                await self._sem.acquire()
            else:
                try:
                    await asyncio.wait_for(
                        self._sem.acquire(), timeout=max(budget, 0.0),
                    )
                except asyncio.TimeoutError:
                    return self._shed(
                        ctx, f"bulkhead '{self.name}' slot wait timed out",
                    )
        finally:
            self._waiting -= 1

        try:
            return await nxt(ctx)
        finally:
            self._sem.release()


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------


class RateLimiterStrategy:
    """Token-bucket rate limit. Sheds (``REJECTED``) when no token is free.

    ``rate_per_s`` tokens accrue per second, up to a ``burst`` ceiling. Each
    call consumes one token; a call with no token available is shed rather
    than queued.
    """

    def __init__(
        self,
        *,
        name: str = "rate_limiter",
        rate_per_s: float = 10.0,
        burst: int = 10,
    ) -> None:
        if rate_per_s <= 0 or burst < 1:
            raise ValueError("rate_per_s must be > 0 and burst >= 1")
        self.name = name
        self.rate_per_s = rate_per_s
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(
            float(self.burst), self._tokens + elapsed * self.rate_per_s,
        )

    async def execute(
        self, nxt: GuardedFn, ctx: ResilienceContext,
    ) -> Outcome:
        self._refill()
        if self._tokens < 1.0:
            emit(LoadShed(
                operation_key=ctx.operation_key, call_id=ctx.call_id,
                name=self.name, reason="rate_limited",
            ))
            return Outcome.failure(
                OutcomeKind.REJECTED,
                detail=f"rate limiter '{self.name}' — no token available",
            )
        self._tokens -= 1.0
        return await nxt(ctx)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


class FallbackStrategy:
    """Substitute a fallback value when the inner call fails.

    On any failure outcome (``OutcomeKind.is_failure`` — TIMEOUT / TRANSIENT /
    TERMINAL) invokes ``fallback`` and returns its value as a success.
    ``REJECTED`` (shed) and ``PARTIAL`` outcomes pass through unchanged — a
    shed call is not a fault to paper over.
    """

    def __init__(self, fallback: Callable[[], T]) -> None:
        self._fallback = fallback

    async def execute(
        self, nxt: GuardedFn, ctx: ResilienceContext,
    ) -> Outcome:
        outcome = await nxt(ctx)
        if not outcome.kind.is_failure:
            return outcome
        try:
            return Outcome.success(self._fallback(), detail="fallback value")
        except Exception as exc:  # noqa: BLE001 - fallback's own failure
            return Outcome.failure(
                OutcomeKind.TRANSIENT_FAILURE,
                error=exc,
                detail=f"fallback raised: {type(exc).__name__}: {exc}",
            )
