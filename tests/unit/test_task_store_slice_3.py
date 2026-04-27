"""Slice 3 schema additions to task_metadata: description column.

Mirrors test_task_store_slice_2 — exercises the migration end-to-end
against a real SQLite db in tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.obsidian.tasks import store


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> Path:
    """Point the task store's _db_path() at a tmp directory."""
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    return db_path


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_fresh_db_has_description_column(isolated_store: Path) -> None:
    """Brand-new DB created via get_connection() must contain the
    description column out of the box."""
    conn = store.get_connection()
    try:
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_metadata)")
        }
    finally:
        conn.close()
    assert "description" in existing


def test_legacy_pre_slice_3_db_gets_description_via_migrate(
    isolated_store: Path,
) -> None:
    """A pre-Slice-3 DB (Slice 2 columns but no description) must get
    the description column via _migrate_schema on next get_connection."""
    import sqlite3
    conn = sqlite3.connect(str(isolated_store))
    # Hand-craft a Slice-2 table without `description`.
    conn.executescript("""
        CREATE TABLE task_metadata (
            task_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'inbox',
            urgency TEXT NOT NULL DEFAULT 'medium',
            complexity TEXT,
            contract TEXT,
            note_uuid TEXT,
            snooze_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            archived_at TEXT,
            task_kind TEXT NOT NULL DEFAULT 'task',
            density TEXT NOT NULL DEFAULT 'sparse',
            outcome_text TEXT,
            next_action_text TEXT,
            definition_of_done TEXT,
            creation_effort TEXT NOT NULL DEFAULT 'developed',
            user_involvement TEXT NOT NULL DEFAULT 'high',
            creation_provenance TEXT NOT NULL DEFAULT 'manual',
            has_deadline INTEGER NOT NULL DEFAULT 0,
            deadline_date TEXT,
            has_dependency INTEGER NOT NULL DEFAULT 0,
            dependency_hint TEXT
        );
    """)
    conn.execute(
        """INSERT INTO task_metadata
           (task_id, state, urgency, created_at, updated_at)
           VALUES ('t-legacy3', 'inbox', 'medium', 'now', 'now')"""
    )
    conn.commit()
    conn.close()

    conn = store.get_connection()
    try:
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_metadata)")
        }
        assert "description" in existing

        # Existing legacy row gets NULL — backfill is task_sync's job.
        row = conn.execute(
            "SELECT description FROM task_metadata WHERE task_id = 't-legacy3'"
        ).fetchone()
        assert row["description"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


def test_create_with_description_persists(isolated_store: Path) -> None:
    store.create(
        task_id="t-x1",
        state="inbox",
        urgency="medium",
        description="Fix the auth bug",
    )
    row = store.get("t-x1")
    assert row is not None
    assert row["description"] == "Fix the auth bug"


def test_create_without_description_is_null(isolated_store: Path) -> None:
    store.create(task_id="t-x2", state="inbox", urgency="medium")
    row = store.get("t-x2")
    assert row is not None
    assert row["description"] is None


def test_update_description_replaces_value(isolated_store: Path) -> None:
    store.create(
        task_id="t-x3", state="inbox", urgency="medium",
        description="Original text",
    )
    store.update("t-x3", description="Rewritten text")
    row = store.get("t-x3")
    assert row["description"] == "Rewritten text"


def test_update_description_to_none_clears(isolated_store: Path) -> None:
    """Sentinel-discipline: passing None explicitly clears the field
    (vs. omitting which leaves it untouched)."""
    store.create(
        task_id="t-x4", state="inbox", urgency="medium",
        description="Will be cleared",
    )
    store.update("t-x4", description=None)
    row = store.get("t-x4")
    assert row["description"] is None


def test_update_without_description_keeps_existing(isolated_store: Path) -> None:
    store.create(
        task_id="t-x5", state="inbox", urgency="medium",
        description="Keep me",
    )
    # Update other fields; description sentinel stays untouched.
    store.update("t-x5", urgency="high")
    row = store.get("t-x5")
    assert row["description"] == "Keep me"
    assert row["urgency"] == "high"


# ---------------------------------------------------------------------------
# search_by_description
# ---------------------------------------------------------------------------


def test_search_by_description_basic(isolated_store: Path) -> None:
    store.create(
        task_id="t-s1", state="inbox", urgency="medium",
        description="Fix the auth bug in login flow",
    )
    store.create(
        task_id="t-s2", state="inbox", urgency="medium",
        description="Refactor the dashboard",
    )
    store.create(
        task_id="t-s3", state="inbox", urgency="medium",
        description="Investigate the auth provider",
    )
    results = store.search_by_description("auth")
    ids = {r["task_id"] for r in results}
    assert ids == {"t-s1", "t-s3"}


def test_search_by_description_case_insensitive(isolated_store: Path) -> None:
    store.create(
        task_id="t-s4", state="inbox", urgency="medium",
        description="Investigate Kubernetes deployment",
    )
    results = store.search_by_description("kubernetes")
    assert len(results) == 1
    assert results[0]["task_id"] == "t-s4"


def test_search_by_description_empty_returns_nothing(isolated_store: Path) -> None:
    store.create(
        task_id="t-s5", state="inbox", urgency="medium",
        description="anything",
    )
    assert store.search_by_description("") == []
    assert store.search_by_description("   ") == []


def test_search_by_description_skips_null(isolated_store: Path) -> None:
    """NULL descriptions should be filtered out (legacy rows)."""
    store.create(task_id="t-s6", state="inbox", urgency="medium")  # no description
    store.create(
        task_id="t-s7", state="inbox", urgency="medium",
        description="something",
    )
    results = store.search_by_description("something")
    assert len(results) == 1
    assert results[0]["task_id"] == "t-s7"


def test_search_by_description_excludes_archived_by_default(
    isolated_store: Path,
) -> None:
    store.create(
        task_id="t-s8", state="inbox", urgency="medium",
        description="archived task to find",
    )
    store.mark_archived("t-s8")
    assert store.search_by_description("archived") == []
    # But include_archived=True surfaces it.
    results = store.search_by_description("archived", include_archived=True)
    assert len(results) == 1


def test_search_by_description_excludes_done_optionally(
    isolated_store: Path,
) -> None:
    store.create(
        task_id="t-s9", state="done", urgency="medium",
        description="done task with text",
    )
    # Default: include_done=True (the LIKE search returns both states).
    assert len(store.search_by_description("done task")) == 1
    # Opt out:
    assert store.search_by_description("done task", include_done=False) == []


def test_search_by_description_escapes_wildcards(isolated_store: Path) -> None:
    """Pass-through % / _ in the query must NOT broaden the match."""
    store.create(
        task_id="t-s10", state="inbox", urgency="medium",
        description="100% done — really",
    )
    store.create(
        task_id="t-s11", state="inbox", urgency="medium",
        description="50 done",
    )
    # Without escaping, '100%' would match both rows. With escaping, only t-s10.
    results = store.search_by_description("100%")
    assert len(results) == 1
    assert results[0]["task_id"] == "t-s10"


def test_search_by_description_respects_limit(isolated_store: Path) -> None:
    for i in range(5):
        store.create(
            task_id=f"t-l{i}",
            state="inbox", urgency="medium",
            description=f"foo task number {i}",
        )
    assert len(store.search_by_description("foo", limit=2)) == 2
    assert len(store.search_by_description("foo", limit=100)) == 5
