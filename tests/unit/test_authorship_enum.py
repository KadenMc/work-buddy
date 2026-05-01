"""Slice 7 PR #70 fix #2: ``authorship`` enum coverage.

The legacy ``user_authored`` + ``approved_at`` columns were removed
in the follow-up cleanup; ``authorship`` is now the sole source of
truth.  These tests pin:

- The enum values, validation, and the safe default ('agent_unapproved').
- Schema migration: pre-PR-70 DBs get the column added + backfilled
  + the legacy columns dropped, all in one connection-open pass.
- ``is_executable`` reads only the enum (no more legacy fallback).
"""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks import action_items, store


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_table_has_authorship_only(fresh_db):
    """Fresh DB has authorship; legacy columns are gone."""
    conn = store.get_connection()
    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_action_items)")
        }
    finally:
        conn.close()
    assert "authorship" in cols
    assert "user_authored" not in cols
    assert "approved_at" not in cols


def test_existing_db_gets_backfilled_then_dropped(tmp_path, monkeypatch):
    """Simulate a pre-PR-70 DB: migration adds authorship, backfills
    from the legacy columns, then drops them in the same pass."""
    import sqlite3
    db = tmp_path / "tasks.sqlite3"

    legacy = sqlite3.connect(str(db))
    try:
        legacy.execute(
            """CREATE TABLE task_metadata (
                task_id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'inbox',
                urgency TEXT NOT NULL DEFAULT 'medium',
                contract TEXT,
                archived_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        legacy.execute(
            """CREATE TABLE task_action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                description TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                user_authored INTEGER NOT NULL DEFAULT 0,
                approved_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        # Three legacy rows: one of each authorship class.
        legacy.execute(
            "INSERT INTO task_action_items "
            "(task_id, sequence, description, user_authored, approved_at, "
            " created_at, updated_at) VALUES "
            "('t-x', 1, 'user wrote this',  1, NULL,  '2026-01-01', '2026-01-01'),"
            "('t-x', 2, 'agent approved',   0, '2026-04-01T12:00:00', "
            "  '2026-01-01', '2026-01-01'),"
            "('t-x', 3, 'agent unapproved', 0, NULL,  '2026-01-01', '2026-01-01')"
        )
        legacy.commit()
    finally:
        legacy.close()

    monkeypatch.setattr(store, "_db_path", lambda: db)
    conn = store.get_connection()
    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_action_items)")
        }
        rows = conn.execute(
            "SELECT description, authorship FROM task_action_items "
            "ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()

    # Legacy columns gone post-migration.
    assert "user_authored" not in cols
    assert "approved_at" not in cols
    assert "authorship" in cols

    by_desc = {r["description"]: r["authorship"] for r in rows}
    assert by_desc["user wrote this"] == "user"
    assert by_desc["agent approved"] == "agent_approved"
    assert by_desc["agent unapproved"] == "agent_unapproved"


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


def test_create_with_authorship_user(fresh_db):
    store.create(task_id="t-au-1")
    a = action_items.create(
        task_id="t-au-1", description="x", authorship="user",
    )
    assert action_items.get(a["id"])["authorship"] == "user"


def test_create_with_authorship_agent_approved(fresh_db):
    store.create(task_id="t-au-2")
    a = action_items.create(
        task_id="t-au-2", description="x", authorship="agent_approved",
    )
    assert action_items.get(a["id"])["authorship"] == "agent_approved"


def test_create_default_is_agent_unapproved(fresh_db):
    """No authorship -> defaults to 'agent_unapproved' (gate-blocked)."""
    store.create(task_id="t-au-3")
    a = action_items.create(task_id="t-au-3", description="x")
    assert action_items.get(a["id"])["authorship"] == "agent_unapproved"


def test_create_rejects_invalid_authorship(fresh_db):
    store.create(task_id="t-au-6")
    with pytest.raises(ValueError):
        action_items.create(
            task_id="t-au-6", description="x", authorship="garbage",
        )


# ---------------------------------------------------------------------------
# update()
# ---------------------------------------------------------------------------


def test_update_authorship_changes_value(fresh_db):
    store.create(task_id="t-au-7")
    a = action_items.create(
        task_id="t-au-7", description="x", authorship="agent_unapproved",
    )
    action_items.update(a["id"], authorship="agent_approved")
    assert action_items.get(a["id"])["authorship"] == "agent_approved"


def test_update_rejects_invalid_authorship(fresh_db):
    store.create(task_id="t-au-7b")
    a = action_items.create(
        task_id="t-au-7b", description="x", authorship="agent_unapproved",
    )
    with pytest.raises(ValueError):
        action_items.update(a["id"], authorship="garbage")


def test_update_omitted_authorship_unchanged(fresh_db):
    """authorship=None on update leaves the value alone."""
    store.create(task_id="t-au-7c")
    a = action_items.create(
        task_id="t-au-7c", description="x", authorship="agent_approved",
    )
    action_items.update(a["id"], description="new desc")
    row = action_items.get(a["id"])
    assert row["authorship"] == "agent_approved"
    assert row["description"] == "new desc"


# ---------------------------------------------------------------------------
# is_executable -- canonical authorship reading.
# ---------------------------------------------------------------------------


def test_is_executable_admits_user():
    assert action_items.is_executable({
        "state": "pending", "authorship": "user",
    }) is True


def test_is_executable_admits_agent_approved():
    assert action_items.is_executable({
        "state": "in_progress", "authorship": "agent_approved",
    }) is True


def test_is_executable_blocks_agent_unapproved():
    assert action_items.is_executable({
        "state": "pending", "authorship": "agent_unapproved",
    }) is False


def test_is_executable_blocks_terminal_state():
    for terminal in ("done", "skipped"):
        assert action_items.is_executable({
            "state": terminal, "authorship": "user",
        }) is False


def test_is_executable_missing_authorship_blocks():
    """Items without authorship default to gate-blocked (safe)."""
    assert action_items.is_executable({"state": "pending"}) is False
