"""v5 Stage 2.8 — sub-thread spawning + decompose Standard Action.

Pins:
- decompose_thread spawns one sub-Thread per source item with
  parent_id set; parent goes to MONITORING.
- Sub-threads inherit autonomy from parent; override-DOWN allowed
  via autonomy_override; override-UP rejected.
- cascade_terminal_to_parent advances the parent to DONE only when
  ALL children are terminal.
- force_close_parent dismisses the parent and cascades
  parent_force_close to live children.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import autonomy, decompose, engine, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_SUBTHREAD_TERMINAL_REPORTED,
    KIND_SUBTHREADS_SPAWNED,
)
from work_buddy.threads.fsm import (
    TRIG_DISMISSED_BY_USER,
    TRIG_EXECUTION_DONE,
)
from work_buddy.threads.models import AutonomyPolicy, ContextItem, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    engine.clear_state_entry_handlers()
    yield db
    engine.clear_state_entry_handlers()


@pytest.fixture
def parent(fresh_db):
    p = Thread(
        autonomy_policy=autonomy.PLAN_THEN_REVIEW,
        fsm_state=FSMState.AWAITING_CONFIRMATION,
    )
    store.insert_thread(p)
    return p


def _items(*labels):
    return [
        ContextItem(id=f"item-{i}", source="test", type="x", label=l)
        for i, l in enumerate(labels)
    ]


# ---------------------------------------------------------------------------
# decompose_thread
# ---------------------------------------------------------------------------


class TestDecomposeThread:
    def test_spawns_n_children(self, parent):
        ids = decompose.decompose_thread(
            parent.thread_id,
            _items("a", "b", "c"),
        )
        assert len(ids) == 3
        children = store.list_threads(parent_id=parent.thread_id)
        assert {c.thread_id for c in children} == set(ids)

    def test_parent_transitions_to_monitoring(self, parent):
        decompose.decompose_thread(parent.thread_id, _items("x"))
        fetched = store.get_thread(parent.thread_id)
        assert fetched.fsm_state == FSMState.MONITORING

    def test_children_inherit_parent_autonomy(self, parent):
        ids = decompose.decompose_thread(parent.thread_id, _items("x"))
        child = store.get_thread(ids[0])
        assert child.autonomy_policy == parent.autonomy_policy

    def test_children_carry_source_context_item(self, parent):
        ids = decompose.decompose_thread(parent.thread_id, _items("hello"))
        child = store.get_thread(ids[0])
        assert len(child.context_items) == 1
        assert child.context_items[0].label == "hello"

    def test_inciting_event_summary_records_parent(self, parent):
        ids = decompose.decompose_thread(parent.thread_id, _items("x"))
        child = store.get_thread(ids[0])
        assert child.inciting_event_summary["source"] == "decompose"
        assert child.inciting_event_summary["parent_id"] == parent.thread_id

    def test_subthreads_spawned_event_recorded(self, parent):
        ids = decompose.decompose_thread(parent.thread_id, _items("a", "b"))
        events = store.list_events(parent.thread_id, kinds=[KIND_SUBTHREADS_SPAWNED])
        assert len(events) == 1
        assert sorted(events[0].data["child_thread_ids"]) == sorted(ids)
        assert events[0].data["source_count"] == 2

    def test_empty_source_raises(self, parent):
        with pytest.raises(decompose.DecomposeRefused):
            decompose.decompose_thread(parent.thread_id, [])

    def test_unknown_parent_raises(self, fresh_db):
        with pytest.raises(decompose.DecomposeRefused):
            decompose.decompose_thread("th-nonexistent", _items("a"))

    def test_autonomy_override_down_accepted(self, parent):
        # parent uses PLAN_THEN_REVIEW; child overrides to HANDS_OFF
        # which is more conservative on every axis.
        ids = decompose.decompose_thread(
            parent.thread_id,
            _items("x"),
            autonomy_override=autonomy.HANDS_OFF,
        )
        child = store.get_thread(ids[0])
        assert child.autonomy_policy == autonomy.HANDS_OFF

    def test_autonomy_override_up_rejected(self, fresh_db):
        # Setup: parent uses HANDS_OFF, child tries END_TO_END (widening)
        p = Thread(
            autonomy_policy=autonomy.HANDS_OFF,
            fsm_state=FSMState.AWAITING_CONFIRMATION,
        )
        store.insert_thread(p)
        with pytest.raises(decompose.DecomposeRefused) as exc_info:
            decompose.decompose_thread(
                p.thread_id, _items("x"),
                autonomy_override=autonomy.END_TO_END,
            )
        assert "widen" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# cascade_terminal_to_parent
# ---------------------------------------------------------------------------


class TestCascade:
    def _setup_with_children(self, parent_state=FSMState.AWAITING_CONFIRMATION):
        p = Thread(autonomy_policy=autonomy.PLAN_THEN_REVIEW,
                   fsm_state=parent_state)
        store.insert_thread(p)
        ids = decompose.decompose_thread(p.thread_id, _items("a", "b"))
        return p.thread_id, ids

    def test_cascade_no_op_if_not_all_terminal(self, fresh_db):
        parent_id, child_ids = self._setup_with_children()
        # Mark only one child terminal
        store.update_thread_state(
            child_ids[0], fsm_state=FSMState.DONE.value,
        )
        out = decompose.cascade_terminal_to_parent(child_ids[0])
        # No transition fired
        assert out is None
        assert store.get_thread(parent_id).fsm_state == FSMState.MONITORING

    def test_cascade_advances_parent_to_done_when_all_terminal(self, fresh_db):
        parent_id, child_ids = self._setup_with_children()
        # Mark all children terminal (cache + cascade individually)
        for cid in child_ids:
            store.update_thread_state(cid, fsm_state=FSMState.DONE.value)
        # Final cascade — parent should advance
        out = decompose.cascade_terminal_to_parent(child_ids[-1])
        assert out == FSMState.DONE.value
        assert store.get_thread(parent_id).fsm_state == FSMState.DONE

    def test_cascade_records_child_terminal_report(self, fresh_db):
        parent_id, child_ids = self._setup_with_children()
        store.update_thread_state(child_ids[0], fsm_state=FSMState.DONE.value)
        decompose.cascade_terminal_to_parent(child_ids[0])
        events = store.list_events(
            parent_id, kinds=[KIND_SUBTHREAD_TERMINAL_REPORTED],
        )
        assert len(events) == 1
        assert events[0].data["child_thread_id"] == child_ids[0]
        assert events[0].data["child_terminal_state"] == "done"

    def test_cascade_no_op_for_orphan_thread(self, fresh_db):
        # Thread with no parent_id
        t = Thread(fsm_state=FSMState.DONE)
        store.insert_thread(t)
        assert decompose.cascade_terminal_to_parent(t.thread_id) is None

    def test_cascade_no_op_when_parent_not_monitoring(self, fresh_db):
        # Parent at AWAITING_CONFIRMATION (decompose hasn't been called)
        p = Thread(fsm_state=FSMState.AWAITING_CONFIRMATION)
        store.insert_thread(p)
        c = Thread(parent_id=p.thread_id, fsm_state=FSMState.DONE)
        store.insert_thread(c)
        assert decompose.cascade_terminal_to_parent(c.thread_id) is None

    def test_register_cascade_handlers_wires_terminals(self, fresh_db):
        decompose.register_cascade_handlers()
        # Walking a child to DONE via the engine should now trigger
        # cascade automatically.
        parent_id, child_ids = self._setup_with_children()

        # Push first child to DONE through the engine (executing → done)
        store.update_thread_state(child_ids[0], fsm_state=FSMState.EXECUTING.value)
        engine.transition(
            child_ids[0], TRIG_EXECUTION_DONE,
            data={"requires_post_review": False},
        )
        # Parent shouldn't have advanced yet (still one child live)
        assert store.get_thread(parent_id).fsm_state == FSMState.MONITORING

        # Walk the second child terminal too
        store.update_thread_state(child_ids[1], fsm_state=FSMState.EXECUTING.value)
        engine.transition(
            child_ids[1], TRIG_EXECUTION_DONE,
            data={"requires_post_review": False},
        )
        # Now the parent should have advanced
        assert store.get_thread(parent_id).fsm_state == FSMState.DONE


# ---------------------------------------------------------------------------
# force_close_parent
# ---------------------------------------------------------------------------


class TestForceClose:
    def test_force_close_dismisses_parent_and_live_children(self, fresh_db):
        p = Thread(
            autonomy_policy=autonomy.PLAN_THEN_REVIEW,
            fsm_state=FSMState.AWAITING_CONFIRMATION,
        )
        store.insert_thread(p)
        ids = decompose.decompose_thread(p.thread_id, _items("a", "b"))
        # Mark one child already terminal — should be skipped
        store.update_thread_state(ids[0], fsm_state=FSMState.DONE.value)

        result = decompose.force_close_parent(p.thread_id)
        assert result["closed_parent"] is True
        assert ids[1] in result["cascaded"]
        assert ids[0] not in result["cascaded"]

        # The live child is now dismissed
        assert store.get_thread(ids[1]).fsm_state == FSMState.DISMISSED
        # The parent is dismissed
        assert store.get_thread(p.thread_id).fsm_state == FSMState.DISMISSED

    def test_force_close_unknown_parent_raises(self, fresh_db):
        with pytest.raises(decompose.DecomposeRefused):
            decompose.force_close_parent("th-nope")
