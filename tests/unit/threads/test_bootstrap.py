"""v5 Stage 2.9 — bootstrap wiring + per-Thread budget read-through.

End-to-end tests that confirm the bootstrap wires the full
pipeline: a Thread enters AWAITING_INFERENCE → queue pulls in
inference work → worker processes → FSM advances → Resolution
Surface card publishes → on terminal, cascade fires.

Also pins:
- budget.get_caller_budget falls back to the Thread's
  autonomy_policy.budget_usd when no explicit cap is set (zero-
  config per-Thread budgets).
- bootstrap_v5(clear_first=True) is fully idempotent.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.llm import budget, queue
from work_buddy.threads import (
    autonomy,
    bootstrap,
    decompose,
    engine,
    inference,
    inference_worker,
    resolution_surface,
    store,
)
from work_buddy.threads.enums import (
    FSMState,
    InferenceTarget,
)
from work_buddy.threads.fsm import (
    TRIG_CONFIRMED,
    TRIG_INFERENCE_DONE,
)
from work_buddy.threads.models import AutonomyPolicy, Thread


@pytest.fixture
def fresh_dbs(tmp_path, monkeypatch):
    threads_db = tmp_path / "threads.db"
    queue_db = tmp_path / "queue.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    monkeypatch.setattr(queue, "_db_path", lambda: queue_db)
    bootstrap.teardown_v5()
    inference.set_llm_runner(inference._stub_runner)
    yield (threads_db, queue_db)
    bootstrap.teardown_v5()
    inference.set_llm_runner(inference._stub_runner)


# ---------------------------------------------------------------------------
# bootstrap_v5
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_bootstrap_marks_state(self, fresh_dbs):
        assert bootstrap.is_bootstrapped() is False
        bootstrap.bootstrap_v5()
        assert bootstrap.is_bootstrapped() is True

    def test_bootstrap_registers_admission_hook(self, fresh_dbs):
        assert len(queue._ADMISSION_HOOKS) == 0
        bootstrap.bootstrap_v5()
        assert budget.budget_admission_hook in queue._ADMISSION_HOOKS

    def test_bootstrap_registers_inference_dispatch(self, fresh_dbs):
        bootstrap.bootstrap_v5()
        handlers = engine._REGISTERED_SIDE_EFFECTS.get(
            FSMState.AWAITING_INFERENCE, []
        )
        assert inference_worker.awaiting_inference_handler in handlers

    def test_bootstrap_registers_resolution_surface_for_every_wait_state(self, fresh_dbs):
        bootstrap.bootstrap_v5()
        for state in FSMState:
            if state.is_wait_state:
                handlers = engine._REGISTERED_SIDE_EFFECTS.get(state, [])
                assert resolution_surface._state_entry_handler in handlers

    def test_bootstrap_registers_cascade_for_terminals(self, fresh_dbs):
        bootstrap.bootstrap_v5()
        for state in (FSMState.DONE, FSMState.DISMISSED, FSMState.HANDED_OFF):
            handlers = engine._REGISTERED_SIDE_EFFECTS.get(state, [])
            assert decompose.cascade_handler in handlers

    def test_clear_first_resets_handlers(self, fresh_dbs):
        bootstrap.bootstrap_v5()
        # Add an extra handler manually so we can prove
        # ``clear_first=True`` removes it.
        manual = lambda r: None
        engine.register_state_entry_handler(FSMState.PROPOSED, manual)
        bootstrap.bootstrap_v5(clear_first=True)
        # After clear_first + re-bootstrap: the manual handler is
        # gone. Wave D (2026-05-03): bootstrap now also registers
        # a dashboard event emitter on EVERY state — including
        # PROPOSED — so we expect exactly one handler (the emitter)
        # and assert ``manual`` is not in the list.
        proposed_handlers = engine._REGISTERED_SIDE_EFFECTS.get(
            FSMState.PROPOSED, []
        )
        assert manual not in proposed_handlers
        # The dashboard emitter is the only bootstrap-registered
        # handler for PROPOSED.
        assert len(proposed_handlers) == 1

    def test_teardown_clears_state(self, fresh_dbs):
        bootstrap.bootstrap_v5()
        bootstrap.teardown_v5()
        assert bootstrap.is_bootstrapped() is False
        assert engine._REGISTERED_SIDE_EFFECTS == {}
        assert queue._ADMISSION_HOOKS == []


class TestNormalizeParametersJson:
    """Action proposals carry parameters as a JSON STRING in the
    schema (parameters_json), because Anthropic's structured-output
    validator rejects open-shape ``object`` types. The runner
    adapter parses parameters_json back to a dict before returning,
    so downstream consumers see the canonical ``parameters`` shape.
    """

    def test_parses_top_level_action(self):
        payload = {
            "kind": "improvised",
            "name": "send_email",
            "parameters_json": '{"to": "x@y", "subject": "hi"}',
        }
        bootstrap._normalize_parameters_json(payload)
        assert "parameters_json" not in payload
        assert payload["parameters"] == {"to": "x@y", "subject": "hi"}

    def test_parses_nested_combined_action(self):
        payload = {
            "intent": {"intent": "do x"},
            "context": {"associated_refs": []},
            "action": {
                "kind": "standard",
                "name": "decompose",
                "parameters_json": '{"items": ["a", "b"]}',
            },
        }
        bootstrap._normalize_parameters_json(payload)
        assert "parameters_json" not in payload["action"]
        assert payload["action"]["parameters"] == {"items": ["a", "b"]}

    def test_missing_parameters_json_is_noop(self):
        payload = {"kind": "improvised", "name": "x"}
        bootstrap._normalize_parameters_json(payload)
        assert "parameters" not in payload  # field stays absent

    def test_invalid_json_falls_back_to_empty_dict(self):
        payload = {"kind": "improvised", "parameters_json": "not-json"}
        bootstrap._normalize_parameters_json(payload)
        assert payload["parameters"] == {}

    def test_non_dict_parsed_value_falls_back(self):
        # Agent returned an array instead of an object — our contract
        # says parameters is a dict, so we coerce to empty.
        payload = {"kind": "improvised", "parameters_json": "[1,2,3]"}
        bootstrap._normalize_parameters_json(payload)
        assert payload["parameters"] == {}


# ---------------------------------------------------------------------------
# Per-Thread budget read-through (from autonomy_policy)
# ---------------------------------------------------------------------------


class TestBudgetReadThrough:
    def test_thread_caller_budget_from_autonomy_policy(self, fresh_dbs):
        # Thread with explicit budget_usd in its policy
        custom_policy = autonomy.compose(
            autonomy.PLAN_THEN_REVIEW, {"budget_usd": 1.50},
        )
        t = Thread(autonomy_policy=custom_policy)
        store.insert_thread(t)
        cid = inference_worker.caller_id_for(t.thread_id)
        # No explicit set_caller_budget; budget read from policy
        assert budget.get_caller_budget(cid) == 1.50

    def test_explicit_budget_overrides_policy(self, fresh_dbs):
        custom_policy = autonomy.compose(
            autonomy.PLAN_THEN_REVIEW, {"budget_usd": 1.50},
        )
        t = Thread(autonomy_policy=custom_policy)
        store.insert_thread(t)
        cid = inference_worker.caller_id_for(t.thread_id)
        # Explicit override takes precedence
        budget.set_caller_budget(cid, 0.10)
        assert budget.get_caller_budget(cid) == 0.10

    def test_unknown_thread_returns_none(self, fresh_dbs):
        cid = inference_worker.caller_id_for("th-nonexistent")
        assert budget.get_caller_budget(cid) is None

    def test_non_thread_caller_returns_none(self, fresh_dbs):
        assert budget.get_caller_budget("scheduled_job:foo") is None


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------


class TestEndToEndPipeline:
    def test_inference_flows_through_bootstrap(self, fresh_dbs):
        """Bootstrap; create a Thread; transition into AWAITING_INFERENCE;
        verify the queue gets an entry; process it; verify the FSM
        advances and a Resolution Surface card publishes."""

        bootstrap.bootstrap_v5()

        # Custom inference runner so process_one_pending succeeds
        def runner(prompt, schema, tier, thread):
            return {
                "payload": {"intent": "schedule a call"},
                "confidence": 0.9, "model": "test", "cost_usd": 0.001,
                "trace_pointer": None,
            }
        inference.set_llm_runner(runner)

        # Capture published ResolutionRequests for assertion
        published: list = []
        with patch.object(resolution_surface, "publish",
                          side_effect=lambda rr: published.append(rr) or None):
            t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
            store.insert_thread(t)

            # Confirm the (placeholder) intent — moves to AWAITING_INFERENCE.
            # The bootstrap-registered handler will enqueue.
            engine.transition(
                t.thread_id, TRIG_CONFIRMED,
                data={"target": "context"},  # explicit next target
            )

            # An entry should be in the queue
            pending = queue.peek_pending(caller_kind="thread")
            assert len(pending) == 1
            assert pending[0].target == "context"

            # Worker processes it
            summary = inference_worker.process_one_pending("w-1")
            assert summary["outcome"] == "done"
            assert summary["next_state"] == "awaiting_context_confirmation"

        # Should have published at least 2 cards: the
        # AWAITING_INTENT_CONFIRMATION (no — that was the source)
        # and the AWAITING_CONTEXT_CONFIRMATION.
        # Actually we entered FROM awaiting_intent_confirmation,
        # so only the destination card publishes. AWAITING_INFERENCE
        # is not a wait state, so its handler enqueues but doesn't
        # publish. After process_one_pending, we land at
        # awaiting_context_confirmation → publish.
        assert any(
            rr.fsm_state == FSMState.AWAITING_CONTEXT_CONFIRMATION
            for rr in published
        )

    def test_budget_rejection_through_bootstrap(self, fresh_dbs):
        """A thread whose autonomy_policy.budget_usd is exhausted
        should auto-escalate to a clarification state on the next
        inference enqueue."""
        bootstrap.bootstrap_v5()

        # Pre-load the Thread's cumulative cost to exceed budget
        budget.register_cost_source("thread", lambda cid: 99.0)

        custom_policy = autonomy.compose(
            autonomy.PLAN_THEN_REVIEW, {"budget_usd": 0.01},
        )
        t = Thread(
            autonomy_policy=custom_policy,
            fsm_state=FSMState.INFERRING_INTENT,
        )
        store.insert_thread(t)

        # Trigger an enqueue — should be rejected, FSM escalates
        eid = inference_worker.enqueue_inference_for_thread(
            t, InferenceTarget.INTENT, estimated_cost_usd=0.05,
        )
        assert eid is None  # rejection signal

        # FSM advanced to clarification
        fetched = store.get_thread(t.thread_id)
        assert fetched.fsm_state == FSMState.AWAITING_INTENT_CLARIFICATION
