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

    # Stage 5: combined-inference fast path. Single LLM call, three
    # FSM transitions. Each transition still goes through the
    # autonomy-gated branch resolver, so policy still decides whether
    # to surface or skip — combined inference is a *call-count*
    # optimization, not a policy bypass.
    if target == InferenceTarget.COMBINED:
        return _process_combined(entry, thread, summary)

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
        # The failed inference path may or may not have written
        # an event — read fresh latest_event_id either way for
        # the optimistic-lock target.
        fresh_parent = store.latest_event_id(thread_id)
        try:
            result = engine.transition(
                thread_id, TRIG_INFERENCE_FAILED,
                data={"error": str(e), "queue_entry_id": entry.id},
                parent_event_id=fresh_parent,
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

    # Success: queue done + FSM forward via TRIG_INFERENCE_DONE.
    # NOTE: inference.run() just inserted a *_inferred event, so
    # the Thread's parent_event_id cache is stale. Pass an
    # explicit parent_event_id read from the events table so
    # the optimistic-lock target reflects what we actually saw,
    # not the cache.
    queue.complete(entry.id, {"proposal": proposal.to_dict()})
    fresh_parent = store.latest_event_id(thread_id)
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
            parent_event_id=fresh_parent,
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
# Combined-inference processing (Stage 5)
# ---------------------------------------------------------------------------


def _process_combined(
    entry, thread, summary: dict[str, Any],
) -> dict[str, Any]:
    """Run a single COMBINED-target LLM call and walk the FSM through
    intent → context → action.

    The call returns a payload shaped like::

        {
          "intent":   {"intent": "...", "supporting_refs": [...]},
          "context":  {"associated_refs": [...], "reasoning": "..."},
          "action":   {"kind": "...", "name": "...", ...,
                       "irreversibility": "...", "regret_potential": "...",
                       "risk_amplifier": bool},
          "confidence": 0.92
        }

    The worker:

    1. Records three ``*_inferred`` events (intent, context, action)
       with the combined payload split per-target.
    2. Records one ``combined_inferred_meta`` audit event so the
       trace is honest about "this was a single call".
    3. Walks the FSM by firing TRIG_INFERENCE_DONE three times.
       Each transition goes through the autonomy-gated branch
       resolver. If at any step the resolver lands on a wait
       state (low confidence or policy denied), we stop and let
       the user resolve before the remaining payloads are surfaced.

    The remaining (unconsumed) target payloads are still recorded
    in the event log as informational ``*_inferred`` events; they
    just don't trigger further FSM transitions until the user
    confirms the current pause point.
    """
    from work_buddy.threads.events import (
        ACTOR_AGENT,
        KIND_COMBINED_INFERRED_META,
    )
    thread_id = thread.thread_id

    # Move into INFERRING_INTENT first (signals "the agent is working").
    store.update_thread_state(
        thread_id, fsm_state=FSMState.INFERRING_INTENT.value,
    )

    tier = ReasoningTier(entry.tier_hint) if entry.tier_hint else None
    try:
        proposal = inference.run(
            thread,
            InferenceTarget.COMBINED,
            tier=tier,
            record_event=False,  # we record per-target events ourselves
        )
    except Exception as e:
        logger.exception(
            "Combined inference failed for thread %s: %s", thread_id, e,
        )
        queue.fail(entry.id, f"{type(e).__name__}: {e}")
        fresh_parent = store.latest_event_id(thread_id)
        try:
            engine.transition(
                thread_id, TRIG_INFERENCE_FAILED,
                data={"error": str(e), "queue_entry_id": entry.id},
                parent_event_id=fresh_parent,
                fire_side_effects=True,
            )
        except engine.InvalidTransition:
            pass
        summary["outcome"] = "failed"
        return summary

    payload = proposal.payload or {}
    overall_confidence = proposal.confidence
    intent_p = payload.get("intent") or {}
    context_p = payload.get("context") or {}
    action_p = payload.get("action") or {}

    # Record per-target *_inferred events. We use the same shape
    # that inference.run() would have produced for staged inference,
    # so downstream code (search-blob refresh, render data) doesn't
    # need to special-case combined output.
    for kind, sub_payload in (
        (KIND_INTENT_INFERRED, intent_p),
        (KIND_CONTEXT_INFERRED, context_p),
        (KIND_ACTION_INFERRED, action_p),
    ):
        store.append_event(ThreadEvent(
            thread_id=thread_id,
            kind=kind,
            actor=ACTOR_AGENT,
            inference_tier=proposal.tier_used.value,
            data={
                "target": kind.replace("_inferred", ""),
                "payload": sub_payload,
                "confidence": overall_confidence,
                "tier_used": proposal.tier_used.value,
                "model_used": proposal.model_used,
                "cost_usd": proposal.cost_usd,
                "reasoning_trace_pointer": proposal.reasoning_trace_pointer,
                "from_combined_call": True,
            },
        ))

    # Audit event records the call provenance (one LLM call, three
    # *_inferred events). Useful when reading the event log later
    # to understand why three "inferred" events all carry the same
    # cost, model, and timestamp.
    store.append_event(ThreadEvent(
        thread_id=thread_id,
        kind=KIND_COMBINED_INFERRED_META,
        actor=ACTOR_AGENT,
        inference_tier=proposal.tier_used.value,
        data={
            "queue_entry_id": entry.id,
            "model_used": proposal.model_used,
            "cost_usd": proposal.cost_usd,
            "tier_used": proposal.tier_used.value,
            "overall_confidence": overall_confidence,
        },
    ))

    queue.complete(entry.id, {"proposal": proposal.to_dict()})

    # Refresh search blob now that intent/action events are recorded.
    try:
        from work_buddy.threads.search import update_search_blob
        update_search_blob(thread_id)
    except Exception as e:
        logger.warning(
            "Combined search-blob refresh failed for %s: %s", thread_id, e,
        )

    # Walk the FSM. Each transition fires the autonomy-gated branch
    # resolver. If the resolver auto-advances, we move into the
    # next INFERRING_* state (manually, since AWAITING_INFERENCE →
    # INFERRING_* is owned by the worker, not the FSM table). If
    # the resolver lands on a wait state, we stop — the remaining
    # target events have been recorded but FSM advancement halts
    # until the user resolves.
    summary["outcome"] = "done"
    fresh_parent = store.latest_event_id(thread_id)
    for stage_idx, (target_name, sub_payload, next_inferring_state) in enumerate((
        ("intent", intent_p, FSMState.INFERRING_CONTEXT),
        ("context", context_p, FSMState.INFERRING_ACTION),
        ("action", action_p, None),  # last stage, no follow-on inferring
    )):
        # Build transition data with target-specific payload merged
        # in (so the autonomy resolver sees fields like
        # irreversibility, regret_potential, etc. for the action).
        data = {
            "target": target_name,
            "confidence": overall_confidence,
            "tier_used": proposal.tier_used.value,
            "model_used": proposal.model_used,
            "queue_entry_id": entry.id,
            "from_combined_call": True,
            **sub_payload,
        }
        try:
            result = engine.transition(
                thread_id, TRIG_INFERENCE_DONE,
                data=data,
                parent_event_id=fresh_parent,
                fire_side_effects=True,
            )
            summary["next_state"] = result.next_state.value
            fresh_parent = store.latest_event_id(thread_id)
        except engine.InvalidTransition:
            logger.warning(
                "TRIG_INFERENCE_DONE not valid for thread %s in state %s "
                "during combined inference stage %d; halting walk",
                thread_id,
                store.get_thread(thread_id).fsm_state.value,
                stage_idx,
            )
            break

        # If the resolver landed on a wait state, the user must
        # resolve before we advance further. Halt the walk; the
        # remaining target payloads have been recorded as events
        # for the audit log but won't drive transitions until the
        # user confirms / clarifies.
        if not result.next_state == FSMState.AWAITING_INFERENCE:
            break
        if next_inferring_state is None:
            break

        # Resolver auto-advanced. Move the cache into the next
        # INFERRING_* state and loop. This mirrors the
        # AWAITING_INFERENCE → INFERRING_* manual-cache update
        # the staged worker does on dequeue.
        store.update_thread_state(
            thread_id, fsm_state=next_inferring_state.value,
        )
        # parent_event_id was bumped; re-read for the next transition.
        fresh_parent = store.latest_event_id(thread_id)

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
