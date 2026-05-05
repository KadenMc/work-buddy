"""v5 end-to-end pipeline smoke test.

Walks a Thread from inciting event to DONE through the full Stage 1
+ Stage 2 + Stage 3 mechanics, with stubbed LLM + notifications so
the test runs without any external services.

This is the template Stage 4 use cases should follow:
- A real LLM runner is registered via ``inference.set_llm_runner``.
- The bootstrap fires.
- The FSM walks the inciting → resolution → execution → terminal
  path.
- Resolution Surface card publication and queue dispatch are
  stubbed for assertion.

The test is intentionally a single end-to-end happy-path. Edge
cases live in the per-module test files under tests/unit/threads/.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.llm import budget, queue
from work_buddy.threads import (
    autonomy,
    bootstrap,
    engine,
    inference,
    inference_worker,
    resolution_surface,
    store,
)
from work_buddy.threads.enums import (
    FSMState,
    InferenceTarget,
    ReasoningTier,
)
from work_buddy.threads.events import KIND_INCITING_EVENT, ThreadEvent
from work_buddy.threads.fsm import (
    TRIG_CONFIRMED,
    TRIG_EXECUTE,
    TRIG_EXECUTION_DONE,
    TRIG_PROVIDED,
)
from work_buddy.threads.models import Thread


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "threads.db")
    monkeypatch.setattr(queue, "_db_path", lambda: tmp_path / "queue.db")
    bootstrap.teardown_threads()
    inference.set_llm_runner(inference._stub_runner)
    yield
    bootstrap.teardown_threads()
    inference.set_llm_runner(inference._stub_runner)


def test_full_pipeline_walk(fresh):
    """A net-new Thread runs end-to-end:

    1. inciting_event → PROPOSED → AWAITING_INFERENCE (queue
       publishes)
    2. worker pulls → INFERRING_INTENT → AWAITING_INTENT_CONFIRMATION
       (Resolution Surface card publishes)
    3. user confirms → AWAITING_INFERENCE → INFERRING_CONTEXT →
       AWAITING_CONTEXT_CONFIRMATION
    4. user confirms → AWAITING_INFERENCE → INFERRING_ACTION →
       AWAITING_CONFIRMATION (consent card)
    5. user approves → EXECUTING → DONE

    The walk asserts step-by-step user confirmation between each
    inference target. Use a STRICT policy that does NOT auto-advance
    past intent/context — the saved ``PLAN_THEN_REVIEW`` composition
    permits both, which would collapse intent + context into a
    single chained worker pass and fail the per-step assertions.
    The strict policy below pins this test to the manual-confirm
    flow it was written to exercise.

    Stub the LLM runner with deterministic proposals and the
    Resolution Surface publisher with a simple capture list.
    """

    # ---- Setup ----
    bootstrap.bootstrap_threads()

    from dataclasses import replace
    strict_policy = replace(
        autonomy.PLAN_THEN_REVIEW,
        auto_advance_states=frozenset({
            FSMState.PROPOSED,
            FSMState.AWAITING_INFERENCE,
            FSMState.INFERRING_INTENT,
            FSMState.INFERRING_CONTEXT,
            FSMState.INFERRING_ACTION,
            # Critically: NOT AWAITING_INTENT_CONFIRMATION /
            # AWAITING_CONTEXT_CONFIRMATION. The post-inference
            # branch resolver should land on the confirmation
            # state and wait for the user, not skip ahead.
        }),
    )

    proposals_by_target = {
        "intent": {"intent": "schedule a meeting"},
        "context": {"associated_refs": ["@calendar"]},
        "action": {
            "kind": "standard",
            "name": "create_calendar_event",
            "parameters": {"title": "schedule a meeting"},
        },
    }

    def runner(prompt, schema, tier, thread):
        # Pick payload by which target the prompt is for —
        # cheaper signal: the test infers based on what's already
        # in the event log.
        from work_buddy.threads.events import (
            KIND_INTENT_INFERRED,
            KIND_CONTEXT_INFERRED,
        )
        seen = {e.kind for e in store.list_events(thread.thread_id)}
        if KIND_INTENT_INFERRED not in seen:
            payload = proposals_by_target["intent"]
        elif KIND_CONTEXT_INFERRED not in seen:
            payload = proposals_by_target["context"]
        else:
            payload = proposals_by_target["action"]
        return {
            "payload": payload,
            "confidence": 0.9,
            "model": "test-runner",
            "cost_usd": 0.001,
            "trace_pointer": None,
        }

    inference.set_llm_runner(runner)

    # ---- Capture published Resolution Surface cards ----
    published: list = []
    with patch.object(
        resolution_surface, "publish",
        side_effect=lambda rr: published.append(rr) or None,
    ):
        # ---- 1. Inciting event ----
        t = Thread(autonomy_policy=strict_policy)
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
            data={"source": "test"},
        ))
        store.update_thread_state(
            t.thread_id,
            parent_event_id=store.latest_event_id(t.thread_id),
        )

        # ---- 2. Move to AWAITING_INFERENCE; worker processes ----
        store.update_thread_state(
            t.thread_id, fsm_state=FSMState.AWAITING_INFERENCE.value,
        )
        # Manually fire the side-effect because we updated the
        # cache directly. In production the engine.transition fires
        # them.
        engine._fire_side_effects(engine.TransitionResult(
            thread_id=t.thread_id,
            prev_state=FSMState.PROPOSED,
            next_state=FSMState.AWAITING_INFERENCE,
            trigger="manual",
            event_id=store.latest_event_id(t.thread_id),
            data={"target": "intent"},
        ))
        # Queue should have one entry
        pending = queue.peek_pending(caller_kind="thread")
        assert len(pending) == 1
        assert pending[0].target == "intent"

        # Worker processes
        summary = inference_worker.process_one_pending("worker-A")
        assert summary["outcome"] == "done"
        assert summary["next_state"] == "awaiting_intent_confirmation"

        # ---- 3. User confirms intent ----
        engine.transition(
            t.thread_id, TRIG_CONFIRMED,
            data={"target": "context"},
        )
        # Now in AWAITING_INFERENCE again with target=context
        pending = queue.peek_pending(caller_kind="thread")
        assert len(pending) == 1
        assert pending[0].target == "context"

        # Worker processes
        summary = inference_worker.process_one_pending("worker-A")
        assert summary["next_state"] == "awaiting_context_confirmation"

        # ---- 4. User confirms context ----
        engine.transition(
            t.thread_id, TRIG_CONFIRMED,
            data={"target": "action"},
        )
        pending = queue.peek_pending(caller_kind="thread")
        assert len(pending) == 1
        assert pending[0].target == "action"

        # Worker processes — lands at AWAITING_CONFIRMATION
        summary = inference_worker.process_one_pending("worker-A")
        assert summary["next_state"] == "awaiting_confirmation"

        # ---- 5. User approves the action ----
        engine.transition(
            t.thread_id, TRIG_EXECUTE,
        )
        assert store.get_thread(t.thread_id).fsm_state == FSMState.EXECUTING

        # ---- 6. Execution completes ----
        engine.transition(
            t.thread_id, TRIG_EXECUTION_DONE,
            data={"requires_post_review": False},
        )
        assert store.get_thread(t.thread_id).fsm_state == FSMState.DONE

    # ---- Verify the published-card sequence ----
    states_published = [rr.fsm_state for rr in published]
    assert FSMState.AWAITING_INTENT_CONFIRMATION in states_published
    assert FSMState.AWAITING_CONTEXT_CONFIRMATION in states_published
    assert FSMState.AWAITING_CONFIRMATION in states_published

    # ---- Verify the event log captures the journey ----
    events = store.list_events(t.thread_id)
    kinds = [e.kind for e in events]
    assert "inciting_event" in kinds
    assert "intent_inferred" in kinds
    assert "context_inferred" in kinds
    assert "action_inferred" in kinds
    # Multiple state_transition events
    assert kinds.count("state_transition") >= 4
