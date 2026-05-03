"""v5 Stage 1.10 — read-only aggregator over v4 entities.

Pins:
- task_metadata rows surface as Task(Thread) with deterministic
  agg-task-<task_id> IDs.
- task_action_items surface as sub-Threads with parent_id pointing
  at their task's aggregated id.
- ClarifyPool entries surface as Threads in awaiting_*_clarification.
- State mappings: done/archived → DONE; mit/focused → AWAITING_CONFIRMATION;
  inbox/snoozed → PROPOSED/AWAITING_REDIRECT; etc.
- ``get_thread_aggregated`` round-trips by ID prefix.
- ``is_aggregated_id`` distinguishes synthesised from real IDs.

Read-only: the aggregator never writes to v4 tables.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import aggregator
from work_buddy.threads.aggregator import (
    is_aggregated_id,
    _ACTION_ITEM_PREFIX,
    _POOL_PREFIX,
    _TASK_PREFIX,
)
from work_buddy.threads.enums import FSMState
from work_buddy.threads.models import Task, Thread


# ---------------------------------------------------------------------------
# State-mapping unit tests (pure function)
# ---------------------------------------------------------------------------


class TestTaskStateMapping:
    @pytest.mark.parametrize("v4_state,expected", [
        ("inbox", FSMState.PROPOSED),
        ("done", FSMState.DONE),
        ("focused", FSMState.AWAITING_CONFIRMATION),
        ("mit", FSMState.AWAITING_CONFIRMATION),
        ("snoozed", FSMState.AWAITING_REDIRECT),
    ])
    def test_known_states_map(self, v4_state, expected):
        row = {"state": v4_state}
        assert aggregator._task_state_to_fsm(row) == expected

    def test_archived_overrides_state_to_done(self):
        row = {"state": "inbox", "archived_at": "2026-01-01T00:00:00+00:00"}
        assert aggregator._task_state_to_fsm(row) == FSMState.DONE


class TestActionItemStateMapping:
    @pytest.mark.parametrize("state,authorship,expected", [
        ("done", "user", FSMState.DONE),
        ("skipped", "user", FSMState.DISMISSED),
        ("in_progress", "agent_approved", FSMState.EXECUTING),
        ("pending", "user", FSMState.AWAITING_CONFIRMATION),
        ("pending", "agent_approved", FSMState.AWAITING_CONFIRMATION),
        ("pending", "agent_unapproved", FSMState.AWAITING_CONFIRMATION),
    ])
    def test_known_states_map(self, state, authorship, expected):
        row = {"state": state, "authorship": authorship}
        assert aggregator._action_item_state_to_fsm(row) == expected


class TestPoolStateMapping:
    def _entry(self, **kwargs):
        # Lightweight stub mimicking ClarifyEntry's attribute access
        class E:
            def __init__(self, **kw):
                self.__dict__.update(kw)
        return E(**kwargs)

    def test_pending_maps_to_intent_confirmation(self):
        e = self._entry(state="pending")
        assert aggregator._pool_state_to_fsm(e) == FSMState.AWAITING_INTENT_CONFIRMATION

    def test_reviewed_approved_maps_to_done(self):
        e = self._entry(state="reviewed", review_outcome="approved")
        assert aggregator._pool_state_to_fsm(e) == FSMState.DONE

    def test_reviewed_rejected_maps_to_dismissed(self):
        e = self._entry(state="reviewed", review_outcome="rejected")
        assert aggregator._pool_state_to_fsm(e) == FSMState.DISMISSED

    def test_quarantined_maps_to_dismissed(self):
        e = self._entry(state="quarantined")
        assert aggregator._pool_state_to_fsm(e) == FSMState.DISMISSED

    def test_expired_maps_to_dismissed(self):
        e = self._entry(state="expired")
        assert aggregator._pool_state_to_fsm(e) == FSMState.DISMISSED


# ---------------------------------------------------------------------------
# ID prefix scheme
# ---------------------------------------------------------------------------


class TestAggregatedIDs:
    def test_recognises_task_prefix(self):
        assert is_aggregated_id("agg-task-t-abc")

    def test_recognises_action_item_prefix(self):
        assert is_aggregated_id("agg-ai-42")

    def test_recognises_pool_prefix(self):
        assert is_aggregated_id("agg-pool-run1:item1")

    def test_real_v5_thread_ids_not_aggregated(self):
        assert not is_aggregated_id("th-abc12345")
        assert not is_aggregated_id("regular-thread-id")


# ---------------------------------------------------------------------------
# End-to-end conversion (using real v4 stores in tmp dir)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_task_db(tmp_path, monkeypatch):
    """Isolate the v4 task DB to a tmp file."""
    from work_buddy.obsidian.tasks import store as task_store
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(task_store, "_db_path", lambda: db)
    yield db


class TestTaskAggregation:
    def test_empty_db_returns_empty_list(self, fresh_task_db):
        assert aggregator.list_aggregated_tasks() == []

    def test_single_task_round_trips(self, fresh_task_db):
        from work_buddy.obsidian.tasks import store
        store.create(task_id="t-aggtest1", description="hello world")

        threads = aggregator.list_aggregated_tasks()
        assert len(threads) == 1
        t = threads[0]
        assert isinstance(t, Task)
        assert t.thread_id == "agg-task-t-aggtest1"
        assert t.subtype == "task"
        assert t.fsm_state == FSMState.PROPOSED  # default state='inbox'
        assert t.inciting_event_summary["source"] == "v4_task_metadata"

    def test_done_task_maps_to_done_state(self, fresh_task_db):
        from work_buddy.obsidian.tasks import store
        store.create(task_id="t-done", state="done")
        threads = aggregator.list_aggregated_tasks()
        assert threads[0].fsm_state == FSMState.DONE

    def test_focused_task_maps_to_awaiting_confirmation(self, fresh_task_db):
        from work_buddy.obsidian.tasks import store
        store.create(task_id="t-foc", state="focused")
        threads = aggregator.list_aggregated_tasks()
        assert threads[0].fsm_state == FSMState.AWAITING_CONFIRMATION

    def test_get_thread_aggregated_resolves_task(self, fresh_task_db):
        from work_buddy.obsidian.tasks import store
        store.create(task_id="t-resolve")
        t = aggregator.get_thread_aggregated("agg-task-t-resolve")
        assert t is not None
        assert t.thread_id == "agg-task-t-resolve"

    def test_get_thread_aggregated_unknown_returns_none(self, fresh_task_db):
        assert aggregator.get_thread_aggregated("agg-task-nonexistent") is None


class TestActionItemAggregation:
    def test_action_items_become_subthreads(self, fresh_task_db):
        from work_buddy.obsidian.tasks import action_items, store
        store.create(task_id="t-with-ai")
        ai_a = action_items.create(
            task_id="t-with-ai", description="step 1", user_authored=True,
        )
        ai_b = action_items.create(
            task_id="t-with-ai", description="step 2", user_authored=False, approved_at="2026-05-02T00:00:00+00:00",
        )

        items = aggregator.list_aggregated_action_items(task_id="t-with-ai")
        assert len(items) == 2
        for sub in items:
            assert sub.thread_id.startswith(_ACTION_ITEM_PREFIX)
            assert sub.parent_id == "agg-task-t-with-ai"
            assert sub.subtype is None  # NOT a Task — plain sub-Thread
            assert sub.fsm_state == FSMState.AWAITING_CONFIRMATION

    def test_action_items_filter_by_aggregated_parent_id(self, fresh_task_db):
        from work_buddy.obsidian.tasks import action_items, store
        store.create(task_id="t-aggfilter")
        action_items.create(
            task_id="t-aggfilter", description="step", user_authored=True,
        )

        # Aggregator accepts the AGGREGATED id form
        items = aggregator.list_aggregated_action_items(
            task_id="agg-task-t-aggfilter",
        )
        assert len(items) == 1

    def test_done_action_item_maps_to_done(self, fresh_task_db):
        from work_buddy.obsidian.tasks import action_items, store
        store.create(task_id="t-done-ai")
        a = action_items.create(
            task_id="t-done-ai", description="x", user_authored=True,
        )
        action_items.update(a["id"], state="done")
        items = aggregator.list_aggregated_action_items(task_id="t-done-ai")
        assert items[0].fsm_state == FSMState.DONE

    def test_get_thread_aggregated_resolves_action_item(self, fresh_task_db):
        from work_buddy.obsidian.tasks import action_items, store
        store.create(task_id="t-getai")
        a = action_items.create(
            task_id="t-getai", description="x", user_authored=True,
        )
        thread = aggregator.get_thread_aggregated(
            f"{_ACTION_ITEM_PREFIX}{a['id']}",
        )
        assert thread is not None
        assert thread.parent_id == "agg-task-t-getai"

    def test_get_thread_aggregated_invalid_action_item_id(self, fresh_task_db):
        # Non-numeric suffix → silent None (logger warning, not exception)
        assert aggregator.get_thread_aggregated("agg-ai-not-a-number") is None


class TestUnifiedListing:
    def test_unified_returns_tasks_and_action_items(self, fresh_task_db):
        from work_buddy.obsidian.tasks import action_items, store
        store.create(task_id="t-uni")
        action_items.create(
            task_id="t-uni", description="step", user_authored=True,
        )
        all_threads = aggregator.list_threads_aggregated()
        thread_ids = {t.thread_id for t in all_threads}
        assert "agg-task-t-uni" in thread_ids
        assert any(
            tid.startswith(_ACTION_ITEM_PREFIX) for tid in thread_ids
        )

    def test_unified_filter_by_subtype_task(self, fresh_task_db):
        from work_buddy.obsidian.tasks import action_items, store
        store.create(task_id="t-fil")
        action_items.create(task_id="t-fil", description="step")

        only_tasks = aggregator.list_threads_aggregated(subtype="task")
        assert all(t.subtype == "task" for t in only_tasks)
        assert any(t.thread_id == "agg-task-t-fil" for t in only_tasks)

    def test_unified_filter_by_parent_id_returns_only_children(self, fresh_task_db):
        from work_buddy.obsidian.tasks import action_items, store
        store.create(task_id="t-parent")
        action_items.create(task_id="t-parent", description="A")
        action_items.create(task_id="t-parent", description="B")

        children = aggregator.list_threads_aggregated(
            parent_id="agg-task-t-parent",
        )
        assert all(t.parent_id == "agg-task-t-parent" for t in children)
        assert len(children) == 2

    def test_unified_filter_by_fsm_state(self, fresh_task_db):
        from work_buddy.obsidian.tasks import store
        store.create(task_id="t-a", state="focused")
        store.create(task_id="t-b", state="inbox")
        focused = aggregator.list_threads_aggregated(
            fsm_state=FSMState.AWAITING_CONFIRMATION,
        )
        ids = {t.thread_id for t in focused}
        assert "agg-task-t-a" in ids
        assert "agg-task-t-b" not in ids


class TestSummary:
    def test_summary_counts(self, fresh_task_db):
        from work_buddy.obsidian.tasks import action_items, store
        store.create(task_id="t-c1")
        store.create(task_id="t-c2")
        action_items.create(task_id="t-c1", description="step")
        s = aggregator.aggregator_summary()
        assert s["tasks"] == 2
        assert s["action_items"] == 1
        # pool_entries depends on whether ClarifyPool resolves; just
        # assert presence of the key.
        assert "pool_entries" in s
