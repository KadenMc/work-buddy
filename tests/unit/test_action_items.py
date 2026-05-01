"""Slice 7: ``task_action_items`` table + CRUD + safety rule.

1. Migration creates the table on fresh AND existing DBs.
2. Create / get / list / update / delete round-trip.
3. ``approve`` sets ``authorship='agent_approved'``.
4. ``set_current`` updates ``task_metadata.current_action_item_id``.
5. ``is_executable`` enforces the authorship enum check (in
   ``{'user', 'agent_approved'}``) AND the terminal-state exclusion.
6. ``position_in_task`` returns the 1-based step index.
"""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks import action_items, store


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_table_exists_after_first_open(fresh_db):
    conn = store.get_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
        names = {r["name"] for r in rows}
    finally:
        conn.close()
    assert "task_action_items" in names


def test_current_action_item_id_column_present(fresh_db):
    conn = store.get_connection()
    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_metadata)")
        }
    finally:
        conn.close()
    assert "current_action_item_id" in cols


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


def test_create_assigns_next_sequence(fresh_db):
    store.create(task_id="t-ai-1")
    a = action_items.create(task_id="t-ai-1", description="Step A")
    b = action_items.create(task_id="t-ai-1", description="Step B")
    assert a["sequence"] == 1
    assert b["sequence"] == 2


def test_create_explicit_sequence(fresh_db):
    store.create(task_id="t-ai-2")
    out = action_items.create(
        task_id="t-ai-2", description="step", sequence=5,
    )
    assert out["sequence"] == 5


def test_get_returns_inserted_row(fresh_db):
    store.create(task_id="t-ai-3")
    created = action_items.create(
        task_id="t-ai-3",
        description="edit code",
        agent_required_contexts='["@filesystem"]',
        authorship="user",
    )
    row = action_items.get(created["id"])
    assert row is not None
    assert row["description"] == "edit code"
    assert row["agent_required_contexts"] == '["@filesystem"]'
    assert row["authorship"] == "user"


def test_list_for_task_orders_by_sequence(fresh_db):
    store.create(task_id="t-ai-4")
    action_items.create(task_id="t-ai-4", description="b", sequence=2)
    action_items.create(task_id="t-ai-4", description="a", sequence=1)
    items = action_items.list_for_task("t-ai-4")
    assert [i["description"] for i in items] == ["a", "b"]


def test_list_for_task_can_skip_done(fresh_db):
    store.create(task_id="t-ai-5")
    a = action_items.create(task_id="t-ai-5", description="a")
    b = action_items.create(task_id="t-ai-5", description="b")
    action_items.update(a["id"], state="done")
    items = action_items.list_for_task("t-ai-5", include_done=False)
    assert [i["description"] for i in items] == ["b"]


def test_update_state_done_stamps_completed_at(fresh_db):
    store.create(task_id="t-ai-6")
    a = action_items.create(task_id="t-ai-6", description="x")
    action_items.update(a["id"], state="done")
    row = action_items.get(a["id"])
    assert row["state"] == "done"
    assert row["completed_at"] is not None


def test_update_invalid_state_raises(fresh_db):
    store.create(task_id="t-ai-7")
    a = action_items.create(task_id="t-ai-7", description="x")
    with pytest.raises(ValueError):
        action_items.update(a["id"], state="garbage")


def test_delete_returns_true_then_false(fresh_db):
    store.create(task_id="t-ai-8")
    a = action_items.create(task_id="t-ai-8", description="x")
    assert action_items.delete(a["id"]) is True
    assert action_items.delete(a["id"]) is False


def test_create_invalid_state_rejected(fresh_db):
    store.create(task_id="t-ai-9")
    with pytest.raises(ValueError):
        action_items.create(task_id="t-ai-9", description="x", state="bogus")


# ---------------------------------------------------------------------------
# approve + set_current
# ---------------------------------------------------------------------------


def test_approve_sets_authorship_to_agent_approved(fresh_db):
    """PR #70 fix #2: approve() sets authorship='agent_approved'
    to preserve agent-origin provenance (vs flipping to 'user' which
    would lose origin).
    """
    store.create(task_id="t-ai-10")
    a = action_items.create(
        task_id="t-ai-10", description="agent proposed",
        authorship="agent_unapproved",
    )
    action_items.approve(a["id"])
    row = action_items.get(a["id"])
    assert row["authorship"] == "agent_approved"
    # is_executable admits this row.
    assert action_items.is_executable(row) is True


def test_set_current_updates_task_metadata(fresh_db):
    store.create(task_id="t-ai-11")
    a = action_items.create(task_id="t-ai-11", description="step")
    action_items.set_current("t-ai-11", a["id"])
    row = store.get("t-ai-11")
    assert row["current_action_item_id"] == a["id"]
    # Clearing
    action_items.set_current("t-ai-11", None)
    row = store.get("t-ai-11")
    assert row["current_action_item_id"] is None


# ---------------------------------------------------------------------------
# is_executable safety rule -- canonical authorship enum reading.
# Comprehensive enum + legacy-fallback coverage lives in test_authorship_enum.py.
# ---------------------------------------------------------------------------


def test_user_item_is_executable():
    item = {"authorship": "user", "state": "pending"}
    assert action_items.is_executable(item) is True


def test_agent_unapproved_is_not_executable():
    item = {"authorship": "agent_unapproved", "state": "pending"}
    assert action_items.is_executable(item) is False


def test_agent_approved_is_executable():
    item = {"authorship": "agent_approved", "state": "in_progress"}
    assert action_items.is_executable(item) is True


def test_done_item_is_not_executable_even_if_user_authored():
    item = {"authorship": "user", "state": "done"}
    assert action_items.is_executable(item) is False


def test_skipped_item_is_not_executable():
    item = {"authorship": "user", "state": "skipped"}
    assert action_items.is_executable(item) is False


def test_missing_authorship_treated_as_unapproved():
    """Items without authorship default to gate-blocked (safe)."""
    assert action_items.is_executable({"state": "pending"}) is False


# ---------------------------------------------------------------------------
# position_in_task
# ---------------------------------------------------------------------------


def test_position_in_task_returns_one_based_index(fresh_db):
    store.create(task_id="t-ai-12")
    a = action_items.create(task_id="t-ai-12", description="a")
    b = action_items.create(task_id="t-ai-12", description="b")
    c = action_items.create(task_id="t-ai-12", description="c")
    pos_a, total_a = action_items.position_in_task(action_items.get(a["id"]))
    pos_b, total_b = action_items.position_in_task(action_items.get(b["id"]))
    pos_c, total_c = action_items.position_in_task(action_items.get(c["id"]))
    assert (pos_a, total_a) == (1, 3)
    assert (pos_b, total_b) == (2, 3)
    assert (pos_c, total_c) == (3, 3)


def test_position_in_task_handles_unknown_item(fresh_db):
    """Item not in any task returns (1, 1) defensively."""
    pos, total = action_items.position_in_task({"id": 999, "task_id": "t-x"})
    assert (pos, total) == (1, 1)
