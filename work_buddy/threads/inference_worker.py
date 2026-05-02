"""Sidecar inference worker — stateless dispatcher for the LLM-call queue.

Stage 2.4 deliverable. DESIGN.md §14 mandates: workers are
**stateless across restarts**, the Thread's event log is the durable
source of truth, and concurrency safety comes from a combination of
queue-level atomic dequeue (Stage 1.6) + event-log optimistic lock
(Stage 1.3).

Pipeline
--------

1. ``enqueue_inference_for_thread(thread, target)`` — called by the
   FSM state-entry handler when a Thread enters AWAITING_INFERENCE.
2. ``process_one_pending(worker_id)`` — claim one queue entry,
   move the Thread into INFERRING_*, run inference, transition
   via TRIG_INFERENCE_DONE (or TRIG_INFERENCE_FAILED), and mark
   the queue entry done/failed.
3. ``run_poller(worker_id, ...)`` — loop wrapper for the sidecar.

Failure model
-------------

If a worker is killed mid-process (kill -9, OOM, host reboot), the
queue entry stays in ``in_flight`` status and the Thread sits in
``inferring_*``. On the next poller iteration, a different worker
will not claim the in_flight entry (dequeue skips non-pending), but
a janitor pass (TODO Stage 2.x) can reclaim entries whose
``dequeued_at`` is older than a configurable timeout.

For Stage 2.4, killed workers leak entries. The fix is small (a
"reclaim stuck in_flight entries" pass) and lands in Stage 2.x as
the worker matures.

Target selection at AWAITING_INFERENCE entry
--------------------------------------------

When the FSM transitions a Thread into AWAITING_INFERENCE, the
state-entry handler reads the transition's ``data`` for an explicit
``target``. If absent, it falls back to a sequential walker:
intent → context → action, picking the first inference target
whose corresponding *_inferred event hasn't yet been recorded.

DESIGN.md says the FSM enqueues into the LLM-call queue; the
target choice is the FSM's responsibility (not the agent's), so
encoding it in the data payload is the right shape.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from work_buddy.llm import queue
from work_buddy.threads import engine, inference, store
from work_buddy.threads.enums import (
    FSMState,
    InferenceTarget,
    ReasoningTier,
)
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_CONTEXT_INFERRED,
    KIND_INTENT_INFERRED,
    ThreadEvent,
)
from work_buddy.threads.fsm import (
    TRIG_INFERENCE_DONE,
    TRIG_INFERENCE_FAILED,
)
from work_buddy.threads.models import Proposal, Thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Target → INFERRING_* state map
# ---------------------------------------------------------------------------


_TARGET_TO_INFERRING: dict[InferenceTarget, FSMState] = {
    InferenceTarget.INTENT: FSMState.INFERRING_INTENT,
    InferenceTarget.CONTEXT: FSMState.INFERRING_CONTEXT,
    InferenceTarget.ACTION: FSMState.INFERRING_ACTION,
}


_TARGET_EVENT_KIND: dict[InferenceTarget, str] = {
    InferenceTarget.INTENT: KIND_INTENT_INFERRED,
    InferenceTarget.CONTEXT: KIND_CONTEXT_INFERRED,
    InferenceTarget.ACTION: KIND_ACTION_INFERRED,
}


# ---------------------------------------------------------------------------
# Caller-id convention
# ---------------------------------------------------------------------------


_CALLER_PREFIX = "thread:"


def caller_id_for(thread_id: str) -> str:
    return f"{_CALLER_PREFIX}{thread_id}"


def thread_id_from_caller(caller_id: str) -> Optional[str]:
    if caller_id.startswith(_CALLER_PREFIX):
        return caller_id[len(_CALLER_PREFIX):]
    return None


# ---------------------------------------------------------------------------
# Target-selection (for the FSM state-entry handler)
# ---------------------------------------------------------------------------


def next_inference_target(thread: Thread) -> InferenceTarget:
    """Pick the first target whose *_inferred event hasn't fired yet.

    Walks intent → context → action. If every target has been
    inferred (action is the deepest), defaults to ACTION (the FSM
    will land at AWAITING_CONFIRMATION which doesn't loop back
    here).
    """
    seen_kinds = {
        e.kind for e in store.list_events(thread.thread_id)
    }
    if KIND_INTENT_INFERRED not in seen_kinds:
        return InferenceTarget.INTENT
    if KIND_CONTEXT_INFERRED not in seen_kinds:
        return InferenceTarget.CONTEXT
    return InferenceTarget.ACTION


# ---------------------------------------------------------------------------
# Enqueue: called by the FSM AWAITING_INFERENCE state-entry handler
# ---------------------------------------------------------------------------


def enqueue_inference_for_thread(
    thread: Thread,
    target: Optional[InferenceTarget] = None,
    *,
    priority: int = 100,
    tier_hint: Optional[ReasoningTier] = None,
    estimated_cost_usd: float = 0.0,
) -> Optional[int]:
    """Enqueue an inference request for ``thread``.

    Returns the queue entry id, or None if the queue rejected
    (e.g. budget exhausted — ``QueueRejected`` is caught and
    surfaces via FSM transition to a clarification state in the
    handler logic, not here).
    """
    target = target or next_inference_target(thread)
    payload = {
        "thread_id": thread.thread_id,
        "target": target.value,
    }
    try:
        return queue.enqueue(
            caller_id=caller_id_for(thread.thread_id),
            caller_kind=queue.CALLER_THREAD,
            target=target.value,
            priority=priority,
            payload=payload,
            tier_hint=tier_hint.value if tier_hint else None,
            estimated_cost_usd=estimated_cost_usd,
        )
    except queue.QueueRejected as e:
        # Budget exhausted (or other admission rejection). Surface
        # to the user via a clarification state. This is the
        # "force USER tier" path from DESIGN.md §9.4.
        logger.warning(
            "Inference enqueue rejected for %s: %s — escalating "
            "to user clarification.",
            thread.thread_id, e,
        )
        try:
            engine.transition(
                thread.thread_id, TRIG_INFERENCE_FAILED,
                data={"rejection_reason": str(e)},
                fire_side_effects=True,
            )
        except engine.InvalidTransition:
            pass
        return None


def awaiting_inference_handler(transition_result) -> None:
    """engine.register_state_entry_handler-compatible adapter for
    AWAITING_INFERENCE.

    The transition's ``data`` may carry an explicit ``target`` and
    ``priority``; otherwise defaults are used.
    """
    thread = store.get_thread(transition_result.thread_id)
    if thread is None:
        return
    data = transition_result.data or {}
    target_str = data.get("target")
    target = InferenceTarget(target_str) if target_str else None
    priority = int(data.get("priority", 100))
    tier_hint_str = data.get("tier_hint")
    tier_hint = ReasoningTier(tier_hint_str) if tier_hint_str else None
    enqueue_inference_for_thread(
        thread, target,
        priority=priority,
        tier_hint=tier_hint,
        estimated_cost_usd=float(data.get("estimated_cost_usd", 0.0)),
    )


def register_inference_dispatch_handler() -> None:
    """Wire ``awaiting_inference_handler`` to the
    AWAITING_INFERENCE state. Stage 2.9 bootstrap calls this."""
    engine.register_state_entry_handler(
        FSMState.AWAITING_INFERENCE, awaiting_inference_handler,
    )


# ---------------------------------------------------------------------------
# Worker: process one pending entry
# ---------------------------------------------------------------------------


def process_one_pending(worker_id: str) -> Optional[dict[str, Any]]:
    """Claim and process one pending entry from the LLM-call queue.

    Returns a summary dict if an entry was processed, or None if
    the queue was empty.

    The summary shape:
        {
          "queue_entry_id": int,
          "thread_id": str | None,
          "target": str,
          "outcome": "done" | "failed" | "skipped",
          "next_state": str | None,
        }
    """
    entry = queue.dequeue(worker_id, caller_kind=queue.CALLER_THREAD)
    if entry is None:
        return None

    thread_id = thread_id_from_caller(entry.caller_id)
    target = InferenceTarget(entry.target)
    summary: dict[str, Any] = {
        "queue_entry_id": entry.id,
        "thread_id": thread_id,
        "target": target.value,
        "outcome": "skipped",
        "next_state": None,
    }

    if thread_id is None:
        # Not a Thread caller (shouldn't happen with the
        # caller_kind filter, but defensive).
        queue.fail(entry.id, f"Unknown caller_id shape: {entry.caller_id!r}")
        summary["outcome"] = "failed"
        return summary

    thread = store.get_thread(thread_id)
    if thread is None:
        queue.fail(entry.id, f"Thread {thread_id!r} not found")
        summary["outcome"] = "failed"
        return summary

    inferring_state = _TARGET_TO_INFERRING.get(target)
    if inferring_state is None:
        queue.fail(entry.id, f"Unknown inference target: {target!r}")
        summary["outcome"] = "failed"
        return summary

    # Move the Thread cache into INFERRING_* so observers
    # (dashboard, audit) see the in-flight state. This is a direct
    # cache update because the AWAITING_INFERENCE → INFERRING_*
    # transition is owned by the worker, not the FSM table.
    store.update_thread_state(
        thread_id, fsm_state=inferring_state.value,
    )

    # Run the inference. Record_event=True records a *_inferred
    # event with provenance.
    tier = ReasoningTier(entry.tier_hint) if entry.tier_hint else None
    try:
        proposal: Proposal = inference.run(
            thread,
            target,
            tier=tier,
            record_event=True,
        )
    except Exception as e:
        logger.exception(
            "Inference failed for thread %s target %s: %s",
            thread_id, target.value, e,
        )
        queue.fail(entry.id, f"{type(e).__name__}: {e}")
        # Move FSM forward via failure trigger
        try:
            result = engine.transition(
                thread_id, TRIG_INFERENCE_FAILED,
                data={"error": str(e), "queue_entry_id": entry.id},
                fire_side_effects=True,
            )
            summary["next_state"] = result.next_state.value
        except engine.InvalidTransition:
            logger.warning(
                "TRIG_INFERENCE_FAILED not valid for thread %s in state %s",
                thread_id, store.get_thread(thread_id).fsm_state.value,
            )
        summary["outcome"] = "failed"
        return summary

    # Success: queue done + FSM forward via TRIG_INFERENCE_DONE
    queue.complete(entry.id, {"proposal": proposal.to_dict()})
    try:
        result = engine.transition(
            thread_id, TRIG_INFERENCE_DONE,
            data={
                "target": target.value,
                "confidence": proposal.confidence,
                "tier_used": proposal.tier_used.value,
                "model_used": proposal.model_used,
                "queue_entry_id": entry.id,
                # Allow the next state's side effects to see the
                # proposal payload (e.g. for the
                # AWAITING_*_CONFIRMATION card)
                **proposal.payload,
            },
            fire_side_effects=True,
        )
        summary["next_state"] = result.next_state.value
        summary["outcome"] = "done"
    except engine.InvalidTransition:
        logger.warning(
            "TRIG_INFERENCE_DONE not valid for thread %s in state %s; "
            "proposal recorded but FSM not advanced",
            thread_id, store.get_thread(thread_id).fsm_state.value,
        )
        summary["outcome"] = "done"
    return summary


# ---------------------------------------------------------------------------
# Poller loop
# ---------------------------------------------------------------------------


def run_poller(
    worker_id: str,
    *,
    max_iterations: Optional[int] = None,
    poll_interval_s: float = 10.0,
    process_fn=None,
) -> int:
    """Drain the queue, sleeping ``poll_interval_s`` between empty
    pulls. Returns the number of entries processed.

    Stage 2.4 ships a simple loop. Stage 2.x can swap in a richer
    runner with concurrent workers, lease reclamation, etc.

    Tests pass ``max_iterations`` to bound the loop and a custom
    ``process_fn`` to stub the dispatch.
    """
    import time

    fn = process_fn or process_one_pending
    iterations = 0
    processed = 0
    while max_iterations is None or iterations < max_iterations:
        result = fn(worker_id)
        iterations += 1
        if result is not None:
            processed += 1
            continue
        if max_iterations is not None and iterations >= max_iterations:
            break
        time.sleep(poll_interval_s)
    return processed
