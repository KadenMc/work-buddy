"""Tests for ``work_buddy.threads.group`` — the v2 group-relationship
pattern: umbrella thread → N group sub-threads, each holding its
items as ``context_items``.

Covers:

- :func:`group.group_thread` — spawn N children per cluster, items
  bucketed correctly, leftover → "Ungrouped".
- :func:`group.move_item` — move a ContextItem between siblings;
  paired events; cross-umbrella + missing-item rejections.
- :func:`group.cascade_approve_umbrella` — fan-out Accept; partial
  failures collected, not raised.
- :func:`group.delete_group_subthread` — manual DISMISS via the X
  button.
- :func:`group.spawn_empty_group` — drop-zone "+ New group" backing.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import group, models, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_CONTEXT_ITEM_MOVED,
    KIND_GROUPS_SPAWNED,
    KIND_GROUP_DELETED,
)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test threads DB. Mirrors the fixture style used in the
    decompose / grouping / autonomy tests."""
    threads_db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    yield


def _make_umbrella(*, conn=None):
    t = models.Thread(
        fsm_state=FSMState.PROPOSED,
        parent_relationship="decompose",  # set to 'group' by group_thread
    )
    store.insert_thread(t, conn=conn)
    return t


def _ctx(item_id: str, label: str = None) -> models.ContextItem:
    return models.ContextItem(
        id=item_id,
        source="chrome_tab",
        type="tab",
        label=label or f"Tab {item_id}",
        payload={"url": f"https://example.com/{item_id}"},
    )


# ---------------------------------------------------------------------------
# group_thread — spawn
# ---------------------------------------------------------------------------


class TestGroupThreadSpawn:
    def test_spawns_one_child_per_cluster(self, fresh_db):
        umbrella = _make_umbrella()
        items = [_ctx(f"i{i}") for i in range(5)]
        clusters = [
            {"label": "A", "item_ids": ["i0", "i1"]},
            {"label": "B", "item_ids": ["i2", "i3", "i4"]},
        ]
        child_ids = group.group_thread(
            umbrella.thread_id, items, clusters,
        )
        assert len(child_ids) == 2
        children = store.list_threads(parent_id=umbrella.thread_id)
        assert len(children) == 2
        # Children carry their cluster's items
        by_label = {
            c.inciting_event_summary.get("cluster_label"): c
            for c in children
        }
        assert {it.id for it in by_label["A"].context_items} == {"i0", "i1"}
        assert {it.id for it in by_label["B"].context_items} == {
            "i2", "i3", "i4",
        }

    def test_unassigned_items_become_ungrouped(self, fresh_db):
        umbrella = _make_umbrella()
        items = [_ctx(f"i{i}") for i in range(4)]
        clusters = [
            {"label": "A", "item_ids": ["i0", "i1"]},
        ]
        # i2, i3 are not in any cluster
        child_ids = group.group_thread(
            umbrella.thread_id, items, clusters,
        )
        assert len(child_ids) == 2  # A + Ungrouped
        children = store.list_threads(parent_id=umbrella.thread_id)
        ungrouped = [
            c for c in children
            if c.inciting_event_summary.get("cluster_label") == "Ungrouped"
        ]
        assert len(ungrouped) == 1
        assert {it.id for it in ungrouped[0].context_items} == {"i2", "i3"}

    def test_marks_parent_as_group_relationship(self, fresh_db):
        umbrella = _make_umbrella()
        items = [_ctx("i0")]
        clusters = [{"label": "A", "item_ids": ["i0"]}]
        group.group_thread(umbrella.thread_id, items, clusters)
        u_after = store.get_thread(umbrella.thread_id)
        assert u_after.parent_relationship == "group"
        assert u_after.fsm_state == FSMState.MONITORING

    def test_records_groups_spawned_event_on_umbrella(self, fresh_db):
        umbrella = _make_umbrella()
        items = [_ctx("i0"), _ctx("i1")]
        clusters = [
            {"label": "A", "item_ids": ["i0"]},
            {"label": "B", "item_ids": ["i1"]},
        ]
        child_ids = group.group_thread(
            umbrella.thread_id, items, clusters,
        )
        events = store.list_events(umbrella.thread_id)
        spawn_events = [e for e in events if e.kind == KIND_GROUPS_SPAWNED]
        assert len(spawn_events) == 1
        data = spawn_events[0].data
        assert sorted(data["child_thread_ids"]) == sorted(child_ids)
        assert data["cluster_count"] == 2
        assert data["source_count"] == 2
        assert sorted(data["child_labels"]) == ["A", "B"]

    def test_empty_source_items_raises(self, fresh_db):
        umbrella = _make_umbrella()
        with pytest.raises(group.GroupRefused, match="at least one source"):
            group.group_thread(umbrella.thread_id, [], [])

    def test_unknown_parent_raises(self, fresh_db):
        with pytest.raises(group.GroupRefused, match="not found"):
            group.group_thread(
                "th-doesnotexist", [_ctx("i0")],
                [{"label": "A", "item_ids": ["i0"]}],
            )

    def test_dict_items_accepted(self, fresh_db):
        # group_thread accepts dicts (will be inflated via from_dict)
        umbrella = _make_umbrella()
        items = [
            _ctx("i0").to_dict(),
            _ctx("i1").to_dict(),
        ]
        clusters = [{"label": "A", "item_ids": ["i0", "i1"]}]
        child_ids = group.group_thread(
            umbrella.thread_id, items, clusters,
        )
        assert len(child_ids) == 1


