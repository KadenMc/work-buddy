"""v5 Stage 2.4 — sidecar inference worker pipeline.

Pins:
- enqueue_inference_for_thread publishes into the LLM-call queue
  with caller_id='thread:<id>'.
- AWAITING_INFERENCE state-entry handler reads target/priority
  from transition data and enqueues.
- process_one_pending claims one entry, transitions Thread to
  INFERRING_*, runs inference, transitions to AWAITING_*_CONFIRMATION,
  marks queue entry done.
- Inference exceptions → queue.fail + TRIG_INFERENCE_FAILED →
  AWAITING_*_CLARIFICATION.
- Budget rejection on enqueue → TRIG_INFERENCE_FAILED.
- Caller-id round-trip: caller_id_for / thread_id_from_caller.
- next_inference_target walks intent → context → action.
- Poller loop processes N pending entries.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.llm import budget, queue
from work_buddy.threads import (
    engine,
    inference,
    inference_worker as worker,
    store,
)
from work_buddy.threads.enums import (
    FSMState,
    InferenceTarget,
    ReasoningTier,
)
from work_buddy.threads.events import (
    KIND_CONTEXT_INFERRED,
    KIND_INTENT_INFERRED,
    ThreadEvent,
)
from work_buddy.threads.fsm import TRIG_CONFIRMED, TRIG_INFERENCE_DONE
from work_buddy.threads.models import Thread


@pytest.fixture
def fresh_dbs(tmp_path, monkeypatch):
    threads_db = tmp_path / "threads.db"
    queue_db = tmp_path / "queue.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    monkeypatch.setattr(queue, "_db_path", lambda: queue_db)
    queue.clear_admission_hooks()
    budget.clear_caller_budgets()
    engine.clear_state_entry_handlers()
    inference.set_llm_runner(inference._stub_runner)
    yield (threads_db, queue_db)
    queue.clear_admission_hooks()
    budget.clear_caller_budgets()
    engine.clear_state_entry_handlers()
    inference.set_llm_runner(inference._stub_runner)


# ---------------------------------------------------------------------------
# Caller-ID convention
# ---------------------------------------------------------------------------


class TestCallerID:
    def test_round_trip(self):
        cid = worker.caller_id_for("th-abc123")
        assert cid == "thread:th-abc123"
        assert worker.thread_id_from_caller(cid) == "th-abc123"

    def test_unrecognised_returns_none(self):
        assert worker.thread_id_from_caller("agent:foo") is None
        assert worker.thread_id_from_caller("garbage") is None


# ---------------------------------------------------------------------------
# next_inference_target
# ---------------------------------------------------------------------------


class TestNextTarget:
    def test_intent_first(self, fresh_dbs):
        t = Thread()
        store.insert_thread(t)
        assert worker.next_inference_target(t) == InferenceTarget.INTENT

    def test_context_after_intent(self, fresh_dbs):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INTENT_INFERRED,
            actor="agent", data={},
        ))
        assert worker.next_inference_target(t) == InferenceTarget.CONTEXT

    def test_action_after_intent_and_context(self, fresh_dbs):
        t = Thread()
        store.insert_thread(t)
        for k in (KIND_INTENT_INFERRED, KIND_CONTEXT_INFERRED):
            store.append_event(ThreadEvent(
                thread_id=t.thread_id, kind=k, actor="agent", data={},
            ))
        assert worker.next_inference_target(t) == InferenceTarget.ACTION


# ---------------------------------------------------------------------------
# enqueue_inference_for_thread
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueues_with_thread_caller_id(self, fresh_dbs):
        t = Thread()
        store.insert_thread(t)
        eid = worker.enqueue_inference_for_thread(t, InferenceTarget.INTENT)
        assert eid is not None
        entry = queue.get_entry(eid)
        assert entry.caller_id == f"thread:{t.thread_id}"
        assert entry.caller_kind == "thread"
        assert entry.target == "intent"
        assert entry.payload["thread_id"] == t.thread_id

    def test_enqueues_with_default_target_intent(self, fresh_dbs):
        t = Thread()
        store.insert_thread(t)
        eid = worker.enqueue_inference_for_thread(t)
        entry = queue.get_entry(eid)
        assert entry.target == "intent"

    def test_priority_and_tier_passthrough(self, fresh_dbs):
        t = Thread()
        store.insert_thread(t)
        eid = worker.enqueue_inference_for_thread(
            t, InferenceTarget.ACTION,
            priority=10,
            tier_hint=ReasoningTier.FRONTIER_BALANCED,
        )
        entry = queue.get_entry(eid)
        assert entry.priority == 10
        assert entry.tier_hint == "frontier_balanced"

    def test_budget_rejection_escalates_via_inference_failed(self, fresh_dbs):
        # Configure a tiny budget so the next enqueue rejects.
        budget.set_caller_budget("thread:t-x", 0.01)
        budget.register_cost_source(
            "thread", lambda cid: 0.10 if cid == "thread:t-x" else 0.0,
        )
        queue.register_admission_hook(budget.budget_admission_hook)

        # Setup: thread sitting in INFERRING_INTENT so failed
        # inference can advance to AWAITING_INTENT_CLARIFICATION.
        t = Thread(thread_id="t-x", fsm_state=FSMState.INFERRING_INTENT)
        store.insert_thread(t)

        eid = worker.enqueue_inference_for_thread(
            t, InferenceTarget.INTENT, estimated_cost_usd=0.05,
        )
        # Enqueue itself returned None (rejected)
        assert eid is None

        # FSM advanced to AWAITING_INTENT_CLARIFICATION via the
        # TRIG_INFERENCE_FAILED escalation.
        fetched = store.get_thread("t-x")
        assert fetched.fsm_state == FSMState.AWAITING_INTENT_CLARIFICATION


# ---------------------------------------------------------------------------
# AWAITING_INFERENCE state-entry handler
# ---------------------------------------------------------------------------


class TestAwaitingInferenceHandler:
    def test_handler_enqueues_with_payload_target(self, fresh_dbs):
        worker.register_inference_dispatch_handler()
        # Push thread into AWAITING_INFERENCE via a confirmation
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        engine.transition(
            t.thread_id, TRIG_CONFIRMED,
            data={"target": "context"},  # explicit target
        )
        # Should now have a queue entry with target='context'
        rows = queue.peek_pending(caller_kind="thread")
        assert len(rows) == 1
        assert rows[0].target == "context"
        assert rows[0].caller_id == f"thread:{t.thread_id}"

    def test_handler_default_target_walks_chain(self, fresh_dbs):
        worker.register_inference_dispatch_handler()
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        # No explicit target in data; default walker picks INTENT (no inferred yet)
        engine.transition(t.thread_id, TRIG_CONFIRMED)
        rows = queue.peek_pending(caller_kind="thread")
        assert len(rows) == 1
        assert rows[0].target == "intent"


# ---------------------------------------------------------------------------
# process_one_pending
# ---------------------------------------------------------------------------


class TestProcessOnePending:
    def _setup_thread_and_enqueue(self, target=InferenceTarget.INTENT):
        t = Thread(fsm_state=FSMState.AWAITING_INFERENCE)
        store.insert_thread(t)
        eid = worker.enqueue_inference_for_thread(t, target)
        return t, eid

    def test_empty_queue_returns_none(self, fresh_dbs):
        assert worker.process_one_pending("w-1") is None

    def test_happy_path_completes_queue_and_advances_fsm(self, fresh_dbs):
        t, eid = self._setup_thread_and_enqueue(InferenceTarget.INTENT)

        def runner(prompt, schema, tier, thread):
            return {
                "payload": {"intent": "schedule"},
                "confidence": 0.85,
                "model": "test-model",
                "cost_usd": 0.001,
                "trace_pointer": None,
            }
        inference.set_llm_runner(runner)

        summary = worker.process_one_pending("w-1")
        assert summary is not None
        assert summary["outcome"] == "done"
        assert summary["next_state"] == "awaiting_intent_confirmation"

        # Queue entry done with proposal payload
        entry = queue.get_entry(eid)
        assert entry.status == "done"
        assert entry.result["proposal"]["payload"]["intent"] == "schedule"

        # Thread cache reflects the new state
        thread = store.get_thread(t.thread_id)
        assert thread.fsm_state == FSMState.AWAITING_INTENT_CONFIRMATION

    def test_inference_exception_marks_queue_failed_and_advances_fsm(self, fresh_dbs):
        t, eid = self._setup_thread_and_enqueue(InferenceTarget.INTENT)

        def runner(prompt, schema, tier, thread):
            raise RuntimeError("rate-limit")
        inference.set_llm_runner(runner)

        summary = worker.process_one_pending("w-1")
        assert summary["outcome"] == "failed"
        assert summary["next_state"] == "awaiting_intent_clarification"

        entry = queue.get_entry(eid)
        assert entry.status == "failed"
        assert "rate-limit" in (entry.error_text or "")

        thread = store.get_thread(t.thread_id)
        assert thread.fsm_state == FSMState.AWAITING_INTENT_CLARIFICATION

    def test_thread_vanished_marks_queue_failed(self, fresh_dbs):
        t, eid = self._setup_thread_and_enqueue(InferenceTarget.INTENT)
        # Delete the thread row
        conn = store.get_connection()
        try:
            conn.execute(
                "DELETE FROM threads WHERE thread_id = ?", (t.thread_id,)
            )
            conn.commit()
        finally:
            conn.close()

        summary = worker.process_one_pending("w-1")
        assert summary["outcome"] == "failed"
        entry = queue.get_entry(eid)
        assert entry.status == "failed"
        assert "not found" in (entry.error_text or "").lower()

    def test_action_target_advances_to_awaiting_confirmation(self, fresh_dbs):
        t, eid = self._setup_thread_and_enqueue(InferenceTarget.ACTION)

        def runner(prompt, schema, tier, thread):
            return {
                "payload": {"kind": "standard", "name": "send_email"},
                "confidence": 0.7,
                "model": "test", "cost_usd": 0.01, "trace_pointer": None,
            }
        inference.set_llm_runner(runner)

        summary = worker.process_one_pending("w-1")
        assert summary["next_state"] == "awaiting_confirmation"


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


class TestPoller:
    def test_poller_drains_pending(self, fresh_dbs):
        # Enqueue two requests; poller processes both, then exits.
        t1 = Thread(fsm_state=FSMState.AWAITING_INFERENCE)
        t2 = Thread(fsm_state=FSMState.AWAITING_INFERENCE)
        store.insert_thread(t1)
        store.insert_thread(t2)
        worker.enqueue_inference_for_thread(t1, InferenceTarget.INTENT)
        worker.enqueue_inference_for_thread(t2, InferenceTarget.INTENT)

        # Bound the loop with max_iterations to avoid the
        # poll_interval sleep on empty.
        processed = worker.run_poller(
            "w-1",
            max_iterations=5,
            poll_interval_s=0.0,
        )
        assert processed == 2

    def test_poller_stub_process_fn(self, fresh_dbs):
        calls: list = []

        def stub(worker_id):
            if len(calls) < 3:
                calls.append(worker_id)
                return {"outcome": "done"}
            return None

        processed = worker.run_poller(
            "w-2",
            max_iterations=5,
            poll_interval_s=0.0,
            process_fn=stub,
        )
        assert processed == 3
