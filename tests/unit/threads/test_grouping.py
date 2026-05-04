"""Tests for ``work_buddy.threads.grouping`` — Stage 5 group-relationship
operations: move + cascade auto-DISMISS + bulk submit + sibling list.

The schema migration + Thread.parent_relationship round-trip are
exercised here too via the full module integration; finer-grained
schema tests live in ``test_store_schema.py``.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import grouping, models, store
from work_buddy.threads.enums import FSMState


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test threads DB. Mirrors the fixture style used in the
    decompose / autonomy tests."""
    threads_db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    yield


def _make_group_parent(scope: str = "scrape-X", *, conn=None):
    t = models.Thread(
        fsm_state=FSMState.MONITORING,
        parent_relationship="group",
        originating_scrape_id=scope,
    )
    store.insert_thread(t, conn=conn)
    return t


def _make_decompose_parent(*, conn=None):
    t = models.Thread(
        fsm_state=FSMState.MONITORING,
        parent_relationship="decompose",
    )
    store.insert_thread(t, conn=conn)
    return t


def _make_child(parent, state: FSMState = FSMState.AWAITING_CONFIRMATION, *, conn=None):
    c = models.Thread(parent_id=parent.thread_id, fsm_state=state)
    store.insert_thread(c, conn=conn)
    return c


# ---------------------------------------------------------------------------
# move_thread_to_parent — happy path
# ---------------------------------------------------------------------------