# ---------------------------------------------------------------------------
# move_item — happy path + validation
# ---------------------------------------------------------------------------


class TestMoveItem:
    def _setup_two_groups(self):
        umbrella = _make_umbrella()
        items = [_ctx("i0"), _ctx("i1"), _ctx("i2")]
        clusters = [
            {"label": "A", "item_ids": ["i0", "i1"]},
            {"label": "B", "item_ids": ["i2"]},
        ]
        child_ids = group.group_thread(
            umbrella.thread_id, items, clusters,
        )
        children = store.list_threads(parent_id=umbrella.thread_id)
        a = next(
            c for c in children
            if c.inciting_event_summary.get("cluster_label") == "A"
        )
        b = next(
            c for c in children
            if c.inciting_event_summary.get("cluster_label") == "B"
        )
        return umbrella, a, b

    def test_move_rewrites_both_threads(self, fresh_db):
        _, a, b = self._setup_two_groups()
        result = group.move_item("i0", a.thread_id, b.thread_id)
        assert result["item"]["id"] == "i0"
        a_after = store.get_thread(a.thread_id)
        b_after = store.get_thread(b.thread_id)
        assert {it.id for it in a_after.context_items} == {"i1"}
        assert {it.id for it in b_after.context_items} == {"i2", "i0"}

    def test_move_emits_paired_events(self, fresh_db):
        _, a, b = self._setup_two_groups()
        result = group.move_item("i0", a.thread_id, b.thread_id)
        a_events = store.list_events(a.thread_id)
        b_events = store.list_events(b.thread_id)
        a_moves = [
            e for e in a_events if e.kind == KIND_CONTEXT_ITEM_MOVED
        ]
        b_moves = [
            e for e in b_events if e.kind == KIND_CONTEXT_ITEM_MOVED
        ]
        assert len(a_moves) == 1
        assert len(b_moves) == 1
        assert a_moves[0].migration_id == result["migration_id"]
        assert b_moves[0].migration_id == result["migration_id"]
        assert a_moves[0].data["direction"] == "out"
        assert b_moves[0].data["direction"] == "in"
        assert a_moves[0].data["item_id"] == "i0"

    def test_move_bumps_parent_event_id_on_both(self, fresh_db):
        # Regression for the optimistic-lock bug fixed in 8748237b.
        _, a, b = self._setup_two_groups()
        a_before = store.get_thread(a.thread_id).parent_event_id
        b_before = store.get_thread(b.thread_id).parent_event_id
        group.move_item("i0", a.thread_id, b.thread_id)
        a_after = store.get_thread(a.thread_id).parent_event_id
        b_after = store.get_thread(b.thread_id).parent_event_id
        assert a_after > (a_before or 0)
        assert b_after > (b_before or 0)

    def test_move_rejects_self_move(self, fresh_db):
        _, a, _ = self._setup_two_groups()
        with pytest.raises(group.GroupRefused, match="same thread"):
            group.move_item("i0", a.thread_id, a.thread_id)

    def test_move_rejects_missing_item(self, fresh_db):
        _, a, b = self._setup_two_groups()
        with pytest.raises(group.GroupRefused, match="not present"):
            group.move_item("i999", a.thread_id, b.thread_id)

    def test_move_rejects_cross_umbrella(self, fresh_db):
        # Two separate umbrellas, each with its own children.
        u1 = _make_umbrella()
        u2 = _make_umbrella()
        group.group_thread(
            u1.thread_id, [_ctx("a0")],
            [{"label": "A", "item_ids": ["a0"]}],
        )
        group.group_thread(
            u2.thread_id, [_ctx("b0")],
            [{"label": "B", "item_ids": ["b0"]}],
        )
        a = store.list_threads(parent_id=u1.thread_id)[0]
        b = store.list_threads(parent_id=u2.thread_id)[0]
        with pytest.raises(group.GroupRefused, match="cross-umbrella"):
            group.move_item("a0", a.thread_id, b.thread_id)


