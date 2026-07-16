from __future__ import annotations

import sqlite3

import pytest

from work_buddy.settings import store
from work_buddy.settings.migrations import SETTINGS_MIGRATIONS
from work_buddy.storage.migrations import SchemaVersionTooNew


@pytest.fixture(autouse=True)
def clear_schema_readiness():
    store._schema_ready.clear()
    yield
    store._schema_ready.clear()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def test_fresh_settings_database_reaches_current_schema(tmp_path) -> None:
    db_path = tmp_path / "settings.db"
    conn = sqlite3.connect(db_path)
    try:
        SETTINGS_MIGRATIONS.run(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert {
            "setting_value_state",
            "journal_day_policy_epoch",
            "_migration_history",
        }.issubset(_tables(conn))
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(setting_value_state)")
        }
        assert {
            "bootstrap_default_value_json",
            "bootstrap_source",
            "value_version",
            "active_timezone",
            "pending_timezone",
            "applied_from_value_json",
            "applied_at",
        }.issubset(columns)
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(journal_day_policy_epoch)")
        }
        assert {
            "uq_journal_day_policy_base",
            "uq_journal_day_policy_date",
        }.issubset(indexes)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO setting_value_state (
                    setting_id, scope, scope_id, pending_source, updated_at
                ) VALUES ('invalid', 'profile', 'default', 'outside-contract', 'now')
                """
            )
    finally:
        conn.close()


def test_unversioned_early_database_upgrades_without_losing_values(tmp_path) -> None:
    db_path = tmp_path / "settings.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE setting_value_state (
                setting_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                active_value_json TEXT,
                pending_value_json TEXT,
                pending_source TEXT,
                effective_at TEXT,
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (setting_id, scope, scope_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO setting_value_state (
                setting_id, scope, scope_id, active_value_json,
                pending_value_json, pending_source, effective_at,
                revision, updated_at
            ) VALUES (?, 'profile', 'default', ?, ?, 'profile', ?, 7, ?)
            """,
            (
                "wb.journal.day-boundary",
                '"05:00"',
                '"04:00"',
                "2026-07-16T05:00:00-04:00",
                "2026-07-15T12:00:00-04:00",
            ),
        )
        conn.commit()

        SETTINGS_MIGRATIONS.run(conn)

        row = conn.execute(
            "SELECT * FROM setting_value_state WHERE setting_id = ?",
            ("wb.journal.day-boundary",),
        ).fetchone()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert row[7] == '"05:00"'
        assert row[8] == '"04:00"'
        assert row[9] == "profile"
        assert row[14] == 7
        assert "journal_day_policy_epoch" in _tables(conn)
    finally:
        conn.close()


def test_store_runs_migrations_once_per_resolved_path(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "settings.db"
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    calls: list[str] = []
    original_run = SETTINGS_MIGRATIONS.run

    def counted_run(conn):
        calls.append(str(db_path))
        return original_run(conn)

    monkeypatch.setattr(SETTINGS_MIGRATIONS, "run", counted_run)
    first = store.get_connection()
    first.close()
    second = store.get_connection()
    second.close()
    assert calls == [str(db_path)]


def test_store_migrates_each_distinct_path(monkeypatch, tmp_path) -> None:
    paths = iter([tmp_path / "one.db", tmp_path / "two.db"])
    monkeypatch.setattr(store, "_db_path", lambda: next(paths))
    first = store.get_connection()
    first.close()
    second = store.get_connection()
    second.close()
    assert len(store._schema_ready) == 2


def test_settings_downgrade_guard_rejects_newer_schema(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "settings.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA user_version = 999")
    finally:
        conn.close()
    monkeypatch.setattr(store, "_db_path", lambda: db_path)
    with pytest.raises(SchemaVersionTooNew):
        store.get_connection()
