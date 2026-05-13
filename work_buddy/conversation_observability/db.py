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
    access by name.
    """
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
