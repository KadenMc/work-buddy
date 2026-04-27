"""Slice 2 schema additions to task_metadata: GTD vocabulary fields.

Tests against a real SQLite db in tmp_path so the migration logic gets
exercised end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.obsidian.tasks import store


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> Path:
    """Point the task store's _db_path() at a tmp directory.

    The store is module-scoped (no class instance) so we monkey-patch
    the path resolver. Returns the db path for assertions.
    """
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    return db_path


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_fresh_db_has_all_slice_2_columns(isolated_store: Path) -> None:
    """A brand-new DB created via get_connection() must contain every
    Slice 2 column out of the box (covered by _SCHEMA's CREATE TABLE)."""
    conn = store.get_connection()
    try:
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_metadata)")
        }
    finally:
        conn.close()
    expected = {col[0] for col in store._SLICE_2_COLUMNS}
    missing = expected - existing
    assert not missing, f"missing columns in fresh DB: {missing}"


def test_legacy_db_gets_columns_via_migrate(
    isolated_store: Path, monkeypatch,
) -> None:
    """A pre-Slice-2 DB (no new columns) must get them via _migrate_schema
    on the next get_connection()."""
    import sqlite3
    # Hand-craft a legacy table with only the pre-Slice-2 columns.
    conn = sqlite3.connect(str(isolated_store))
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
            archived_at TEXT
        );
    """)
    conn.execute(
        """INSERT INTO task_metadata
           (task_id, state, urgency, created_at, updated_at)
           VALUES ('t-legacy', 'inbox', 'high', 'now', 'now')"""
    )
    conn.commit()
    conn.close()

    # First get_connection() runs _migrate_schema().
    conn = store.get_connection()
    try:
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_metadata)")
        }
        expected = {col[0] for col in store._SLICE_2_COLUMNS}
        missing = expected - existing
        assert not missing, f"missing after migrate: {missing}"

        # Existing legacy row got the defaults backfilled.
        row = conn.execute(
            "SELECT * FROM task_metadata WHERE task_id = 't-legacy'"
        ).fetchone()
        assert row["task_kind"] == "task"
        assert row["density"] == "sparse"
        assert row["creation_effort"] == "developed"
        assert row["user_involvement"] == "high"
        assert row["creation_provenance"] == "manual"
        assert row["has_deadline"] == 0
        assert row["has_dependency"] == 0
    finally:
        conn.close()


def test_migrate_is_idempotent(isolated_store: Path) -> None:
    """Calling _migrate_schema multiple times doesn't error."""
    store.get_connection().close()
    # Second call — columns already exist, should no-op.
    conn = store.get_connection()
    try:
        store._migrate_schema(conn)
        store._migrate_schema(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# create() with new params
# ---------------------------------------------------------------------------


def test_create_defaults_match_legacy_assumption(isolated_store: Path) -> None:
    """Calling create() with no Slice-2 args produces a row that looks
    like a legacy task: task='task', density='sparse', creation_effort
    ='developed', user_involvement='high', provenance='manual'."""
    store.create("t-default")
    row = store.get("t-default")
    assert row["task_kind"] == "task"
    assert row["density"] == "sparse"
    assert row["creation_effort"] == "developed"
    assert row["user_involvement"] == "high"
    assert row["creation_provenance"] == "manual"
    assert row["has_deadline"] == 0
    assert row["deadline_date"] is None
    assert row["has_dependency"] == 0
    assert row["dependency_hint"] is None
    assert row["outcome_text"] is None
    assert row["next_action_text"] is None
    assert row["definition_of_done"] is None


def test_create_with_developed_density(isolated_store: Path) -> None:
    store.create(
        "t-dev",
        density="developed",
        outcome_text="ECG classifier publishable",
        next_action_text="Run augmentation experiment",
        definition_of_done="ROC-AUC > 0.92 on holdout",
        creation_effort="developed",
        user_involvement="high",
    )
    row = store.get("t-dev")
    assert row["density"] == "developed"
    assert row["outcome_text"] == "ECG classifier publishable"
    assert row["next_action_text"] == "Run augmentation experiment"
    assert row["definition_of_done"] == "ROC-AUC > 0.92 on holdout"


def test_create_with_deadline_and_dependency(isolated_store: Path) -> None:
    store.create(
        "t-time",
        has_deadline=True,
        deadline_date="2026-05-12",
        has_dependency=True,
        dependency_hint="Ben's review",
    )
    row = store.get("t-time")
    assert row["has_deadline"] == 1
    assert row["deadline_date"] == "2026-05-12"
    assert row["has_dependency"] == 1
    assert row["dependency_hint"] == "Ben's review"


def test_create_with_agent_provenance(isolated_store: Path) -> None:
    """Provenance is open — agent-inferred values should round-trip."""
    store.create(
        "t-agent",
        creation_effort="sparse",
        user_involvement="low",
        creation_provenance="agent_inferred_from_journal",
    )
    row = store.get("t-agent")
    assert row["creation_effort"] == "sparse"
    assert row["user_involvement"] == "low"
    assert row["creation_provenance"] == "agent_inferred_from_journal"


def test_create_rejects_invalid_task_kind(isolated_store: Path) -> None:
    with pytest.raises(ValueError, match="Invalid task_kind"):
        store.create("t-bad", task_kind="frobnitz")


def test_create_rejects_invalid_density(isolated_store: Path) -> None:
    with pytest.raises(ValueError, match="Invalid density"):
        store.create("t-bad2", density="nonsense")


def test_create_rejects_invalid_creation_effort(isolated_store: Path) -> None:
    with pytest.raises(ValueError, match="Invalid creation_effort"):
        store.create("t-bad3", creation_effort="extreme")


def test_create_rejects_invalid_user_involvement(isolated_store: Path) -> None:
    with pytest.raises(ValueError, match="Invalid user_involvement"):
        store.create("t-bad4", user_involvement="critical")


# ---------------------------------------------------------------------------
# update() with new params
# ---------------------------------------------------------------------------


def test_update_promotes_density_and_fields(isolated_store: Path) -> None:
    store.create("t-promo")
    store.update(
        "t-promo",
        density="developed",
        outcome_text="Done with this",
        next_action_text="Step 1",
    )
    row = store.get("t-promo")
    assert row["density"] == "developed"
    assert row["outcome_text"] == "Done with this"
    assert row["next_action_text"] == "Step 1"


def test_update_can_clear_text_fields(isolated_store: Path) -> None:
    """Sentinel discipline: passing None clears the field; not passing
    leaves it unchanged."""
    store.create(
        "t-clear",
        density="developed",
        outcome_text="initial outcome",
        next_action_text="initial action",
    )
    # Clear outcome only
    store.update("t-clear", outcome_text=None)
    row = store.get("t-clear")
    assert row["outcome_text"] is None
    assert row["next_action_text"] == "initial action"


def test_update_rejects_invalid_enums(isolated_store: Path) -> None:
    store.create("t-enum-check")
    with pytest.raises(ValueError):
        store.update("t-enum-check", task_kind="bogus")
    with pytest.raises(ValueError):
        store.update("t-enum-check", density="bogus")
    with pytest.raises(ValueError):
        store.update("t-enum-check", creation_effort="bogus")
    with pytest.raises(ValueError):
        store.update("t-enum-check", user_involvement="bogus")


def test_update_deadline_round_trips(isolated_store: Path) -> None:
    store.create("t-dl")
    store.update("t-dl", has_deadline=True, deadline_date="2026-06-01")
    row = store.get("t-dl")
    assert row["has_deadline"] == 1
    assert row["deadline_date"] == "2026-06-01"
    # Clear it
    store.update("t-dl", has_deadline=False, deadline_date=None)
    row = store.get("t-dl")
    assert row["has_deadline"] == 0
    assert row["deadline_date"] is None


def test_update_dependency_round_trips(isolated_store: Path) -> None:
    store.create("t-dep")
    store.update("t-dep", has_dependency=True, dependency_hint="needs API key")
    row = store.get("t-dep")
    assert row["has_dependency"] == 1
    assert row["dependency_hint"] == "needs API key"


# ---------------------------------------------------------------------------
# Enum constants
# ---------------------------------------------------------------------------


def test_enum_constants_complete():
    assert "task" in store.VALID_TASK_KINDS
    assert "periodic" in store.VALID_TASK_KINDS
    assert "habit" in store.VALID_TASK_KINDS
    assert "sparse" in store.VALID_DENSITIES
    assert "developed" in store.VALID_DENSITIES
    assert "dense" in store.VALID_DENSITIES  # forward-compat
    assert store.VALID_CREATION_EFFORTS == {"sparse", "medium", "developed"}
    assert store.VALID_USER_INVOLVEMENTS == {"low", "medium", "high"}
