"""Slice 7 PR #70 fix #2: ``authorship`` enum migration coverage.

Pins the new canonical column + the back-compat shim to the legacy
``user_authored`` + ``approved_at`` fields.  The shim exists so
existing callers (tests, dashboards, sidecar jobs) keep working
until they migrate.
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


def test_table_has_authorship_column(fresh_db):
    conn = store.get_connection()
    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_action_items)")
        }
    finally:
        conn.close()
    assert "authorship" in cols
    # Legacy fields kept for back-compat.
    assert "user_authored" in cols
    assert "approved_at" in cols


def test_existing_db_gets_backfilled(tmp_path, monkeypatch):
    """Simulate a pre-PR-70 DB and verify the migration backfills authorship."""
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
        rows = conn.execute(
            "SELECT description, authorship FROM task_action_items "
            "ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()
    by_desc = {r["description"]: r["authorship"] for r in rows}
    assert by_desc["user wrote this"] == "user"
    assert by_desc["agent approved"] == "agent_approved"
    assert by_desc["agent unapproved"] == "agent_unapproved"


# ---------------------------------------------------------------------------
# create() + back-compat shim
# ---------------------------------------------------------------------------


def test_create_with_authorship_user(fresh_db):
    store.create(task_id="t-au-1")
    a = action_items.create(
        task_id="t-au-1", description="x", authorship="user",
    )
    row = action_items.get(a["id"])
    assert row["authorship"] == "user"
    assert row["user_authored"] == 1


def test_create_with_authorship_agent_approved_stamps_approved_at(fresh_db):
    store.create(task_id="t-au-2")
    a = action_items.create(
        task_id="t-au-2", description="x", authorship="agent_approved",
    )
    row = action_items.get(a["id"])
    assert row["authorship"] == "agent_approved"
    assert row["user_authored"] == 0
    assert row["approved_at"] is not None  # auto-stamped


def test_create_with_authorship_agent_unapproved_default(fresh_db):
    """No authorship + no legacy fields -> defaults to agent_unapproved
    (the safe option that gates execution)."""
    store.create(task_id="t-au-3")
    a = action_items.create(task_id="t-au-3", description="x")
    row = action_items.get(a["id"])
    assert row["authorship"] == "agent_unapproved"


def test_create_with_legacy_user_authored_true(fresh_db):
    """Legacy callers passing user_authored=True still get authorship='user'."""
    store.create(task_id="t-au-4")
    a = action_items.create(
        task_id="t-au-4", description="x", user_authored=True,
    )
    row = action_items.get(a["id"])
    assert row["authorship"] == "user"
    assert row["user_authored"] == 1


def test_create_with_legacy_approved_at_only(fresh_db):
    """user_authored=False + approved_at set -> authorship='agent_approved'."""
    store.create(task_id="t-au-5")
    a = action_items.create(
        task_id="t-au-5", description="x",
        user_authored=False,
        approved_at="2026-04-01T12:00:00+00:00",
    )
    row = action_items.get(a["id"])
    assert row["authorship"] == "agent_approved"
    assert row["approved_at"] == "2026-04-01T12:00:00+00:00"


def test_create_rejects_invalid_authorship(fresh_db):
    store.create(task_id="t-au-6")
    with pytest.raises(ValueError):
        action_items.create(
            task_id="t-au-6", description="x", authorship="garbage",
        )


# ---------------------------------------------------------------------------
# update() — authorship + back-compat
# ---------------------------------------------------------------------------


def test_update_authorship_keeps_legacy_fields_in_sync(fresh_db):
    store.create(task_id="t-au-7")
    a = action_items.create(
        task_id="t-au-7", description="x", authorship="agent_unapproved",
    )
    action_items.update(a["id"], authorship="agent_approved")
    row = action_items.get(a["id"])
    assert row["authorship"] == "agent_approved"
    assert row["approved_at"] is not None
    assert row["user_authored"] == 0


def test_update_legacy_user_authored_recomputes_authorship(fresh_db):
    """Legacy caller setting user_authored=True bumps authorship='user'."""
    store.create(task_id="t-au-8")
    a = action_items.create(
        task_id="t-au-8", description="x", authorship="agent_approved",
    )
    action_items.update(a["id"], user_authored=True)
    row = action_items.get(a["id"])
    assert row["authorship"] == "user"
    assert row["user_authored"] == 1


# ---------------------------------------------------------------------------
# is_executable -- enum reading + legacy fallback
# ---------------------------------------------------------------------------


def test_is_executable_reads_authorship_enum():
    assert action_items.is_executable({
        "state": "pending", "authorship": "user",
    }) is True
    assert action_items.is_executable({
        "state": "pending", "authorship": "agent_approved",
    }) is True
    assert action_items.is_executable({
        "state": "pending", "authorship": "agent_unapproved",
    }) is False


def test_is_executable_terminal_states_blocked_regardless():
    for terminal in ("done", "skipped"):
        assert action_items.is_executable({
            "state": terminal, "authorship": "user",
        }) is False


def test_is_executable_legacy_fallback_when_authorship_absent():
    """Items constructed without the authorship key (older tests, raw
    dicts) fall back to the (user_authored, approved_at) check."""
    assert action_items.is_executable({
        "state": "pending", "user_authored": 1,
    }) is True
    assert action_items.is_executable({
        "state": "pending", "user_authored": 0,
        "approved_at": "2026-04-01T00:00:00+00:00",
    }) is True
    assert action_items.is_executable({
        "state": "pending", "user_authored": 0, "approved_at": None,
    }) is False
