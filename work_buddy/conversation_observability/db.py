"""Connection + schema setup for the conversation-observability DB.

Mirrors the messaging/llm_queue convention:
* WAL mode (concurrent reader-friendly; IR indexing may run while a
  context bundle reads the same DB).
* Idempotent schema creation on every connection.
* Config-driven path (override via
  ``conversation_observability.db_path`` in config.local.yaml; default
  is ``<data_root>/conversation_observability/conversation_observability.db``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from work_buddy.conversation_observability.schema import SCHEMA
from work_buddy.paths import data_dir


def _default_db_path() -> Path:
    return data_dir("conversation_observability") / "conversation_observability.db"


def db_path(cfg: dict | None = None) -> Path:
    """Resolve the DB path from config, falling back to the default."""
    if cfg is None:
        from work_buddy.config import load_config

        cfg = load_config()
    explicit = (cfg.get("conversation_observability") or {}).get("db_path")
    if explicit:
        return Path(explicit)
    return _default_db_path()


def get_connection(cfg: dict | None = None) -> sqlite3.Connection:
    """Open (or create) the conversation-observability DB.

    Idempotent: schema is re-run on every connect via
    ``CREATE TABLE IF NOT EXISTS`` so a missing file becomes a populated
    one transparently. ``Row`` factory is set so callers can use column
    access by name. Forward-only ALTER migrations run on every connect
    so existing DBs pick up new columns without intervention.
    """
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 30s busy-timeout: WAL mode allows concurrent readers + a single
    # writer, but writers compete for the write lock. The sidecar
    # refresh cron and inline collector refreshes can fire close
    # together; 30s gives the slow side enough headroom to complete a
    # batch before the next caller times out. The refresh functions
    # themselves keep transactions short (one commit per session) so
    # the worst-case wait is bounded by single-session work, not by
    # entire scans.
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Forward-only column additions on ``observed_sessions``, plus
    legacy-table cleanup.

    ``CREATE TABLE IF NOT EXISTS`` cannot add columns to a pre-existing
    table. Each new column gets its own ``ALTER TABLE`` here; the
    list-of-strings shape keeps additions cheap to declare without
    spawning a versioned migration framework for what's still a small
    schema.

    Also drops legacy ``session_summaries`` and ``topic_summaries``
    tables (2026-05-28 ablation). Data was one-shot-migrated into the
    summarization framework's ``summary_items`` + ``summary_nodes``
    before the drop. Idempotent: ``DROP TABLE IF EXISTS``.
    """
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(observed_sessions)")
    }
    for col_name, col_decl in (
        ("commits_scanned_mtime", "REAL"),
        ("writes_scanned_mtime", "REAL"),
        ("prs_scanned_mtime", "REAL"),
        ("note_reads_scanned_mtime", "REAL"),
    ):
        if col_name not in cols:
            conn.execute(
                f"ALTER TABLE observed_sessions ADD COLUMN {col_name} {col_decl}"
            )
    # Drop legacy summary tables (migration completed 2026-05-28).
    conn.execute("DROP TABLE IF EXISTS topic_summaries")
    conn.execute("DROP TABLE IF EXISTS session_summaries")
    conn.commit()