class TestMoveHappyPath:
    def test_basic_move_rewrites_parent_id(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-A")
        c = _make_child(g1)
        result = grouping.move_thread_to_parent(c.thread_id, g2.thread_id)
        assert result["from_parent"] == g1.thread_id
        assert result["to_parent"] == g2.thread_id
        assert result["migration_id"]
        moved = store.get_thread(c.thread_id)
        assert moved.parent_id == g2.thread_id

    def test_move_emits_paired_events(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-A")
        c = _make_child(g1)
        result = grouping.move_thread_to_parent(c.thread_id, g2.thread_id)
        # Both old and new parent should carry an item_moved event with
        # the same migration_id.
        old_events = store.list_events(g1.thread_id)
        new_events = store.list_events(g2.thread_id)
        old_moves = [e for e in old_events if e.kind == "item_moved"]
        new_moves = [e for e in new_events if e.kind == "item_moved"]
        assert len(old_moves) == 1
        assert len(new_moves) == 1
        assert old_moves[0].migration_id == result["migration_id"]
        assert new_moves[0].migration_id == result["migration_id"]
        assert old_moves[0].data["direction"] == "outgoing"
        assert new_moves[0].data["direction"] == "incoming"
        assert old_moves[0].data["item_id"] == c.thread_id

    def test_last_item_out_auto_dismisses_old_parent(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-A")
        c = _make_child(g1)
        result = grouping.move_thread_to_parent(c.thread_id, g2.thread_id)
        assert result["old_parent_dismissed"] is True
        assert result["old_parent_state"] == "dismissed"
        g1_after = store.get_thread(g1.thread_id)
        assert g1_after.fsm_state == FSMState.DISMISSED

    def test_remaining_children_block_auto_dismiss(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-A")
        c1 = _make_child(g1)
        c2 = _make_child(g1)  # second child stays put
        result = grouping.move_thread_to_parent(c1.thread_id, g2.thread_id)
        assert result["old_parent_dismissed"] is False
        assert result["old_parent_state"] is None
        g1_after = store.get_thread(g1.thread_id)
        assert g1_after.fsm_state == FSMState.MONITORING


# ---------------------------------------------------------------------------
# move_thread_to_parent — validation
# ---------------------------------------------------------------------------


class TestMoveValidation:
    def test_destination_not_found(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        c = _make_child(g1)
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.move_thread_to_parent(c.thread_id, "nonexistent")
        assert exc.value.reason == "destination_not_found"

    def test_destination_not_group(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        d = _make_decompose_parent()
        c = _make_child(g1)
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.move_thread_to_parent(c.thread_id, d.thread_id)
        assert exc.value.reason == "destination_not_group"

    def test_source_not_group(self, fresh_db):
        d = _make_decompose_parent()
        g = _make_group_parent("scrape-A")
        c = _make_child(d)
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.move_thread_to_parent(c.thread_id, g.thread_id)
        assert exc.value.reason == "source_not_group"

    def test_scope_mismatch(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-B")  # different scope
        c = _make_child(g1)
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.move_thread_to_parent(c.thread_id, g2.thread_id)
        assert exc.value.reason == "scope_mismatch"

    def test_missing_scope(self, fresh_db):
        # Two group parents but neither has originating_scrape_id —
        # they aren't siblings of anything.
        g1 = models.Thread(
            fsm_state=FSMState.MONITORING,
            parent_relationship="group",
            originating_scrape_id=None,
        )
        g2 = models.Thread(
            fsm_state=FSMState.MONITORING,
            parent_relationship="group",
            originating_scrape_id=None,
        )
        store.insert_thread(g1)
        store.insert_thread(g2)
        c = _make_child(g1)
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.move_thread_to_parent(c.thread_id, g2.thread_id)
        assert exc.value.reason == "missing_scrape_scope"

    def test_same_parent_rejected(self, fresh_db):
        g = _make_group_parent("scrape-A")
        c = _make_child(g)
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.move_thread_to_parent(c.thread_id, g.thread_id)
        assert exc.value.reason == "same_parent"

    def test_orphan_item_rejected(self, fresh_db):
        # Item with no parent at all.
        g = _make_group_parent("scrape-A")
        orphan = models.Thread(fsm_state=FSMState.AWAITING_CONFIRMATION)
        store.insert_thread(orphan)
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.move_thread_to_parent(orphan.thread_id, g.thread_id)
        assert exc.value.reason == "source_orphan"

    def test_item_not_found(self, fresh_db):
        g = _make_group_parent("scrape-A")
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.move_thread_to_parent("nonexistent", g.thread_id)
        assert exc.value.reason == "item_not_found"


# ---------------------------------------------------------------------------
# list_sibling_group_parents
# ---------------------------------------------------------------------------


class TestListSiblings:
    def test_returns_all_with_same_scope(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-A")
        g3 = _make_group_parent("scrape-A")
        # Decompose-parent in the same DB shouldn't appear.
        _make_decompose_parent()
        # Group with different scope shouldn't appear.
        _make_group_parent("scrape-B")
        sibs = grouping.list_sibling_group_parents(g2.thread_id)
        sib_ids = {s.thread_id for s in sibs}
        assert sib_ids == {g1.thread_id, g2.thread_id, g3.thread_id}

    def test_excludes_self_when_requested(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-A")
        sibs = grouping.list_sibling_group_parents(
            g1.thread_id, include_self=False,
        )
        ids = {s.thread_id for s in sibs}
        assert ids == {g2.thread_id}

    def test_decompose_parent_returns_empty(self, fresh_db):
        d = _make_decompose_parent()
        assert grouping.list_sibling_group_parents(d.thread_id) == []

    def test_unknown_id_returns_empty(self, fresh_db):
        assert grouping.list_sibling_group_parents("nope") == []


# ---------------------------------------------------------------------------
# bulk_submit_group
# ---------------------------------------------------------------------------


class TestBulkSubmit:
    def test_skips_non_awaiting_children(self, fresh_db):
        g = _make_group_parent("scrape-A")
        # Mix of states.
        _make_child(g, FSMState.AWAITING_CONFIRMATION)
        _make_child(g, FSMState.PROPOSED)  # skip
        _make_child(g, FSMState.AWAITING_CONFIRMATION)
        # Bootstrap doesn't run in tests, so no engine-registered
        # transition handlers for the accept call. Skip the actual
        # transition by stubbing engine.transition before exercising.
        from unittest.mock import patch
        with patch("work_buddy.threads.engine.transition") as t:
            t.return_value = type("R", (), {"next_state": FSMState.EXECUTING})()
            result = grouping.bulk_submit_group(g.thread_id)
        assert result["submitted"] == 2
        assert result["skipped"] == 1
        assert result["failed"] == 0

    def test_rejects_non_group_parent(self, fresh_db):
        d = _make_decompose_parent()
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.bulk_submit_group(d.thread_id)
        assert exc.value.reason == "parent_not_group"

    def test_rejects_unknown_parent(self, fresh_db):
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.bulk_submit_group("nope")
        assert exc.value.reason == "parent_not_found"

    def test_per_item_failure_does_not_block_batch(self, fresh_db):
        g = _make_group_parent("scrape-A")
        _make_child(g, FSMState.AWAITING_CONFIRMATION)
        _make_child(g, FSMState.AWAITING_CONFIRMATION)
        _make_child(g, FSMState.AWAITING_CONFIRMATION)
        from unittest.mock import patch
        # Make the 2nd call raise.
        calls = {"n": 0}

        def fake(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return type("R", (), {"next_state": FSMState.EXECUTING})()

        with patch("work_buddy.threads.engine.transition", side_effect=fake):
            result = grouping.bulk_submit_group(g.thread_id)
        assert result["submitted"] == 2
        assert result["failed"] == 1
        # Per-item results carry the error string for the failed one.
        errors = [r for r in result["results"] if r.get("error")]
        assert len(errors) == 1
        assert "boom" in errors[0]["error"]


# ---------------------------------------------------------------------------
# Cascade — exercised via grouping module since the trigger is an
# external move op, not a terminal-state entry.
# ---------------------------------------------------------------------------


class TestCascadeAfterItemMoved:
    def test_only_fires_for_group_parents(self, fresh_db):
        from work_buddy.threads.decompose import cascade_after_item_moved
        d = _make_decompose_parent()
        # Even with zero children, decompose-parents shouldn't auto-DISMISS.
        result = cascade_after_item_moved(d.thread_id)
        assert result is None
        d_after = store.get_thread(d.thread_id)
        assert d_after.fsm_state == FSMState.MONITORING

    def test_skips_when_children_remain(self, fresh_db):
        from work_buddy.threads.decompose import cascade_after_item_moved
        g = _make_group_parent("scrape-A")
        _make_child(g)
        result = cascade_after_item_moved(g.thread_id)
        assert result is None
        g_after = store.get_thread(g.thread_id)
        assert g_after.fsm_state == FSMState.MONITORING

    def test_dismisses_empty_group(self, fresh_db):
        from work_buddy.threads.decompose import cascade_after_item_moved
        g = _make_group_parent("scrape-A")
        result = cascade_after_item_moved(g.thread_id)
        assert result == "dismissed"
        g_after = store.get_thread(g.thread_id)
        assert g_after.fsm_state == FSMState.DISMISSED


# ---------------------------------------------------------------------------
# spawn_sibling_group
# ---------------------------------------------------------------------------


class TestSpawnSiblingGroup:
    def test_creates_sibling_with_inherited_scope(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        result = grouping.spawn_sibling_group(g1.thread_id, label="My new group")
        assert result["originating_scrape_id"] == "scrape-A"
        assert result["label"] == "My new group"
        new_parent = store.get_thread(result["parent_id"])
        assert new_parent.parent_relationship == "group"
        assert new_parent.originating_scrape_id == "scrape-A"
        assert new_parent.fsm_state == FSMState.MONITORING

    def test_appears_in_sibling_list(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        result = grouping.spawn_sibling_group(g1.thread_id)
        sibs = grouping.list_sibling_group_parents(g1.thread_id)
        ids = {s.thread_id for s in sibs}
        assert g1.thread_id in ids
        assert result["parent_id"] in ids

    def test_can_move_into_new_sibling(self, fresh_db):
        g1 = _make_group_parent("scrape-A")
        c = _make_child(g1)
        # Keep g1 alive after the move so we don't auto-DISMISS it.
        _make_child(g1)
        result = grouping.spawn_sibling_group(g1.thread_id)
        mv = grouping.move_thread_to_parent(c.thread_id, result["parent_id"])
        assert mv["to_parent"] == result["parent_id"]

    def test_rejects_non_group_reference(self, fresh_db):
        d = _make_decompose_parent()
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.spawn_sibling_group(d.thread_id)
        assert exc.value.reason == "reference_not_group"

    def test_rejects_reference_without_scope(self, fresh_db):
        # A group-parent with no originating_scrape_id can't have siblings.
        g = models.Thread(
            fsm_state=FSMState.MONITORING,
            parent_relationship="group",
            originating_scrape_id=None,
        )
        store.insert_thread(g)
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.spawn_sibling_group(g.thread_id)
        assert exc.value.reason == "reference_missing_scope"

    def test_unknown_reference_rejected(self, fresh_db):
        with pytest.raises(grouping.MoveValidationError) as exc:
            grouping.spawn_sibling_group("nope")
        assert exc.value.reason == "reference_not_group"


# ---------------------------------------------------------------------------
# suggest_cross_group_merges
# ---------------------------------------------------------------------------


class TestCrossGroupSuggestions:
    def test_returns_empty_for_decompose_parent(self, fresh_db):
        d = _make_decompose_parent()
        result = grouping.suggest_cross_group_merges(d.thread_id)
        assert result["suggestions"] == []
        assert result["embed_status"] == "skipped"

    def test_returns_empty_when_scope_missing(self, fresh_db):
        g = models.Thread(
            fsm_state=FSMState.MONITORING,
            parent_relationship="group",
            originating_scrape_id=None,
        )
        store.insert_thread(g)
        result = grouping.suggest_cross_group_merges(g.thread_id)
        assert result["suggestions"] == []

    def test_returns_empty_when_fewer_than_two_items(self, fresh_db):
        g = _make_group_parent("scrape-A")
        result = grouping.suggest_cross_group_merges(g.thread_id)
        assert result["suggestions"] == []

    def test_skips_within_group_pairs(self, fresh_db):
        # When two items are in the SAME group, no suggestion should
        # surface — within-group similarity is conveyed by display
        # ordering, not by a "move" suggestion.
        g1 = _make_group_parent("scrape-A")
        c1 = _make_child(g1)
        c2 = _make_child(g1)
        # Same-group; no cross-group suggestions possible.
        result = grouping.suggest_cross_group_merges(g1.thread_id)
        for s in result.get("suggestions", []):
            assert s["from_parent"] != s["to_parent"]
