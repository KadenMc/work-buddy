"""Slice 5a tests — task_metadata schema migration + CRUD round-trip.

Three concerns:

1. The migration adds the three new columns
   (``agent_required_contexts``, ``user_required_contexts``,
   ``required_contexts_source``) to fresh and pre-existing databases.
2. ``store.create`` accepts the new kwargs and round-trips them.
3. ``store.update`` honours the sentinel discipline — explicit None
   clears the value; not-provided leaves it unchanged.
"""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks import store


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Point the store at a fresh sqlite file per-test."""
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


def _columns(db_path) -> set[str]:
    conn = store.get_connection()
    try:
        return {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_metadata)")
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_fresh_db_has_slice_5a_columns(fresh_db):
    cols = _columns(fresh_db)
    assert "agent_required_contexts" in cols
    assert "user_required_contexts" in cols
    assert "required_contexts_source" in cols


def test_migration_adds_columns_to_existing_db(tmp_path, monkeypatch):
    """Simulate a pre-Slice-5a DB and verify the migration adds the columns.

    The DB is stamped at user_version=4 so the migration runner knows
    to apply m005 (Slice-5a context arrays) and onward. Without the
    stamp, baseline-detect would assume fully-migrated (correct for
    real production legacy DBs, wrong for this partial-schema
    simulation)."""
    import sqlite3
    db = tmp_path / "tasks.sqlite3"

    # Build a stripped-down task_metadata at the v4 schema layer
    # (post-Slice-4, pre-Slice-5a). The runner will roll forward
    # from v4 to current.
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
        legacy.execute("PRAGMA user_version = 4")
        legacy.commit()
    finally:
        legacy.close()

    monkeypatch.setattr(store, "_db_path", lambda: db)
    # Trigger migration via get_connection.
    conn = store.get_connection()
    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_metadata)")
        }
    finally:
        conn.close()
    assert "agent_required_contexts" in cols
    assert "user_required_contexts" in cols
    assert "required_contexts_source" in cols


# ---------------------------------------------------------------------------
# create + get round-trip
# ---------------------------------------------------------------------------


def test_create_round_trips_context_fields(fresh_db):
    store.create(
        task_id="t-ctx-001",
        agent_required_contexts='["@filesystem"]',
        user_required_contexts='["@user_workstation"]',
        required_contexts_source="agent_inferred",
    )
    row = store.get("t-ctx-001")
    assert row is not None
    assert row["agent_required_contexts"] == '["@filesystem"]'
    assert row["user_required_contexts"] == '["@user_workstation"]'
    assert row["required_contexts_source"] == "agent_inferred"


def test_create_defaults_context_fields_to_null(fresh_db):
    """Legacy callers don't pass the new kwargs — values stay NULL."""
    store.create(task_id="t-ctx-legacy")
    row = store.get("t-ctx-legacy")
    assert row["agent_required_contexts"] is None
    assert row["user_required_contexts"] is None
    assert row["required_contexts_source"] is None


def test_create_rejects_invalid_context_source(fresh_db):
    with pytest.raises(ValueError):
        store.create(
            task_id="t-ctx-bad",
            required_contexts_source="not_a_real_source",
        )


# ---------------------------------------------------------------------------
# update sentinel discipline
# ---------------------------------------------------------------------------


def test_update_sets_context_fields(fresh_db):
    store.create(task_id="t-ctx-002")
    store.update(
        "t-ctx-002",
        agent_required_contexts='["@vault"]',
        user_required_contexts='[]',
        required_contexts_source="user_authored",
    )
    row = store.get("t-ctx-002")
    assert row["agent_required_contexts"] == '["@vault"]'
    assert row["user_required_contexts"] == "[]"
    assert row["required_contexts_source"] == "user_authored"


def test_update_clear_with_explicit_none(fresh_db):
    store.create(
        task_id="t-ctx-003",
        agent_required_contexts='["@filesystem"]',
        required_contexts_source="agent_inferred",
    )
    store.update("t-ctx-003", agent_required_contexts=None)
    row = store.get("t-ctx-003")
    assert row["agent_required_contexts"] is None
    # Source untouched
    assert row["required_contexts_source"] == "agent_inferred"


def test_update_omitted_field_unchanged(fresh_db):
    store.create(
        task_id="t-ctx-004",
        agent_required_contexts='["@filesystem"]',
    )
    # Update a different field; contexts should stay put.
    store.update("t-ctx-004", urgency="high")
    row = store.get("t-ctx-004")
    assert row["agent_required_contexts"] == '["@filesystem"]'


def test_update_rejects_invalid_context_source(fresh_db):
    store.create(task_id="t-ctx-005")
    with pytest.raises(ValueError):
        store.update("t-ctx-005", required_contexts_source="garbage")
