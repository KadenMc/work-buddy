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
    concurrent readers + a single writer. Forward-only `ALTER TABLE` columns
    are added in `_migrate_schema` for existing DBs that pre-date the
    column.
    """
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Forward-only column additions on ``summary_items``.

    ``CREATE TABLE IF NOT EXISTS`` cannot add columns to a pre-existing
    table. Each new column gets its own ``ALTER TABLE`` here; the
    list-of-tuples shape keeps additions cheap to declare without
    spawning a versioned migration framework for what's still a small
    schema. Idempotent: re-running on a fully-migrated DB is a no-op.
    """
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(summary_items)")
    }
    # v2 additions (PRD F9 + F14). Order is deliberate so a debugger
    # reading PRAGMA table_info on a partial-migration DB can tell how
    # far the migration has progressed.
    additions = (
        ("total_turns", "INTEGER"),
        ("last_finalized_boundary", "INTEGER"),
        ("truncated", "INTEGER NOT NULL DEFAULT 0"),
        ("activity_kind", "TEXT"),
        ("pathway", "TEXT"),
        ("chunks_used", "INTEGER"),
        ("model_chain", "TEXT"),
        ("models_actually_used", "TEXT"),
        ("escalation_triggered", "INTEGER NOT NULL DEFAULT 0"),
        ("escalation_reason", "TEXT"),
    )
    for col_name, col_decl in additions:
        if col_name not in cols:
            conn.execute(
                f"ALTER TABLE summary_items ADD COLUMN {col_name} {col_decl}"
            )
    conn.commit()
