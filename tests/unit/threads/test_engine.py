"""v5 Stage 2.2 — FSM engine.

Pins:
- ``transition()`` looks up the (state, trigger) cell, applies the
  transition under optimistic lock, writes a ``state_transition``
  event, and updates the threads.fsm_state cache.
- Branch resolver handles EXECUTING/execution_done →
  done_or_review and MONITORING/execution_done →
  done_when_all_subthreads_terminal.
- Optimistic-lock conflicts surface as OptimisticLockConflict.
- State-entry side effects fire on transition into the new state.
- Invalid (state, trigger) cells raise InvalidTransition.

DESIGN.md §7.6 / §7.7 / §13.3 are the spec.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import engine, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_INCITING_EVENT,
    KIND_STATE_TRANSITION,
    OptimisticLockConflict,
    ThreadEvent,
)
from work_buddy.threads.fsm import (
    TRIG_CONFIRMED,
    TRIG_DISMISSED_BY_USER,
    TRIG_EXECUTE,
    TRIG_EXECUTION_DONE,
    TRIG_INFERENCE_DONE,
    TRIG_PROVIDED,
    TRIG_REDIRECTED,
    TRIG_REVIEW_ACCEPTED,
)
from work_buddy.threads.models import Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    engine.clear_state_entry_handlers()
    yield db
    engine.clear_state_entry_handlers()


@pytest.fixture
def proposed_thread(fresh_db):
    """A Thread sitting at PROPOSED, no events yet."""
    t = Thread()
    store.insert_thread(t)
    return t


# ---------------------------------------------------------------------------
# Basic transition flow
# ---------------------------------------------------------------------------


class TestBasicTransition:
    def test_inferring_intent_done_advances_to_confirmation(self, fresh_db):
        # Push the Thread into INFERRING_INTENT first. The default
        # AutonomyPolicy() has empty auto_advance_states, so the
        # autonomy resolver will route to AWAITING_INTENT_CONFIRMATION
        # (no auto-advance).
        t = Thread(fsm_state=FSMState.INFERRING_INTENT)
        store.insert_thread(t)

        result = engine.transition(t.thread_id, TRIG_INFERENCE_DONE,
                                   data={"intent": "schedule"})
        assert result.prev_state == FSMState.INFERRING_INTENT
        assert result.next_state == FSMState.AWAITING_INTENT_CONFIRMATION

        # Cache reflects the new state
        fetched = store.get_thread(t.thread_id)
        assert fetched.fsm_state == FSMState.AWAITING_INTENT_CONFIRMATION

        # The transition wrote two events: the canonical
        # state_transition + the autonomy audit. The audit is a
        # follow-up record; the state_transition is what advances
        # the thread.
        events = store.list_events(t.thread_id)
        kinds = [e.kind for e in events]
        assert KIND_STATE_TRANSITION in kinds
        st_event = next(e for e in events if e.kind == KIND_STATE_TRANSITION)
        assert st_event.data["from"] == "inferring_intent"
        assert st_event.data["to"] == "awaiting_intent_confirmation"
        assert st_event.data["intent"] == "schedule"
        # The audit event records the no-auto-advance decision.
        from work_buddy.threads.events import KIND_AUTO_ADVANCE_DECISION
        assert KIND_AUTO_ADVANCE_DECISION in kinds
        audit = next(
            e for e in events if e.kind == KIND_AUTO_ADVANCE_DECISION
        )
        assert audit.data["advance"] is False
        assert audit.data["target"] == "intent"

    def test_dismiss_from_proposed_lands_terminal(self, proposed_thread):
        result = engine.transition(
            proposed_thread.thread_id, TRIG_DISMISSED_BY_USER,
        )
        assert result.next_state == FSMState.DISMISSED
        fetched = store.get_thread(proposed_thread.thread_id)
        assert fetched.fsm_state == FSMState.DISMISSED

    def test_redirect_from_confirmation_loops_back_to_inference(self, fresh_db):
        t = Thread(fsm_state=FSMState.AWAITING_CONTEXT_CONFIRMATION)
        store.insert_thread(t)

        result = engine.transition(t.thread_id, TRIG_REDIRECTED,
                                   data={"feedback": "different project"})
        assert result.next_state == FSMState.AWAITING_INFERENCE


class TestBranchedTransitions:
    def test_executing_done_without_review_flag_goes_to_done(self, fresh_db):
        t = Thread(fsm_state=FSMState.EXECUTING)
        store.insert_thread(t)
        result = engine.transition(
            t.thread_id, TRIG_EXECUTION_DONE,
            data={"requires_post_review": False},
        )
        assert result.next_state == FSMState.DONE

    def test_executing_done_with_review_flag_goes_to_review(self, fresh_db):
        t = Thread(fsm_state=FSMState.EXECUTING)
        store.insert_thread(t)
        result = engine.transition(
            t.thread_id, TRIG_EXECUTION_DONE,
            data={"requires_post_review": True},
        )
        assert result.next_state == FSMState.AWAITING_REVIEW

    def test_monitoring_done_with_all_terminal(self, fresh_db):
        t = Thread(fsm_state=FSMState.MONITORING)
        store.insert_thread(t)
        result = engine.transition(
            t.thread_id, TRIG_EXECUTION_DONE,
            data={"all_terminal": True},
        )
        assert result.next_state == FSMState.DONE

    def test_monitoring_done_partial_stays_monitoring(self, fresh_db):
        t = Thread(fsm_state=FSMState.MONITORING)
        store.insert_thread(t)
        result = engine.transition(
            t.thread_id, TRIG_EXECUTION_DONE,
            data={"all_terminal": False},
        )
        assert result.next_state == FSMState.MONITORING

    def test_custom_branch_resolver_overrides_default(self, fresh_db):
        t = Thread(fsm_state=FSMState.EXECUTING)
        store.insert_thread(t)

        def my_resolver(ctx):
            return FSMState.AWAITING_REVIEW  # always review

        result = engine.transition(
            t.thread_id, TRIG_EXECUTION_DONE,
            data={"requires_post_review": False},
            branch_resolver=my_resolver,
        )
        assert result.next_state == FSMState.AWAITING_REVIEW


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrors:
    def test_unknown_thread_raises(self, fresh_db):
        with pytest.raises(engine.ThreadNotFound):
            engine.transition("th-nonexistent", TRIG_CONFIRMED)

    def test_invalid_trigger_raises(self, proposed_thread):
        # PROPOSED + EXECUTE is empty in the table
        with pytest.raises(engine.InvalidTransition):
            engine.transition(proposed_thread.thread_id, TRIG_EXECUTE)

    def test_terminal_state_rejects_all_triggers(self, fresh_db):
        t = Thread(fsm_state=FSMState.DONE)
        store.insert_thread(t)
        for trig in (TRIG_CONFIRMED, TRIG_INFERENCE_DONE, TRIG_PROVIDED):
            with pytest.raises(engine.InvalidTransition):
                engine.transition(t.thread_id, trig)


# ---------------------------------------------------------------------------
# Optimistic locking
# ---------------------------------------------------------------------------


class TestOptimisticLock:
    def test_explicit_parent_event_id_match(self, fresh_db):
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        # Stamp a prior event so parent_event_id is meaningful
        e = store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
        ))
        # Caller passes the matching parent_event_id → success
        result = engine.transition(
            t.thread_id, TRIG_CONFIRMED,
            parent_event_id=e.id,
        )
        assert result.next_state == FSMState.AWAITING_INFERENCE

    def test_explicit_parent_event_id_mismatch_raises(self, fresh_db):
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        e = store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
        ))
        # A second writer has landed an event since we read
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
            parent_event_id=e.id,
        ))
        # We still think parent is e.id → conflict
        with pytest.raises(OptimisticLockConflict):
            engine.transition(
                t.thread_id, TRIG_CONFIRMED,
                parent_event_id=e.id,
            )

    def test_default_uses_thread_parent_event_id(self, fresh_db):
        # When the caller doesn't pass parent_event_id, the engine
        # uses the thread's stored value. After one transition the
        # cache should have been bumped — a second transition must
        # consult the bumped value.
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        first = engine.transition(t.thread_id, TRIG_CONFIRMED)
        # Cache now points at first.event_id; another transition is
        # only valid from AWAITING_INFERENCE — push the FSM
        # forward via the path:
        # awaiting_inference (current) → it has only dismissed/parent_force_close out
        # so the next legal transition is dismiss
        second = engine.transition(
            t.thread_id, TRIG_DISMISSED_BY_USER,
        )
        # Sanity: second event's parent_event_id is the first's id
        events = store.list_events(t.thread_id)
        assert events[1].parent_event_id == first.event_id


# ---------------------------------------------------------------------------
# Side-effect dispatch
# ---------------------------------------------------------------------------


class TestSideEffects:
    def test_handler_fires_on_state_entry(self, fresh_db):
        called: list[engine.TransitionResult] = []
        engine.register_state_entry_handler(
            FSMState.AWAITING_INFERENCE,
            lambda r: called.append(r),
        )
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        result = engine.transition(t.thread_id, TRIG_CONFIRMED)
        assert len(called) == 1
        assert called[0].thread_id == t.thread_id
        assert called[0].next_state == FSMState.AWAITING_INFERENCE
        assert called[0] is result

    def test_handler_isolation_between_states(self, fresh_db):
        confirmed_calls: list = []
        review_calls: list = []
        engine.register_state_entry_handler(
            FSMState.AWAITING_INFERENCE,
            lambda r: confirmed_calls.append(r),
        )
        engine.register_state_entry_handler(
            FSMState.AWAITING_REVIEW,
            lambda r: review_calls.append(r),
        )
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        engine.transition(t.thread_id, TRIG_CONFIRMED)
        assert len(confirmed_calls) == 1
        assert len(review_calls) == 0

    def test_multiple_handlers_per_state(self, fresh_db):
        a, b = [], []
        engine.register_state_entry_handler(
            FSMState.AWAITING_INFERENCE, lambda r: a.append(r),
        )
        engine.register_state_entry_handler(
            FSMState.AWAITING_INFERENCE, lambda r: b.append(r),
        )
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        engine.transition(t.thread_id, TRIG_CONFIRMED)
        assert len(a) == 1
        assert len(b) == 1

    def test_handler_exception_does_not_abort_transition(self, fresh_db):
        engine.register_state_entry_handler(
            FSMState.AWAITING_INFERENCE,
            lambda r: (_ for _ in ()).throw(RuntimeError("oops")),
        )
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        # Transition still succeeds even though the handler raised
        result = engine.transition(t.thread_id, TRIG_CONFIRMED)
        assert result.next_state == FSMState.AWAITING_INFERENCE
        # And the cache was updated
        assert store.get_thread(t.thread_id).fsm_state == FSMState.AWAITING_INFERENCE

    def test_fire_side_effects_can_be_disabled(self, fresh_db):
        called: list = []
        engine.register_state_entry_handler(
            FSMState.AWAITING_INFERENCE, lambda r: called.append(r),
        )
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        engine.transition(
            t.thread_id, TRIG_CONFIRMED, fire_side_effects=False,
        )
        assert called == []


# ---------------------------------------------------------------------------
# Reachability helper
# ---------------------------------------------------------------------------


class TestReachability:
    def test_executing_reaches_done_or_review_or_redirect(self):
        reach = engine.reachable_states_from(FSMState.EXECUTING)
        # done_or_review branch + execution_failed
        assert FSMState.DONE in reach
        assert FSMState.AWAITING_REVIEW in reach
        assert FSMState.AWAITING_REDIRECT in reach

    def test_terminal_states_reach_nothing(self):
        for s in (FSMState.DONE, FSMState.DISMISSED, FSMState.HANDED_OFF):
            assert engine.reachable_states_from(s) == set()


# ---------------------------------------------------------------------------
# End-to-end: walk a Thread through several transitions
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_walk_inference_to_done(self, fresh_db):
        # A Thread that proceeds: inferring_intent → confirmation →
        # awaiting_inference → ... eventually executing → done.
        t = Thread(fsm_state=FSMState.INFERRING_INTENT)
        store.insert_thread(t)

        engine.transition(t.thread_id, TRIG_INFERENCE_DONE)
        engine.transition(t.thread_id, TRIG_CONFIRMED)
        # We're now at awaiting_inference. Manual leap into
        # inferring_action to test the action-side branches:
        store.update_thread_state(t.thread_id, fsm_state="inferring_action")
        engine.transition(t.thread_id, TRIG_INFERENCE_DONE)
        # awaiting_confirmation → executing
        engine.transition(t.thread_id, TRIG_EXECUTE)
        # executing → done (no review)
        result = engine.transition(
            t.thread_id, TRIG_EXECUTION_DONE,
            data={"requires_post_review": False},
        )
        assert result.next_state == FSMState.DONE

        # Five state_transition events plus two auto_advance_decision
        # audit events (one for intent_review_or_advance, one for
        # action_review_or_execute) = 7 total. The default
        # AutonomyPolicy() doesn't auto-advance, so each branch
        # resolver lands on the surface-to-user state and records its
        # decision.
        events = store.list_events(t.thread_id)
        from work_buddy.threads.events import (
            KIND_AUTO_ADVANCE_DECISION,
        )
        st_events = [e for e in events if e.kind == KIND_STATE_TRANSITION]
        audit_events = [e for e in events if e.kind == KIND_AUTO_ADVANCE_DECISION]
        assert len(st_events) == 5
        assert len(audit_events) == 2
        assert all(a.data["advance"] is False for a in audit_events)

    def test_walk_with_redirect(self, fresh_db):
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        engine.transition(t.thread_id, TRIG_REDIRECTED,
                          data={"feedback": "wrong project"})
        assert store.get_thread(t.thread_id).fsm_state == FSMState.AWAITING_INFERENCE
