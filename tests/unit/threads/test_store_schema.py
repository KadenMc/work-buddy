"""v5 Stage 1.3 — schema migration + minimum CRUD for Threads.

Pins:
- threads + thread_events tables exist on a fresh DB.
- Round-trip: insert → get → list → update_state.
- Event log: append_event → list_events → latest_event_id.
- Optimistic lock: parent_event_id mismatch raises.
- Cross-Thread linked events via migration_id.
- Validation: unknown event kinds rejected at submit.

DESIGN.md §13 (event log) is the spec.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    ACTOR_AGENT,
    ACTOR_FSM_ENGINE,
    KIND_INCITING_EVENT,
    KIND_INTENT_INFERRED,
    KIND_THREAD_CREATED,
    KIND_CONTEXT_ADDED,
    KIND_CONTEXT_REMOVED,
    OptimisticLockConflict,
    ThreadEvent,
)
from work_buddy.threads.models import (
    AutonomyPolicy,
    ContextItem,
    Task,
    Thread,
)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_threads_table_exists(self, fresh_db):
        conn = store.get_connection()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        names = {r["name"] for r in rows}
        assert "threads" in names
        assert "thread_events" in names

    def test_threads_columns(self, fresh_db):
        conn = store.get_connection()
        try:
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(threads)")
            }
        finally:
            conn.close()
        # Pin every load-bearing column so future migrations don't
        # silently drop one.
        expected = {
            "thread_id", "parent_id", "subtype", "fsm_state",
            "parent_event_id", "autonomy_policy_json",
            "context_items_json", "risk_profile_json",
            "inciting_event_summary_json", "current_focus_thread_id",
            "created_at", "updated_at", "archived_at",
        }
        assert expected.issubset(cols), f"missing: {expected - cols}"

    def test_thread_events_columns(self, fresh_db):
        conn = store.get_connection()
        try:
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(thread_events)")
            }
        finally:
            conn.close()
        expected = {
            "id", "thread_id", "kind", "actor", "inference_tier",
            "timestamp", "data_json", "parent_event_id", "migration_id",
        }
        assert expected.issubset(cols), f"missing: {expected - cols}"

    def test_indexes_present(self, fresh_db):
        conn = store.get_connection()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        finally:
            conn.close()
        names = {r["name"] for r in rows}
        for n in (
            "idx_threads_parent",
            "idx_threads_state",
            "idx_threads_subtype",
            "idx_thread_events_thread_kind",
            "idx_thread_events_thread_id_pk",
            "idx_thread_events_migration",
        ):
            assert n in names, f"missing index {n}"

    def test_foreign_keys_enforced(self, fresh_db):
        conn = store.get_connection()
        try:
            # thread_events.thread_id → threads.thread_id
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO thread_events "
                    "(thread_id, kind, actor, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    ("th-nonexistent", KIND_INTENT_INFERRED, ACTOR_AGENT,
                     "2026-05-02T00:00:00+00:00"),
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Thread CRUD
# ---------------------------------------------------------------------------


class TestThreadCRUD:
    def test_insert_and_get(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        fetched = store.get_thread(t.thread_id)
        assert fetched is not None
        assert fetched.thread_id == t.thread_id
        assert fetched.fsm_state == FSMState.PROPOSED

    def test_get_unknown_returns_none(self, fresh_db):
        assert store.get_thread("th-does-not-exist") is None

    def test_insert_with_context_items_round_trips(self, fresh_db):
        items = (
            ContextItem(id="tab-1", source="chrome", type="tab", label="A"),
            ContextItem(id="tab-2", source="chrome", type="tab", label="B"),
        )
        t = Thread(context_items=items)
        store.insert_thread(t)
        fetched = store.get_thread(t.thread_id)
        assert len(fetched.context_items) == 2
        assert {c.id for c in fetched.context_items} == {"tab-1", "tab-2"}

    def test_insert_with_autonomy_policy_round_trips(self, fresh_db):
        ap = AutonomyPolicy(budget_usd=2.5, inference_confidence_floor=0.7)
        t = Thread(autonomy_policy=ap)
        store.insert_thread(t)
        fetched = store.get_thread(t.thread_id)
        assert fetched.autonomy_policy.budget_usd == 2.5
        assert fetched.autonomy_policy.inference_confidence_floor == 0.7

    def test_subtype_column_persists(self, fresh_db):
        task = Task()
        store.insert_thread(task)
        fetched = store.get_thread(task.thread_id)
        assert fetched.subtype == "task"
        assert fetched.is_task

    def test_parent_id_chain(self, fresh_db):
        parent = Thread()
        child = Thread(parent_id=parent.thread_id)
        store.insert_thread(parent)
        store.insert_thread(child)
        fetched_child = store.get_thread(child.thread_id)
        assert fetched_child.parent_id == parent.thread_id

    def test_list_threads_filters_by_state(self, fresh_db):
        a = Thread(fsm_state=FSMState.PROPOSED)
        b = Thread(fsm_state=FSMState.AWAITING_CONFIRMATION)
        c = Thread(fsm_state=FSMState.AWAITING_CONFIRMATION)
        for t in (a, b, c):
            store.insert_thread(t)

        rows = store.list_threads(state="awaiting_confirmation")
        assert {r.thread_id for r in rows} == {b.thread_id, c.thread_id}

    def test_list_threads_filters_by_subtype(self, fresh_db):
        plain = Thread()
        task = Task()
        store.insert_thread(plain)
        store.insert_thread(task)

        rows = store.list_threads(subtype="task")
        assert [r.thread_id for r in rows] == [task.thread_id]

    def test_list_threads_filters_by_parent(self, fresh_db):
        parent = Thread()
        child_a = Thread(parent_id=parent.thread_id)
        child_b = Thread(parent_id=parent.thread_id)
        store.insert_thread(parent)
        store.insert_thread(child_a)
        store.insert_thread(child_b)

        rows = store.list_threads(parent_id=parent.thread_id)
        assert {r.thread_id for r in rows} == {
            child_a.thread_id, child_b.thread_id,
        }

    def test_update_state_writes_cache(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        ok = store.update_thread_state(
            t.thread_id,
            fsm_state=FSMState.AWAITING_INFERENCE.value,
            parent_event_id=42,
        )
        assert ok

        fetched = store.get_thread(t.thread_id)
        assert fetched.fsm_state == FSMState.AWAITING_INFERENCE
        assert fetched.parent_event_id == 42

    def test_update_unknown_returns_false(self, fresh_db):
        assert store.update_thread_state(
            "th-unknown", fsm_state="awaiting_inference",
        ) is False


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


class TestEventLog:
    def test_append_and_list(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        e1 = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INCITING_EVENT,
            actor="inciting", data={"source": "test"},
        ))
        e2 = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_THREAD_CREATED,
            actor=ACTOR_FSM_ENGINE, parent_event_id=e1.id,
        ))

        assert e1.id is not None
        assert e2.id is not None
        assert e2.id > e1.id

        events = store.list_events(t.thread_id)
        assert [e.kind for e in events] == [
            KIND_INCITING_EVENT, KIND_THREAD_CREATED,
        ]
        assert events[0].data == {"source": "test"}

    def test_append_validates_kind(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        with pytest.raises(ValueError):
            store.append_event(ThreadEvent(
                thread_id=t.thread_id, kind="garbage", actor=ACTOR_AGENT,
            ))

    def test_optimistic_lock_conflict_raises(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        e1 = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INCITING_EVENT,
            actor="inciting",
        ))
        # Actor B reads, expects parent_event_id=e1.id, but Actor A
        # lands a new event first.
        store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_THREAD_CREATED,
            actor=ACTOR_FSM_ENGINE, parent_event_id=e1.id,
        ))
        with pytest.raises(OptimisticLockConflict):
            store.append_event(ThreadEvent(
                thread_id=t.thread_id, kind=KIND_INTENT_INFERRED,
                actor=ACTOR_FSM_ENGINE, parent_event_id=e1.id,
            ))

    def test_optimistic_lock_passes_on_match(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        e1 = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INCITING_EVENT, actor="inciting",
        ))
        # parent matches the actual latest → should land cleanly
        e2 = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_THREAD_CREATED,
            actor=ACTOR_FSM_ENGINE, parent_event_id=e1.id,
        ))
        assert e2.id is not None

    def test_optimistic_lock_skipped_when_no_parent(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        # First-ever event for a thread: parent_event_id=None bypasses
        # the lock check.
        e = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INCITING_EVENT, actor="inciting",
        ))
        assert e.id is not None

    def test_latest_event_id(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        assert store.latest_event_id(t.thread_id) is None

        e = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INCITING_EVENT, actor="inciting",
        ))
        assert store.latest_event_id(t.thread_id) == e.id

    def test_list_events_filters_by_kind(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INCITING_EVENT, actor="inciting",
        ))
        e2 = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INTENT_INFERRED,
            actor=ACTOR_AGENT, inference_tier="frontier_fast",
        ))
        events = store.list_events(t.thread_id, kinds=[KIND_INTENT_INFERRED])
        assert len(events) == 1
        assert events[0].id == e2.id
        assert events[0].inference_tier == "frontier_fast"

    def test_cross_thread_linked_events_via_migration_id(self, fresh_db):
        a = Thread()
        b = Thread()
        store.insert_thread(a)
        store.insert_thread(b)

        store.append_event(ThreadEvent(
            thread_id=a.thread_id, kind=KIND_CONTEXT_REMOVED,
            actor=ACTOR_FSM_ENGINE,
            data={"item_id": "tab-1"},
            migration_id="mig-abc",
        ))
        store.append_event(ThreadEvent(
            thread_id=b.thread_id, kind=KIND_CONTEXT_ADDED,
            actor=ACTOR_FSM_ENGINE,
            data={"item_id": "tab-1"},
            migration_id="mig-abc",
        ))

        linked = store.get_linked_events("mig-abc")
        assert len(linked) == 2
        assert {e.thread_id for e in linked} == {a.thread_id, b.thread_id}
        assert {e.kind for e in linked} == {
            KIND_CONTEXT_ADDED, KIND_CONTEXT_REMOVED,
        }

    def test_event_data_round_trips(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        payload = {"intent": "schedule", "supporting_refs": ["ref-1", "ref-2"]}
        e = store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INTENT_INFERRED,
            actor=ACTOR_AGENT, data=payload, inference_tier="frontier_fast",
        ))
        events = store.list_events(t.thread_id)
        assert events[0].data == payload
        assert events[0].id == e.id


# ---------------------------------------------------------------------------
# Cascading delete (parent → children, parent → events)
# ---------------------------------------------------------------------------


class TestCascadeDelete:
    def test_deleting_parent_cascades_to_children(self, fresh_db):
        parent = Thread()
        child = Thread(parent_id=parent.thread_id)
        store.insert_thread(parent)
        store.insert_thread(child)

        conn = store.get_connection()
        try:
            conn.execute(
                "DELETE FROM threads WHERE thread_id = ?", (parent.thread_id,)
            )
            conn.commit()
        finally:
            conn.close()

        assert store.get_thread(parent.thread_id) is None
        assert store.get_thread(child.thread_id) is None

    def test_deleting_thread_cascades_to_events(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id, kind=KIND_INCITING_EVENT, actor="inciting",
        ))

        conn = store.get_connection()
        try:
            conn.execute(
                "DELETE FROM threads WHERE thread_id = ?", (t.thread_id,)
            )
            conn.commit()
        finally:
            conn.close()
        assert store.list_events(t.thread_id) == []
