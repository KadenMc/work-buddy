"""Resilience-framework adapter for the LocalInferenceBroker.

Presents a broker slot acquisition + inference call as a single resilience
*guarded call*: the broker's queue-wait and inference budgets are clamped to
the propagating Deadline, broker errors are mapped onto the outcome
taxonomy, and the unified ``guard.*`` telemetry is emitted.

The broker's scheduling internals are untouched — this is the "participate,
don't rewrite" adapter (DESIGN §6, stage A2). The broker's ``SlotMetrics``
ring remains a separate, finer-grained record; unifying it with the
resilience metrics surface is deferred to stage B2 (DESIGN §15).

Import direction: this module lives under ``work_buddy.inference`` and
depends on ``work_buddy.resilience`` (the foundation), never the reverse.
"""

from __future__ import annotations

import asyncio
from typing import Callable, TypeVar

from work_buddy.inference.broker import (
    BrokerError,
    InferenceTimeout,
    LocalInferenceBroker,
    Priority,
    QueueFull,
    QueueWaitTimeout,
    Ticket,
    get_broker,
)
from work_buddy.resilience import (
    Deadline,
    Outcome,
    OutcomeKind,
    current_context,
    default_classify,
    guarded_call,
)

T = TypeVar("T")

_INF = float("inf")


def classify_broker_error(exc: BaseException) -> OutcomeKind:
    """Map a broker exception onto the outcome taxonomy.

    ``QueueFull`` / ``QueueWaitTimeout`` → ``REJECTED`` (the call was shed;
    it never reached the dependency). ``InferenceTimeout`` → ``TIMEOUT``.
    Any other ``BrokerError`` → ``TRANSIENT_FAILURE``. Non-broker exceptions
    fall through to the framework default classifier.
    """
    if isinstance(exc, (QueueFull, QueueWaitTimeout)):
        return OutcomeKind.REJECTED
    if isinstance(exc, InferenceTimeout):
        return OutcomeKind.TIMEOUT
    if isinstance(exc, BrokerError):
        return OutcomeKind.TRANSIENT_FAILURE
    return default_classify(exc)


def _clamp_budget(deadline: Deadline, requested: float | None) -> float | None:
    """Clamp a broker budget to the deadline's remaining time.

    An unbounded deadline passes ``requested`` through untouched (``None``
    stays ``None``, so the broker applies its own profile default). A
    bounded deadline clamps to the remaining budget — and when ``requested``
    is ``None``, the remaining budget *becomes* the effective value.
    """
    remaining = deadline.remaining()
    if remaining == _INF:
        return requested
    if requested is None:
        return max(remaining, 0.0)
    return max(min(requested, remaining), 0.0)


async def guarded_broker_call(
    fn: Callable[[Ticket], T],
    *,
    profile: str,
    priority: Priority = Priority.WORKFLOW,
    queue_wait_s: float | None = None,
    inference_s: float | None = None,
    deadline: Deadline | None = None,
    operation_key: str | None = None,
    broker: LocalInferenceBroker | None = None,
) -> Outcome:
    """Run ``fn`` inside a broker slot as a resilience guarded call.

    ``fn`` is a *synchronous* callable receiving the broker :class:`Ticket`.
    It runs in a worker thread, so the broker's queue wait never blocks the
    event loop; the resilience context is snapshotted into that thread by
    ``asyncio.to_thread``. ``ticket.inference_s`` is re-clamped to the
    deadline-aware budget *after* admission, so ``fn`` can read it to bound
    its own HTTP timeout (the broker's documented contract).

    Returns an :class:`Outcome`; broker failures are classified onto the
    taxonomy, not raised. A ``CallCompleted`` event is emitted to the
    unified telemetry surface.
    """
    active_broker = broker if broker is not None else get_broker()
    op_key = operation_key or f"inference:{profile}"

    def _run_in_slot() -> T:
        # current_context() is the guarded_call context, snapshotted into
        # this worker thread by asyncio.to_thread.
        ctx = current_context()
        dl = ctx.deadline if ctx is not None else Deadline.never()
        eff_queue_wait = _clamp_budget(dl, queue_wait_s)
        with active_broker.slot(
            profile=profile,
            priority=priority,
            queue_wait_s=eff_queue_wait,
            inference_s=inference_s,
        ) as ticket:
            # Post-admission: re-clamp the inference budget to what is
            # actually left now — the queue wait may have consumed some.
            ticket.inference_s = _clamp_budget(  # type: ignore[assignment]
                dl, ticket.inference_s,
            )
            return fn(ticket)

    async def _guarded() -> T:
        return await asyncio.to_thread(_run_in_slot)

    return await guarded_call(
        op_key,
        _guarded,
        deadline=deadline,
        classify=classify_broker_error,
    )
