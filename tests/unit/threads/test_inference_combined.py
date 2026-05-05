"""Combined-inference tests — Stage 5 / Phase 3 of autonomy plan.

A single LLM call returns intent + context + action; the worker
records three separate ``*_inferred`` events plus a
``combined_inferred_meta`` audit event, and walks the FSM through
inferring_intent → inferring_context → inferring_action → final
state, gated by the autonomy resolver at each step.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import (
    autonomy, bootstrap, engine, inference, inference_worker, store,
)
from work_buddy.threads.enums import (
    FSMState, InferenceTarget, ReasoningTier,
)
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_AUTO_ADVANCE_DECISION,
    KIND_COMBINED_INFERRED_META,
    KIND_CONTEXT_INFERRED,
    KIND_INTENT_INFERRED,
)
from work_buddy.threads.fsm import TRIG_BEGIN_INFERENCE
from work_buddy.threads.models import AutonomyPolicy, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    # Isolate the LLM-call queue too — it's a separate DB. Without
    # this, leftover entries from previous tests can be dequeued
    # by this test's worker, causing "Thread not found" errors.
    from work_buddy.llm import queue as llm_queue
    queue_db = tmp_path / "llm_queue.db"
    monkeypatch.setattr(llm_queue, "_db_path", lambda: queue_db)

    # Set up bootstrap so the AWAITING_INFERENCE state-entry handler
    # is registered (the worker's enqueue path is exercised by the
    # journal-spawn flow; here we set up state directly).
    bootstrap.bootstrap_threads(clear_first=True)
    yield db
    bootstrap.teardown_threads()


def _stub_combined_runner(payload):
    """Build a runner that returns ``payload`` for InferenceTarget.COMBINED."""
    def runner(prompt, schema, tier, thread):
        return {
            "payload": payload,
            "confidence": payload.get("confidence", 0.9),
            "model": "test-stub",
            "cost_usd": 0.001,
            "trace_pointer": None,
        }
    return runner


def _enqueue_and_drain(thread_id: str, *, target=InferenceTarget.COMBINED):
    """Helper: put a queue entry and run the worker once."""
    from work_buddy.llm import queue
    queue.enqueue(
        caller_id=inference_worker.caller_id_for(thread_id),
        caller_kind=queue.CALLER_THREAD,
        target=target.value,
        priority=100,
        payload={"thread_id": thread_id, "target": target.value},
    )
    return inference_worker.process_one_pending("test-worker")


class TestCombinedInferenceHighConfidence:
    def test_walks_from_inferring_intent_to_awaiting_confirmation(
        self, fresh_db, monkeypatch,
    ):
        """With PLAN_THEN_REVIEW + high confidence:
        worker records three *_inferred events + one
        combined_inferred_meta + walks FSM to AWAITING_CONFIRMATION."""

        # Stub the LLM
        combined_payload = {
            "intent": {
                "intent": "Schedule a check-in with Sam",
                "supporting_refs": ["journal_note: re sam"],
            },
            "context": {
                "associated_refs": [{"id": "ci-sam", "label": "Sam"}],
                "reasoning": "Sam appears in the inciting line.",
            },
            "action": {
                "kind": "improvised",
                "name": "send_calendar_invite",
                "plan_summary": "30-minute check-in next week",
                "irreversibility": "low",
                "regret_potential": "low",
                "risk_amplifier": False,
            },
            "confidence": 0.92,
        }
        inference.set_llm_runner(_stub_combined_runner(combined_payload))

        # Spawn a thread directly in AWAITING_INFERENCE
        t = Thread(
            autonomy_policy=autonomy.PLAN_THEN_REVIEW,
            fsm_state=FSMState.AWAITING_INFERENCE,
        )
        store.insert_thread(t)

        result = _enqueue_and_drain(t.thread_id)
        assert result is not None
        assert result["outcome"] == "done"

        # Final state: PLAN_THEN_REVIEW does NOT auto-advance through
        # AWAITING_CONFIRMATION — that's the legitimate user-pause.
        fetched = store.get_thread(t.thread_id)
        assert fetched.fsm_state == FSMState.AWAITING_CONFIRMATION

        # Event log:
        events = store.list_events(t.thread_id)
        kinds = [e.kind for e in events]
        # Three per-target *_inferred events
        assert kinds.count(KIND_INTENT_INFERRED) == 1
        assert kinds.count(KIND_CONTEXT_INFERRED) == 1
        assert kinds.count(KIND_ACTION_INFERRED) == 1
        # One audit event recording the combined call
        assert kinds.count(KIND_COMBINED_INFERRED_META) == 1
        # All three per-target events flagged as from_combined_call
        for e in events:
            if e.kind in (KIND_INTENT_INFERRED, KIND_CONTEXT_INFERRED,
                          KIND_ACTION_INFERRED):
                assert e.data.get("from_combined_call") is True
        # Two auto_advance_decision events (intent & context advanced;
        # action was the surface point)
        adv_decisions = [e for e in events if e.kind == KIND_AUTO_ADVANCE_DECISION]
        assert len(adv_decisions) == 3
        intent_dec = next(d for d in adv_decisions if d.data["target"] == "intent")
        context_dec = next(d for d in adv_decisions if d.data["target"] == "context")
        action_dec = next(d for d in adv_decisions if d.data["target"] == "action")
        assert intent_dec.data["advance"] is True
        assert context_dec.data["advance"] is True
        assert action_dec.data["advance"] is False  # surfaced for review


class TestCombinedInferenceLowConfidence:
    def test_low_confidence_halts_at_intent_confirmation(
        self, fresh_db, monkeypatch,
    ):
        """If confidence is below the floor, the autonomy resolver
        surfaces the intent confirmation. Combined output is still
        recorded in events; FSM walking halts at the first wait
        state."""
        combined_payload = {
            "intent": {"intent": "Maybe schedule something?"},
            "context": {"associated_refs": []},
            "action": {"kind": "suggestion", "name": "ask_user"},
            "confidence": 0.2,  # well below PLAN_THEN_REVIEW's 0.6
        }
        inference.set_llm_runner(_stub_combined_runner(combined_payload))

        t = Thread(
            autonomy_policy=autonomy.PLAN_THEN_REVIEW,
            fsm_state=FSMState.AWAITING_INFERENCE,
        )
        store.insert_thread(t)

        _enqueue_and_drain(t.thread_id)

        fetched = store.get_thread(t.thread_id)
        assert fetched.fsm_state == FSMState.AWAITING_INTENT_CONFIRMATION

        # All three target events still recorded (they came from one
        # LLM call) — the user can edit/redirect intent and the
        # context+action stay informational until refreshed.
        events = store.list_events(t.thread_id)
        kinds = [e.kind for e in events]
        assert KIND_INTENT_INFERRED in kinds
        assert KIND_CONTEXT_INFERRED in kinds
        assert KIND_ACTION_INFERRED in kinds


class TestCombinedInferencePropertiesPropagate:
    def test_action_risk_fields_seen_by_resolver(
        self, fresh_db, monkeypatch,
    ):
        """Risk fields declared in the combined action payload
        (irreversibility, regret_potential, risk_amplifier) flow
        through the worker into the action branch resolver's
        decision data."""
        combined_payload = {
            "intent": {"intent": "Delete an old draft"},
            "context": {"associated_refs": []},
            "action": {
                "kind": "improvised",
                "name": "delete_email_draft",
                "irreversibility": "high",  # would block under PLAN_THEN_REVIEW
                "regret_potential": "medium",
                "risk_amplifier": True,
            },
            "confidence": 0.95,
        }
        inference.set_llm_runner(_stub_combined_runner(combined_payload))

        # Use END_TO_END so the only thing potentially blocking is
        # the action's risk profile. (PLAN_THEN_REVIEW would block
        # at AWAITING_CONFIRMATION via auto_advance_states regardless.)
        t = Thread(
            autonomy_policy=autonomy.END_TO_END,
            fsm_state=FSMState.AWAITING_INFERENCE,
        )
        store.insert_thread(t)

        _enqueue_and_drain(t.thread_id)

        fetched = store.get_thread(t.thread_id)
        # Risk is high + risk_amplifier=True → action branch resolver
        # forces AWAITING_CONFIRMATION even under END_TO_END.
        assert fetched.fsm_state == FSMState.AWAITING_CONFIRMATION

        events = store.list_events(t.thread_id)
        action_dec = next(
            e for e in events
            if e.kind == KIND_AUTO_ADVANCE_DECISION
            and e.data["target"] == "action"
        )
        assert action_dec.data["advance"] is False
        assert action_dec.data["irreversibility"] == "high"
        assert action_dec.data["risk_amplifier"] is True
