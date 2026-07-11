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


# DB paths whose schema has already been ensured this process. The
# schema + ALTER migration pass is idempotent but not free (a full
# executescript + a PRAGMA table_info per connect); the sidecar refresh
# cron and inline collector open this DB frequently, so running it on
# every connect put that cost on every open. Keyed on the resolved path
# (which varies with ``cfg``) so a test pointing at a fresh DB still
# migrates it exactly once. Process-lifetime; assumes the DB file is not
# externally deleted out from under a live process.
_schema_ready: set[str] = set()


def get_connection(cfg: dict | None = None) -> sqlite3.Connection:
    """Open (or create) the conversation-observability DB.

    Idempotent: the schema + forward-only ALTER migrations are ensured
    once per DB path per process (see ``_schema_ready``), so a missing
    file becomes a populated one transparently on first open. ``Row``
    factory is set so callers can use column access by name; the
    per-connection WAL pragma always runs.
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
    key = str(path)
    if key not in _schema_ready:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        _schema_ready.add(key)
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
        ("harness_id", "TEXT NOT NULL DEFAULT 'claudecode'"),
        ("native_session_id", "TEXT"),
        ("cwd", "TEXT"),
        ("commits_scanned_mtime", "REAL"),
        ("writes_scanned_mtime", "REAL"),
        ("prs_scanned_mtime", "REAL"),
        ("note_reads_scanned_mtime", "REAL"),
    ):
        if col_name not in cols:
            conn.execute(
                f"ALTER TABLE observed_sessions ADD COLUMN {col_name} {col_decl}"
            )
    # This index must be created after the forward migration. Putting it in
    # SCHEMA breaks legacy databases whose observed_sessions table predates
    # harness_id: CREATE TABLE IF NOT EXISTS preserves the old table shape.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_observed_sessions_harness "
        "ON observed_sessions(harness_id)"
    )
    # Drop legacy summary tables (migration completed 2026-05-28).
    conn.execute("DROP TABLE IF EXISTS topic_summaries")
    conn.execute("DROP TABLE IF EXISTS session_summaries")
    conn.commit()
