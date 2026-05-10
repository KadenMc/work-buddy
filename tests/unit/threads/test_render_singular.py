"""Tests for the singular-pattern hoist in `build_render_data`.

When an umbrella Thread has ``parent_relationship='singular'`` (set by
``pipelines/inline.py:_spawn_inline_umbrella`` for inline captures whose
verdict produced 2+ records), the dashboard render hoists each child
Thread's actions onto the umbrella's `actions` array — so the user sees
ONE thread with N actions instead of an empty umbrella + N detached
sub-thread cards.

Each hoisted action carries:
- ``host_thread_id``: the child's thread_id (used to route per-action
  Approve/Reject POSTs to the right child endpoint).
- ``state``: derived from the child's ``fsm_state`` via
  :func:`render._per_action_state_from_fsm`.
- ``settled``: bool — true when state is ``done | rejected | failed``.

Order: pending first (in child-spawn order), settled last. Stable.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import cleanup, render, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import KIND_ACTION_INFERRED, ThreadEvent
from work_buddy.threads.models import ContextItem, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    cleanup.clear_cleanup_adapters()
    yield db
    cleanup.clear_cleanup_adapters()


def _make_singular_umbrella(title: str = "Inline selection: Sarah's birthday") -> Thread:
    """Create + insert an umbrella with parent_relationship='singular'."""
    umbrella = Thread(
        fsm_state=FSMState.MONITORING,
        parent_relationship="singular",
        inciting_event_summary={
            "source": "inline_capture",
            "title": title,
            "description": "Sarah's birthday gift task + calendar event",
        },
        context_items=(
            ContextItem(
                id="inline_abc",
                source="inline_selection",
                type="selection",
                label="Buy gift for Sarah's birthday May 12",
                payload={"text": "Buy gift for Sarah's birthday May 12"},
            ),
        ),
    )
    store.insert_thread(umbrella)
    return umbrella


def _make_child_with_action(
    parent_id: str,
    action_name: str,
    fsm_state: FSMState = FSMState.AWAITING_CONFIRMATION,
    order_index: int = 0,
) -> Thread:
    """Create + insert a child Thread carrying one `action_inferred` event."""
    child = Thread(
        parent_id=parent_id,
        fsm_state=fsm_state,
        order_index=order_index,
        inciting_event_summary={
            "source": "inline_capture",
            "title": action_name,
            "description": f"Action: {action_name}",
        },
    )
    store.insert_thread(child)
    store.append_event(ThreadEvent(
        thread_id=child.thread_id,
        kind=KIND_ACTION_INFERRED,
        actor="inciting",
        data={
            "payload": {
                "kind": "standard",
                "name": action_name,
                "parameters": {"foo": "bar"},
                "rationale": f"Test rationale for {action_name}",
            },
            "confidence": 0.8,
            "model_used": "claude-sonnet-test",
        },
    ))
    return child


# ---------------------------------------------------------------------------
# Hoist behavior
# ---------------------------------------------------------------------------


class TestSingularHoist:

    def test_zero_children_actions_empty(self, fresh_db):
        """Singular umbrella with no children renders empty actions
        (no error)."""
        umbrella = _make_singular_umbrella()
        data = render.build_render_data(umbrella.thread_id)
        assert data["parent_relationship"] == "singular"
        assert data["actions"] == []

    def test_one_child_action_hoisted(self, fresh_db):
        """One child with one action_inferred → one entry in parent's
        `actions` array, marked with host_thread_id and pending state."""
        umbrella = _make_singular_umbrella()
        child = _make_child_with_action(
            parent_id=umbrella.thread_id,
            action_name="task_create",
            fsm_state=FSMState.AWAITING_CONFIRMATION,
        )

        data = render.build_render_data(umbrella.thread_id)

        assert len(data["actions"]) == 1
        a = data["actions"][0]
        assert a["host_thread_id"] == child.thread_id
        assert a["state"] == "pending"
        assert a["settled"] is False
        assert a["name"] == "task_create"

    def test_two_children_hoisted_in_spawn_order(self, fresh_db):
        """Two children spawn in order_index order; hoisted actions
        keep that order when both are pending."""
        umbrella = _make_singular_umbrella()
        child_a = _make_child_with_action(
            parent_id=umbrella.thread_id,
            action_name="task_create",
            order_index=0,
        )
        child_b = _make_child_with_action(
            parent_id=umbrella.thread_id,
            action_name="calendar_event_suggested",
            order_index=1,
        )

        data = render.build_render_data(umbrella.thread_id)

        assert len(data["actions"]) == 2
        assert data["actions"][0]["host_thread_id"] == child_a.thread_id
        assert data["actions"][1]["host_thread_id"] == child_b.thread_id
        assert data["actions"][0]["name"] == "task_create"
        assert data["actions"][1]["name"] == "calendar_event_suggested"
        assert all(a["state"] == "pending" for a in data["actions"])

    def test_pending_first_settled_last(self, fresh_db):
        """Mixed states: pending first (in spawn order), settled last."""
        umbrella = _make_singular_umbrella()
        # First spawned, but already done.
        child_done = _make_child_with_action(
            parent_id=umbrella.thread_id,
            action_name="task_create",
            fsm_state=FSMState.DONE,
            order_index=0,
        )
        # Second spawned, still pending.
        child_pending = _make_child_with_action(
            parent_id=umbrella.thread_id,
            action_name="calendar_event_suggested",
            fsm_state=FSMState.AWAITING_CONFIRMATION,
            order_index=1,
        )

        data = render.build_render_data(umbrella.thread_id)

        assert len(data["actions"]) == 2
        # Pending action shows first (despite being spawned second).
        assert data["actions"][0]["host_thread_id"] == child_pending.thread_id
        assert data["actions"][0]["state"] == "pending"
        assert data["actions"][0]["settled"] is False
        # Settled action last.
        assert data["actions"][1]["host_thread_id"] == child_done.thread_id
        assert data["actions"][1]["state"] == "done"
        assert data["actions"][1]["settled"] is True

    def test_dismissed_child_state_rejected(self, fresh_db):
        """A DISMISSED child surfaces as `rejected` state on the parent's
        hoisted action; settled=True."""
        umbrella = _make_singular_umbrella()
        _make_child_with_action(
            parent_id=umbrella.thread_id,
            action_name="task_create",
            fsm_state=FSMState.DISMISSED,
        )

        data = render.build_render_data(umbrella.thread_id)
        assert len(data["actions"]) == 1
        assert data["actions"][0]["state"] == "rejected"
        assert data["actions"][0]["settled"] is True

    def test_executing_child_state_executing(self, fresh_db):
        umbrella = _make_singular_umbrella()
        _make_child_with_action(
            parent_id=umbrella.thread_id,
            action_name="task_create",
            fsm_state=FSMState.EXECUTING,
        )
        data = render.build_render_data(umbrella.thread_id)
        assert data["actions"][0]["state"] == "executing"
        assert data["actions"][0]["settled"] is False  # still in flight


# ---------------------------------------------------------------------------
# Non-singular umbrellas keep existing behavior
# ---------------------------------------------------------------------------


class TestNonSingularUnaffected:

    def test_decompose_parent_not_hoisted(self, fresh_db):
        """A `parent_relationship='decompose'` parent (the default) does
        NOT hoist children's actions — preserves today's behaviour
        for decompose/group umbrellas."""
        parent = Thread(
            fsm_state=FSMState.MONITORING,
            parent_relationship="decompose",
            inciting_event_summary={"description": "decompose parent"},
        )
        store.insert_thread(parent)
        _make_child_with_action(parent_id=parent.thread_id, action_name="x")

        data = render.build_render_data(parent.thread_id)
        # No own action_inferred on parent → actions stays empty (no hoist).
        assert data["actions"] == []
        # Sub-thread count IS reflected (so the standard render path
        # would show "Sub-threads (1)" — the existing behaviour).
        assert data["sub_thread_count"] == 1

    def test_group_parent_not_hoisted(self, fresh_db):
        """A `parent_relationship='group'` umbrella (chrome/journal cluster)
        does NOT hoist either — group has its own multi-column drag-drop
        view that surfaces children's actions per-cluster."""
        umbrella = Thread(
            fsm_state=FSMState.MONITORING,
            parent_relationship="group",
            inciting_event_summary={"description": "Daily note: 2026-05-09"},
        )
        store.insert_thread(umbrella)
        _make_child_with_action(parent_id=umbrella.thread_id, action_name="route")

        data = render.build_render_data(umbrella.thread_id)
        assert data["actions"] == []
        assert data["parent_relationship"] == "group"


# ---------------------------------------------------------------------------
# Per-action state derivation helper
# ---------------------------------------------------------------------------


class TestPerActionStateFromFsm:

    @pytest.mark.parametrize("state,expected", [
        ("awaiting_confirmation", "pending"),
        ("AWAITING_CONFIRMATION", "pending"),  # case-insensitive
        ("executing", "executing"),
        ("done", "done"),
        ("dismissed", "rejected"),
        ("inferring_intent", "pending"),
        ("awaiting_intent_clarification", "pending"),
        ("", "pending"),
    ])
    def test_state_mapping(self, state, expected):
        from work_buddy.threads.render import _per_action_state_from_fsm
        assert _per_action_state_from_fsm(state) == expected
