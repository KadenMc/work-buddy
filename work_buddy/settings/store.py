"""SQLite persistence for personal settings values and pending transitions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from work_buddy.settings.migrations import SETTINGS_MIGRATIONS


_schema_ready: set[str] = set()


def _db_path() -> Path:
    from work_buddy.paths import resolve

    return resolve("db/settings")


def get_connection() -> sqlite3.Connection:
    """Open Settings state and migrate each resolved database path once."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    key = str(path.resolve())
    if key not in _schema_ready:
        try:
            SETTINGS_MIGRATIONS.run(conn)
        except Exception:
            conn.close()
            raise
        _schema_ready.add(key)
    return conn
