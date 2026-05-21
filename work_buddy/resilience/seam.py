"""The execution seam — the strategy contract and the guarded-call runner.

The seam is the callback-wrapper shape every resilience strategy implements:
a strategy receives the next-in-chain as ``nxt``, decides whether and when to
call it, and always returns an ``Outcome``.

In this stage there are no concrete strategies yet. The runner executes a
call through an (empty) strategy chain, classifies the result into the
outcome taxonomy, emits telemetry, and returns an ``Outcome``. Stage B1 adds
the concrete strategies; the runner does not change — strategies slot into
the chain it already accepts.

See ``.data/designs/resilience-framework/DESIGN.md`` §5, §7.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Awaitable, Callable, Protocol, TypeVar, Union, runtime_checkable

from work_buddy.resilience.context import (
    ResilienceContext,
    current_context,
    use_context,
)
from work_buddy.resilience.deadline import Deadline
from work_buddy.resilience.outcome import Outcome, OutcomeKind
from work_buddy.resilience.telemetry import CallCompleted, emit

T = TypeVar("T")

#: The next-in-chain callback a strategy receives.
GuardedFn = Callable[[ResilienceContext], "Awaitable[Outcome]"]

#: Maps an exception raised by the guarded callable to an outcome kind.
Classifier = Callable[[BaseException], OutcomeKind]


@runtime_checkable
class Strategy(Protocol):
    """One composable resilience primitive.

    A strategy receives the next-in-chain as ``nxt``, decides whether and
    when to call it, and ALWAYS returns an ``Outcome`` — it never raises to
    signal a policy decision. Concrete strategies (Timeout, Retry,
    CircuitBreaker, ...) arrive in stage B1.
    """

    async def execute(
        self, nxt: GuardedFn, ctx: ResilienceContext,
    ) -> Outcome: ...


def default_classify(exc: BaseException) -> OutcomeKind:
    """Fallback exception classifier.

    ``TimeoutError`` / ``asyncio.TimeoutError`` → ``TIMEOUT``; anything else
    → ``TRANSIENT_FAILURE``. Domain adapters (stages A2/A3) pass their own
    classifier that knows broker / Obsidian error types.
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return OutcomeKind.TIMEOUT
    return OutcomeKind.TRANSIENT_FAILURE


async def _invoke(fn: Callable[[], Union[T, Awaitable[T]]]) -> T:
    """Call ``fn`` and await the result if it is awaitable."""
    result = fn()
    if inspect.isawaitable(result):
        return await result
    return result  # type: ignore[return-value]


def _wrap(strategy: Strategy, nxt: GuardedFn) -> GuardedFn:
    async def _wrapped(ctx: ResilienceContext) -> Outcome:
        return await strategy.execute(nxt, ctx)

    return _wrapped


async def guarded_call(
    operation_key: str,
    fn: Callable[[], Union[T, Awaitable[T]]],
    *,
    deadline: Deadline | None = None,
    strategies: list[Strategy] | None = None,
    classify: Classifier = default_classify,
) -> Outcome:
    """Run ``fn`` as a guarded call and return its ``Outcome``.

    Builds — or derives, when already inside a guarded call — a
    ``ResilienceContext``, runs ``fn`` through the strategy chain, classifies
    any exception via ``classify``, emits a ``CallCompleted`` event, and
    returns an ``Outcome``.

    Never raises for a failure of ``fn``: the failure is carried in the
    returned ``Outcome``. (A bug *inside a strategy* may still raise — that
    is not a policy decision and is not swallowed.)
    """
    parent = current_context()
    if parent is not None:
        ctx = parent.derive_child(
            operation_key=operation_key, deadline=deadline,
        )
    else:
        ctx = ResilienceContext(
            operation_key=operation_key,
            deadline=deadline or Deadline.never(),
        )

    # Innermost link: deadline-gate, run fn, wrap the result in an Outcome.
    async def _base(c: ResilienceContext) -> Outcome:
        if c.deadline.expired():
            return Outcome.failure(
                OutcomeKind.REJECTED,
                detail="deadline already expired before the call started",
            )
        try:
            value = await _invoke(fn)
        except Exception as exc:  # noqa: BLE001 - classified, not swallowed
            return Outcome.failure(
                classify(exc),
                error=exc,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return Outcome.success(value)

    # Wrap with strategies, innermost-last. Empty in this stage; B1 populates.
    chain: GuardedFn = _base
    for strategy in reversed(strategies or []):
        chain = _wrap(strategy, chain)

    t0 = time.monotonic()
    with use_context(ctx):
        outcome = await chain(ctx)
    duration = time.monotonic() - t0

    emit(CallCompleted(
        operation_key=ctx.operation_key,
        call_id=ctx.call_id,
        parent_call_id=ctx.parent_call_id,
        duration_s=duration,
        outcome=outcome.kind,
        error_type=(
            type(outcome.error).__name__ if outcome.error is not None
            else None
        ),
    ))
    return outcome


def guarded_call_sync(
    operation_key: str,
    fn: Callable[[], T],
    *,
    deadline: Deadline | None = None,
    strategies: list[Strategy] | None = None,
    classify: Classifier = default_classify,
) -> Outcome:
    """Synchronous entry point for callers with no running event loop.

    Raises ``RuntimeError`` if called from within a running loop — use
    ``await guarded_call(...)`` there instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "guarded_call_sync() called from within a running event loop; "
            "use 'await guarded_call(...)' instead."
        )
    return asyncio.run(guarded_call(
        operation_key, fn,
        deadline=deadline, strategies=strategies, classify=classify,
    ))
