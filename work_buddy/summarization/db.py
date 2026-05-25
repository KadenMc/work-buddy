"""Connection + schema setup for the summarization store.

Mirrors `conversation_observability/db.py`: WAL mode for concurrent readers,
idempotent schema creation on every connect, config-driven path (override via
`summarization.db_path` in `config.local.yaml`; default
`<data_root>/summarization/summarization.db`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from work_buddy.paths import data_dir
from work_buddy.summarization.schema import SCHEMA


def _default_db_path() -> Path:
    return data_dir("summarization") / "summarization.db"


def db_path(cfg: dict | None = None) -> Path:
    """Resolve the DB path from config, falling back to the default."""
    if cfg is None:
        from work_buddy.config import load_config

        cfg = load_config()
    explicit = (cfg.get("summarization") or {}).get("db_path")
    if explicit:
        return Path(explicit)
    return _default_db_path()


def get_connection(cfg: dict | None = None) -> sqlite3.Connection:
    """Open (or create) the summarization DB.

    Idempotent: `CREATE TABLE IF NOT EXISTS` re-runs on every connect, so a
    missing file becomes a populated one transparently. WAL mode allows
    concurrent readers + a single writer.
    """
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
