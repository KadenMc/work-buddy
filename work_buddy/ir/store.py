"""SQLite document store + numpy vector storage for the IR engine.

Follows the messaging/models.py pattern: WAL mode, _SCHEMA auto-creation,
get_connection() with config-driven path.

Vector storage uses a companion .npz file alongside the DB for efficiency
(float16 to halve storage — ~73MB at 50K docs × 768-dim).
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.ir.sources.base import Document, Source
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    item_id      TEXT NOT NULL DEFAULT '',  -- source item (e.g. JSONL file path)
    fields       TEXT NOT NULL,   -- JSON dict of field_name -> text
    dense_text   TEXT NOT NULL,
    display_text TEXT NOT NULL DEFAULT '',
    metadata     TEXT NOT NULL DEFAULT '{}',
    indexed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS indexed_items (
    item_id    TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    mtime      REAL NOT NULL,    -- file mtime at index time
    doc_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_docs_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_docs_item ON documents(item_id);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _db_path(cfg: dict | None = None) -> Path:
    if cfg is None:
        from work_buddy.config import load_config
        cfg = load_config()
    ir_cfg = cfg.get("ir", {})
    explicit = ir_cfg.get("db_path")
    if explicit:
        return Path(explicit)
    return Path.home() / ".claude" / "projects" / "work_buddy_ir.db"


def _npz_path(cfg: dict | None = None, source: str | None = None) -> Path:
    """Companion .npz file for vector storage, per-source.

    Each source gets its own vector file (e.g., work_buddy_ir.conversation.npz).
    If source is None, returns the base path (for backward compat / status checks).
    """
    base = _db_path(cfg).with_suffix("")
    if source:
        return base.parent / f"{base.name}.{source}.npz"
    return base.with_suffix(".npz")


def get_connection(cfg: dict | None = None) -> sqlite3.Connection:
    """Open (or create) the IR database with WAL mode."""
    path = _db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Document CRUD
# ---------------------------------------------------------------------------

def upsert_documents(
    conn: sqlite3.Connection,
    docs: list[Document],
    item_id: str = "",
) -> int:
    """Insert or replace documents. Returns count inserted."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            d.doc_id,
            d.source,
            item_id,
            json.dumps(d.fields, ensure_ascii=False),
            d.dense_text,
            d.display_text,
            json.dumps(d.metadata, ensure_ascii=False),
            now,
        )
        for d in docs
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO documents "
        "(doc_id, source, item_id, fields, dense_text, display_text, metadata, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_documents(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    item_id: str | None = None,
    doc_id_prefix: str | None = None,
    metadata_filter: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Load documents from the store with optional filters.

    Args:
        source: Filter by source type (e.g. "conversation").
        item_id: Filter by exact item_id (e.g. a JSONL file path).
        doc_id_prefix: Filter by doc_id prefix (e.g. "session_uuid:" to get
            all spans from one session, or "tab_id:" for one tab).
        metadata_filter: Filter by metadata JSON fields. Dict of
            {key: value} matched via SQLite json_extract (case-insensitive).

    Returns dicts with fields/metadata already parsed from JSON.
    """
    clauses = []
    params: list[Any] = []

    if source:
        clauses.append("source = ?")
        params.append(source)
    if item_id:
        clauses.append("item_id = ?")
        params.append(item_id)
    if doc_id_prefix:
        clauses.append("doc_id LIKE ?")
        params.append(f"{doc_id_prefix}%")
    if metadata_filter:
        for key, value in metadata_filter.items():
            clauses.append("LOWER(json_extract(metadata, ?)) = LOWER(?)")
            params.extend([f"$.{key}", value])

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM documents{where} ORDER BY doc_id", params
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "doc_id": row["doc_id"],
            "source": row["source"],
            "fields": json.loads(row["fields"]),
            "dense_text": row["dense_text"],
            "display_text": row["display_text"],
            "metadata": json.loads(row["metadata"]),
            "indexed_at": row["indexed_at"],
        })
    return results


def get_indexed_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    """Return doc_ids already indexed for a given source."""
    rows = conn.execute(
        "SELECT doc_id FROM documents WHERE source = ?", (source,)
    ).fetchall()
    return {row["doc_id"] for row in rows}


def get_indexed_items(conn: sqlite3.Connection, source: str) -> dict[str, float]:
    """Return {item_id: mtime} for items already indexed for a source."""
    rows = conn.execute(
        "SELECT item_id, mtime FROM indexed_items WHERE source = ?", (source,)
    ).fetchall()
    return {row["item_id"]: row["mtime"] for row in rows}


def mark_item_indexed(
    conn: sqlite3.Connection,
    item_id: str,
    source: str,
    mtime: float,
    doc_count: int,
) -> None:
    """Record that an item has been indexed with its current mtime."""
    conn.execute(
        "INSERT OR REPLACE INTO indexed_items (item_id, source, mtime, doc_count) "
        "VALUES (?, ?, ?, ?)",
        (item_id, source, mtime, doc_count),
    )


def delete_item_docs(conn: sqlite3.Connection, item_id: str) -> int:
    """Delete all documents from a specific item (e.g., a changed session)."""
    cursor = conn.execute(
        "DELETE FROM documents WHERE item_id = ?", (item_id,)
    )
    conn.execute("DELETE FROM indexed_items WHERE item_id = ?", (item_id,))
    conn.commit()
    return cursor.rowcount


def doc_count(conn: sqlite3.Connection, source: str | None = None) -> int:
    """Count indexed documents, optionally by source."""
    if source:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM documents WHERE source = ?", (source,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as n FROM documents").fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM index_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# Vector storage
# ---------------------------------------------------------------------------

def save_vectors(
    vectors: "np.ndarray",
    doc_ids: list[str],
    cfg: dict | None = None,
    source: str | None = None,
) -> Path:
    """Save vectors to companion .npz file (float16 for storage efficiency)."""
    import numpy as np

    path = _npz_path(cfg, source=source)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        vectors=vectors.astype(np.float16),
        doc_ids=np.array(doc_ids, dtype=object),
    )
    logger.info("Saved %d vectors to %s (%.1fMB)", len(doc_ids), path,
                path.stat().st_size / 1024 / 1024)
    return path


def load_vectors(
    cfg: dict | None = None,
    source: str | None = None,
) -> tuple["np.ndarray", list[str]] | None:
    """Load vectors from companion .npz file. Returns None if not found."""
    import numpy as np

    path = _npz_path(cfg, source=source)
    if not path.exists():
        return None
    data = np.load(path, allow_pickle=True)
    vectors = data["vectors"].astype(np.float32)  # Upcast for computation
    doc_ids = data["doc_ids"].tolist()
    return vectors, doc_ids


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def _get_source(source_name: str) -> Source:
    """Import and instantiate a source adapter by name."""
    if source_name == "conversation":
        from work_buddy.ir.sources.conversations import ConversationSource
        return ConversationSource()
    if source_name == "chrome":
        from work_buddy.ir.sources.chrome import ChromeSource
        return ChromeSource()
    if source_name == "projects":
        from work_buddy.ir.sources.projects import ProjectsSource
        return ProjectsSource()
    if source_name == "docs":
        from work_buddy.ir.sources.docs import DocsSource
        return DocsSource()
    raise ValueError(f"Unknown source: {source_name}. Available: conversation, chrome, projects, docs")


def build_index(
    source: str = "conversation",
    days: int = 30,
    force: bool = False,
) -> dict[str, Any]:
    """Build or update the IR index for a given source.

    Uses file mtime to detect changes:
    - New items (not indexed before) → parse and insert
    - Changed items (mtime differs from last index) → delete old spans, re-parse
    - Unchanged items → skip entirely

    Args:
        source: Source adapter name (e.g. "conversation").
        days: Lookback window for discovering items.
        force: If True, drop existing docs for this source and rebuild.

    Returns:
        Stats dict with counts and timing.
    """
    from work_buddy.config import load_config
    cfg = load_config()

    t0 = time.time()
    adapter = _get_source(source)
    conn = get_connection(cfg)

    if force:
        conn.execute("DELETE FROM documents WHERE source = ?", (source,))
        conn.execute("DELETE FROM indexed_items WHERE source = ?", (source,))
        conn.commit()

    # Discover items with their mtimes
    discovered = adapter.discover(days=days)
    indexed_items = get_indexed_items(conn, source)

    items_new = 0
    items_changed = 0
    items_skipped = 0
    total_inserted = 0

    for item_id, mtime in discovered:
        prev_mtime = indexed_items.get(item_id)

        if prev_mtime is not None and abs(prev_mtime - mtime) < 0.001:
            # Unchanged — skip
            items_skipped += 1
            continue

        if prev_mtime is not None:
            # Changed — delete old spans for this item
            deleted = delete_item_docs(conn, item_id)
            items_changed += 1
            logger.debug("Re-indexing changed item %s (deleted %d old docs)", item_id, deleted)
        else:
            items_new += 1

        # Parse and insert
        docs = adapter.parse(item_id)
        if docs:
            inserted = upsert_documents(conn, docs, item_id=item_id)
            total_inserted += inserted

        mark_item_indexed(conn, item_id, source, mtime, len(docs))

    conn.commit()
    total = doc_count(conn, source)
    set_meta(conn, f"last_build:{source}", datetime.now(timezone.utc).isoformat())
    conn.close()

    build_time = time.time() - t0
    stats = {
        "source": source,
        "items_discovered": len(discovered),
        "items_new": items_new,
        "items_changed": items_changed,
        "items_skipped": items_skipped,
        "docs_inserted": total_inserted,
        "docs_total": total,
        "build_time_s": round(build_time, 1),
    }
    logger.info("Index build: %s", stats)
    return stats


def index_status(source: str | None = None) -> dict[str, Any]:
    """Report index health and stats."""
    from work_buddy.config import load_config
    cfg = load_config()

    db = _db_path(cfg)
    npz = _npz_path(cfg)

    if not db.exists():
        return {"status": "no_index", "db_path": str(db)}

    conn = get_connection(cfg)
    total = doc_count(conn)

    sources_info = {}
    for row in conn.execute("SELECT DISTINCT source FROM documents").fetchall():
        src = row["source"]
        count = doc_count(conn, src)
        last_build = get_meta(conn, f"last_build:{src}")
        sources_info[src] = {"doc_count": count, "last_build": last_build}

    conn.close()

    result: dict[str, Any] = {
        "status": "ok",
        "db_path": str(db),
        "total_docs": total,
        "sources": sources_info,
    }

    # Check for per-source vector files
    vectors_info = {}
    for src in sources_info:
        npz = _npz_path(cfg, source=src)
        if npz.exists():
            vdata = load_vectors(cfg, source=src)
            if vdata:
                vectors, doc_ids = vdata
                vectors_info[src] = {
                    "vector_file": str(npz),
                    "vector_file_mb": round(npz.stat().st_size / 1024 / 1024, 1),
                    "vector_count": len(doc_ids),
                    "vector_dims": vectors.shape[1],
                }
    if vectors_info:
        result["vectors"] = vectors_info

    return result


# ---------------------------------------------------------------------------
# RRF fusion (pure Python — safe to import from MCP server)
# ---------------------------------------------------------------------------

def rrf_fuse(
    rankings: list[dict[str, float]],
    k: int = 60,
) -> dict[str, float]:
    """Reciprocal Rank Fusion over multiple ranking lists.

    Args:
        rankings: List of {doc_id: score} dicts (one per retrieval method).
        k: RRF constant (default 60 per original paper).

    Returns:
        {doc_id: fused_score} dict.
    """
    fused: dict[str, float] = {}

    for ranking in rankings:
        if not ranking:
            continue
        # Sort by score descending to assign ranks
        sorted_ids = sorted(ranking, key=ranking.get, reverse=True)
        for rank, doc_id in enumerate(sorted_ids, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)

    return fused


# ---------------------------------------------------------------------------
# Substring search (pure Python — safe to import from MCP server)
# ---------------------------------------------------------------------------

def substring_search(
    query: str,
    *,
    source: str | None = None,
    scope: str | None = None,
    metadata_filter: dict[str, str] | None = None,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Source-agnostic case-insensitive substring search over the IR store.

    Searches display_text and all field values for occurrences of query.
    Score = match count, max-normalized to [0, 1].

    Args:
        query: Search term (case-insensitive substring match).
        source: Filter by source type (e.g. "conversation").
        scope: Filter by doc_id prefix.
        metadata_filter: Filter by metadata fields (passed to load_documents).
        top_k: Maximum results to return.

    Returns:
        List of result dicts matching engine.search() shape:
        {doc_id, score, source, display_text, metadata}.
    """
    conn = get_connection()
    docs = load_documents(conn, source=source, doc_id_prefix=scope,
                          metadata_filter=metadata_filter)
    conn.close()

    if not docs:
        return []

    query_lower = query.lower()
    scored: list[tuple[dict, int]] = []

    for doc in docs:
        # Build searchable text from display_text + all field values
        field_texts = " ".join(doc["fields"].values())
        searchable = f"{doc['display_text']} {field_texts}".lower()

        count = searchable.count(query_lower)
        if count > 0:
            scored.append((doc, count))

    if not scored:
        return []

    # Max-normalize counts to [0, 1]
    max_count = max(c for _, c in scored)
    scored.sort(key=lambda x: x[1], reverse=True)

    results = []
    for doc, count in scored[:top_k]:
        results.append({
            "doc_id": doc["doc_id"],
            "score": round(count / max_count, 4),
            "source": doc["source"],
            "display_text": doc["display_text"],
            "metadata": doc["metadata"],
        })

    return results
