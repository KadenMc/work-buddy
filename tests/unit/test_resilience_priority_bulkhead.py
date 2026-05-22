"""Tests for PriorityBulkheadStrategy and context-property seeding (stage B2).

PriorityBulkheadStrategy is the native-async, faithful port of the inference
broker's per-profile priority-admission algorithm. These tests pin the
behavior the broker's own suite pins (admission, priority ordering, queue
capacity, wait timeout) at the strategy level.
"""

from __future__ import annotations

import asyncio

import pytest

from work_buddy.resilience import (
    PRIORITY,
    Deadline,
    Outcome,
    OutcomeKind,
    Priority,
    PriorityBulkheadStrategy,
    ResilienceContext,
    ResiliencePipelineBuilder,
    current_context,
)
from work_buddy.resilience.telemetry import _reset_listeners_for_tests


@pytest.fixture(autouse=True)
def _reset_listeners():
    _reset_listeners_for_tests()
    yield
    _reset_listeners_for_tests()


def _ctx(
    priority: Priority | None = None, deadline: Deadline | None = None,
) -> ResilienceContext:
    c = ResilienceContext(
        operation_key="t", deadline=deadline or Deadline.never(),
    )
    if priority is not None:
        c.set(PRIORITY, priority)
    return c


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_priority_bulkhead_admits_when_idle():
    pb = PriorityBulkheadStrategy(max_concurrent=1)

    async def _nxt(ctx):
        return Outcome.success(42)

    outcome = asyncio.run(pb.execute(_nxt, _ctx()))
    assert outcome.unwrap() == 42


def test_priority_bulkhead_serializes_beyond_max_concurrent():
    pb = PriorityBulkheadStrategy(max_concurrent=1)
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

        t1 = asyncio.create_task(pb.execute(_slow, _ctx()))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(pb.execute(_fast, _ctx()))
        await asyncio.sleep(0.02)
        assert order == ["enter"]  # fast is blocked behind the single slot
        gate.set()
        await asyncio.gather(t1, t2)

    asyncio.run(_impl())
    assert order == ["enter", "exit", "fast"]


# ---------------------------------------------------------------------------
# Priority ordering — the defining behavior
# ---------------------------------------------------------------------------


def test_higher_priority_admits_ahead_of_queued_lower():
    """An INTERACTIVE call queued *after* a BACKGROUND call still admits
    first once the held slot frees — strict priority over FIFO."""
    pb = PriorityBulkheadStrategy(max_concurrent=1)
    gate = asyncio.Event()
    admit_order: list[str] = []

    async def _impl():
        async def _holder(ctx):
            admit_order.append("holder")
            await gate.wait()
            return Outcome.success(0)

        async def _bg(ctx):
            admit_order.append("background")
            return Outcome.success(1)

        async def _int(ctx):
            admit_order.append("interactive")
            return Outcome.success(2)

        t_hold = asyncio.create_task(
            pb.execute(_holder, _ctx(Priority.BACKGROUND)),
        )
        await asyncio.sleep(0.03)            # holder occupies the slot
        t_bg = asyncio.create_task(
            pb.execute(_bg, _ctx(Priority.BACKGROUND)),
        )
        await asyncio.sleep(0.03)            # background queues first
        t_int = asyncio.create_task(
            pb.execute(_int, _ctx(Priority.INTERACTIVE)),
        )
        await asyncio.sleep(0.03)            # interactive queues second
        gate.set()                           # release the holder
        await asyncio.gather(t_hold, t_bg, t_int)

    asyncio.run(_impl())
    assert admit_order[0] == "holder"
    assert admit_order[1] == "interactive", (
        f"INTERACTIVE should jump the queue; got {admit_order}"
    )
    assert admit_order[2] == "background"


def test_absent_priority_defaults_to_workflow():
    """A call with no PRIORITY set is treated as WORKFLOW (it still
    admits; the default just slots it between INTERACTIVE and BACKGROUND)."""
    pb = PriorityBulkheadStrategy(max_concurrent=1)

    async def _nxt(ctx):
        return Outcome.success("ok")

    outcome = asyncio.run(pb.execute(_nxt, _ctx()))  # no priority on ctx
    assert outcome.is_success


# ---------------------------------------------------------------------------
# Shedding
# ---------------------------------------------------------------------------


def test_priority_bulkhead_sheds_when_queue_full():
    pb = PriorityBulkheadStrategy(max_concurrent=1, max_queued=1)
    gate = asyncio.Event()

    async def _impl():
        async def _slow(ctx):
            await gate.wait()
            return Outcome.success(1)

        async def _waiter(ctx):
            return Outcome.success(2)

        t_hold = asyncio.create_task(
            pb.execute(_slow, _ctx(Priority.WORKFLOW)),
        )
        await asyncio.sleep(0.02)
        t_wait = asyncio.create_task(
            pb.execute(_waiter, _ctx(Priority.WORKFLOW)),
        )
        await asyncio.sleep(0.02)
        # the third WORKFLOW call exceeds max_queued at that priority
        shed = await pb.execute(_waiter, _ctx(Priority.WORKFLOW))
        assert shed.kind is OutcomeKind.REJECTED
        gate.set()
        await asyncio.gather(t_hold, t_wait)

    asyncio.run(_impl())


def test_priority_bulkhead_sheds_on_slot_wait_timeout():
    pb = PriorityBulkheadStrategy(max_concurrent=1, max_queued=8)
    gate = asyncio.Event()

    async def _impl():
        async def _slow(ctx):
            await gate.wait()
            return Outcome.success(1)

        t_hold = asyncio.create_task(pb.execute(_slow, _ctx()))
        await asyncio.sleep(0.02)

        async def _quick(ctx):
            return Outcome.success(2)

        outcome = await pb.execute(_quick, _ctx(deadline=Deadline.after(0.05)))
        assert outcome.kind is OutcomeKind.REJECTED
        gate.set()
        await t_hold

    asyncio.run(_impl())


# ---------------------------------------------------------------------------
# Context-property seeding (the priority-delivery mechanism)
# ---------------------------------------------------------------------------


def test_pipeline_properties_seed_the_context():
    captured: dict = {}

    def _fn():
        captured["priority"] = current_context().get(PRIORITY)
        return "ok"

    pipeline = ResiliencePipelineBuilder("p").build()
    asyncio.run(pipeline.execute(
        _fn, properties={PRIORITY: Priority.INTERACTIVE},
    ))
    assert captured["priority"] is Priority.INTERACTIVE


def test_priority_bulkhead_in_a_pipeline_with_seeded_priority():
    """End-to-end: a pipeline carrying a PriorityBulkheadStrategy, executed
    with a seeded priority, runs the call."""
    pipeline = (
        ResiliencePipelineBuilder("inference-ish")
        .add(PriorityBulkheadStrategy(max_concurrent=2))
        .build()
    )
    outcome = asyncio.run(pipeline.execute(
        lambda: "done", properties={PRIORITY: Priority.BACKGROUND},
    ))
    assert outcome.unwrap() == "done"
