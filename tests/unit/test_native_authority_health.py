"""Read-only health ownership for native content authorities."""

from __future__ import annotations

import sqlite3


def _database(path, ddl: str, insert: str, *, version: int = 1) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(ddl)
        connection.execute(insert)
        connection.execute(f"PRAGMA user_version={version}")


def test_native_authority_check_does_not_create_missing_database(tmp_path):
    from work_buddy.health import checks

    path = tmp_path / "missing.db"
    result = checks._check_sqlite_authority(
        path,
        table="authority",
        query="SELECT mode FROM authority WHERE singleton=1",
        expected=("native",),
        label="Test SQLite",
    )

    assert result["ok"] is False
    assert not path.exists()


def test_native_authority_check_requires_exact_active_state(tmp_path):
    from work_buddy.health import checks

    path = tmp_path / "authority.db"
    _database(
        path,
        "CREATE TABLE authority(singleton INTEGER PRIMARY KEY, mode TEXT)",
        "INSERT INTO authority VALUES (1, 'legacy')",
        version=7,
    )
    legacy = checks._check_sqlite_authority(
        path,
        table="authority",
        query="SELECT mode FROM authority WHERE singleton=1",
        expected=("native",),
        label="Test SQLite",
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE authority SET mode='native' WHERE singleton=1")
    native = checks._check_sqlite_authority(
        path,
        table="authority",
        query="SELECT mode FROM authority WHERE singleton=1",
        expected=("native",),
        label="Test SQLite",
    )

    assert legacy["ok"] is False
    assert native == {"ok": True, "detail": "Test SQLite authority active (schema v7)"}