# ---------------------------------------------------------------------------
# cascade_approve_umbrella — partial failures
# ---------------------------------------------------------------------------


class TestCascadeApproveUmbrella:
    def test_skips_terminal_children(self, fresh_db):
        umbrella = _make_umbrella()
        group.group_thread(
            umbrella.thread_id, [_ctx("i0"), _ctx("i1")],
            [
                {"label": "A", "item_ids": ["i0"]},
                {"label": "B", "item_ids": ["i1"]},
            ],
        )
        # Force one child to terminal state.
        children = store.list_threads(parent_id=umbrella.thread_id)
        store.update_thread_state(
            children[0].thread_id, fsm_state=FSMState.DONE.value,
        )
        result = group.cascade_approve_umbrella(umbrella.thread_id)
        assert children[0].thread_id in result["skipped_terminal"]

    def test_collects_failures_continues(self, fresh_db):
        umbrella = _make_umbrella()
        group.group_thread(
            umbrella.thread_id, [_ctx("i0"), _ctx("i1")],
            [
                {"label": "A", "item_ids": ["i0"]},
                {"label": "B", "item_ids": ["i1"]},
            ],
        )
        # Children freshly spawned land in PROPOSED — no Accept-equivalent
        # trigger from there, so cascade_approve will record them as
        # failed rather than raising.
        result = group.cascade_approve_umbrella(umbrella.thread_id)
        assert result["approved"] == []
        assert len(result["failed"]) == 2
        assert all("error" in f for f in result["failed"])

    def test_rejects_non_group_umbrella(self, fresh_db):
        # A thread with parent_relationship == 'decompose' is not a
        # valid umbrella for cascade_approve.
        t = models.Thread(parent_relationship="decompose")
        store.insert_thread(t)
        with pytest.raises(group.GroupRefused, match="not a group"):
            group.cascade_approve_umbrella(t.thread_id)


# ---------------------------------------------------------------------------
# delete_group_subthread — X-button delete
# ---------------------------------------------------------------------------


class TestDeleteGroupSubthread:
    def test_dismisses_child_records_audit(self, fresh_db):
        umbrella = _make_umbrella()
        group.group_thread(
            umbrella.thread_id, [_ctx("i0")],
            [{"label": "A", "item_ids": ["i0"]}],
        )
        child = store.list_threads(parent_id=umbrella.thread_id)[0]
        result = group.delete_group_subthread(child.thread_id)
        assert result["dismissed"] == child.thread_id
        # Child is now terminal
        c_after = store.get_thread(child.thread_id)
        assert c_after.fsm_state == FSMState.DISMISSED
        # Umbrella has audit event
        events = store.list_events(umbrella.thread_id)
        deletes = [e for e in events if e.kind == KIND_GROUP_DELETED]
        assert len(deletes) == 1
        assert deletes[0].data["deleted_child_id"] == child.thread_id

    def test_rejects_non_group_child(self, fresh_db):
        # Thread with no parent → can't delete via this op.
        t = models.Thread()
        store.insert_thread(t)
        with pytest.raises(group.GroupRefused, match="no parent"):
            group.delete_group_subthread(t.thread_id)


# ---------------------------------------------------------------------------
# spawn_empty_group — "+ New group" drop zone
# ---------------------------------------------------------------------------


class TestSpawnEmptyGroup:
    def test_spawns_empty_child_under_umbrella(self, fresh_db):
        umbrella = _make_umbrella()
        group.group_thread(
            umbrella.thread_id, [_ctx("i0")],
            [{"label": "A", "item_ids": ["i0"]}],
        )
        new_id = group.spawn_empty_group(umbrella.thread_id, "Reading list")
        new_child = store.get_thread(new_id)
        assert new_child.parent_id == umbrella.thread_id
        assert new_child.context_items == ()
        assert (
            new_child.inciting_event_summary["cluster_label"]
            == "Reading list"
        )
        assert new_child.inciting_event_summary["user_created"] is True

    def test_blank_label_falls_back(self, fresh_db):
        umbrella = _make_umbrella()
        group.group_thread(
            umbrella.thread_id, [_ctx("i0")],
            [{"label": "A", "item_ids": ["i0"]}],
        )
        new_id = group.spawn_empty_group(umbrella.thread_id, "   ")
        new_child = store.get_thread(new_id)
        assert (
            new_child.inciting_event_summary["cluster_label"] == "New group"
        )

    def test_rejects_non_group_umbrella(self, fresh_db):
        t = models.Thread(parent_relationship="decompose")
        store.insert_thread(t)
        with pytest.raises(group.GroupRefused, match="not a group"):
            group.spawn_empty_group(t.thread_id, "X")
