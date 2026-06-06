"""SQLite chunk store for the vault semantic index.

Follows the ``messaging/models.py`` / ``ir/store.py`` pattern: WAL mode,
``_SCHEMA`` auto-creation, ``get_connection()`` with a config-driven path, and a
forward-only ``_migrate()``. The chunk store is a **derived, disposable cache** —
the Markdown files are the source of truth, so a lost or corrupt DB is rebuilt,
never recovered.

Two deliberate departures from ``ir/store.py``:

- **DB lives under ``data_root``** via ``paths.resolve("db/vault-index")`` — *not*
  the IR engine's out-of-``data_root`` ``~/.claude`` path. ``data_root`` defaults
  to the dot-prefixed ``.data`` so the DB never gets re-ingested by Obsidian or by
  this very indexer (Markdown-only). A ``vault_index.db_path`` config override
  exists for tests and relocation.
- **``doc_id`` is a hash of the identity tuple, not the human-readable key.** The
  ``Chunk.key`` string is a namespacing *aid* that a hostile heading could forge
  (see ``chunker.Chunk.key``); the primary key is a SHA-1 over the canonical
  ``(source_path, heading_path, dup_index, split_index)`` JSON, which cannot
  collide. The readable key is kept in the ``chunk_key`` column for diagnostics.

Dense vectors live HERE too, as float16 blobs in the ``chunk_vectors`` table
(FK-cascaded to ``chunks``; DESIGN §5) — *not* in a sidecar ``.npz`` (the IR engine's
mechanism, which DESIGN §12 says not to copy). The FTS5 lexical index (``chunks_fts``)
is part of this module as well. So this module is the durable chunk-row store, its
dense vectors, the lexical index, and the incremental-detection bookkeeping.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.vault_index.chunker import Chunk

logger = get_logger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS chunks (
    doc_id        TEXT PRIMARY KEY,   -- SHA-1 of the identity tuple (collision-safe)
    item_id       TEXT NOT NULL,      -- source file (incremental unit + per-item delete)
    vault_id      TEXT NOT NULL DEFAULT '',  -- which vault/root (populated by the source layer)
    source_path   TEXT NOT NULL,      -- the chunk's namespacing path
    chunk_key     TEXT NOT NULL,      -- human-readable Chunk.key (diagnostic, NOT the PK)
    heading_path  TEXT NOT NULL,      -- JSON list[str] breadcrumb
    text          TEXT NOT NULL,      -- raw section text (display + lexical)
    embed_input   TEXT NOT NULL,      -- breadcrumb-prefixed text for the embedder
    line_start    INTEGER NOT NULL,
    line_end      INTEGER NOT NULL,
    split_index   INTEGER NOT NULL DEFAULT 0,
    split_count   INTEGER NOT NULL DEFAULT 1,
    dup_index     INTEGER NOT NULL DEFAULT 0,
    indexed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS indexed_items (
    item_id     TEXT PRIMARY KEY,
    vault_id    TEXT NOT NULL DEFAULT '',
    mtime       REAL NOT NULL,         -- file mtime at index time
    size        INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    indexed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Dense vectors live HERE (DESIGN §5: "chunks + vec(blob)"), FK-cascaded to the
-- chunks table — NOT in a sidecar .npz (that's the IR engine's mechanism, which
-- DESIGN §12 says not to copy). One float16 blob per chunk → incremental writes
-- are O(1) per chunk instead of rewriting a monolithic vector file.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    doc_id  TEXT PRIMARY KEY REFERENCES chunks(doc_id) ON DELETE CASCADE,
    dim     INTEGER NOT NULL,
    vector  BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_item ON chunks(item_id);
CREATE INDEX IF NOT EXISTS idx_chunks_vault ON chunks(vault_id);
-- Covering index for the status aggregates: lets the per-vault COUNT and the
-- per-vault vector JOIN (GROUP BY vault_id, join on doc_id) run index-only,
-- without scanning the big text/embed_input columns. Keeps the index panel
-- sub-second on an 85k-chunk store.
CREATE INDEX IF NOT EXISTS idx_chunks_vault_doc ON chunks(vault_id, doc_id);

-- Lexical index over chunk text (external-content FTS5 — indexes `chunks` in place,
-- no duplication). Kept in sync by the triggers below + the indexer's explicit
-- delete-then-reinsert for changed files; `PRAGMA recursive_triggers=ON`
-- (get_connection) makes an INSERT OR REPLACE also fire the delete trigger so a
-- direct re-upsert can't leave a stale entry.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, embed_input, content='chunks', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, embed_input)
    VALUES (new.rowid, new.text, new.embed_input);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, embed_input)
    VALUES ('delete', old.rowid, old.text, old.embed_input);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, embed_input)
    VALUES ('delete', old.rowid, old.text, old.embed_input);
    INSERT INTO chunks_fts(rowid, text, embed_input)
    VALUES (new.rowid, new.text, new.embed_input);
END;
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _db_path(cfg: dict[str, Any] | None = None) -> Path:
    """Resolve the chunk-store DB path (under ``data_root`` by default)."""
    if cfg is None:
        from work_buddy.config import load_config
        cfg = load_config()
    custom = (cfg.get("vault_index", {}) or {}).get("db_path")
    if custom:
        from work_buddy.paths import repo_root
        p = Path(custom) if Path(custom).is_absolute() else repo_root() / custom
    else:
        from work_buddy.paths import resolve
        p = resolve("db/vault-index")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_connection(cfg: dict[str, Any] | None = None) -> sqlite3.Connection:
    """Open (or create) the chunk store with WAL mode."""
    path = _db_path(cfg)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Enforce the chunk_vectors -> chunks FK so a vector is auto-deleted when its
    # chunk is. Safe for the other (FK-less) tables. Must be set outside any
    # transaction, so before executescript.
    conn.execute("PRAGMA foreign_keys=ON")
    # So an INSERT OR REPLACE on `chunks` also fires the FTS delete trigger (and the
    # chunk_vectors cascade), keeping the FTS index from retaining a stale entry.
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only schema migrations.

    Backfills the FTS5 index for a DB whose ``chunks`` predate it (the triggers
    only fire on future writes, so existing rows must be indexed once). Guarded by
    an ``index_meta`` flag so the O(N) rebuild runs at most once.
    """
    if get_meta(conn, "fts_built") != "1":
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if n > 0:
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        set_meta(conn, "fts_built", "1")


# ---------------------------------------------------------------------------
# Identity + helpers
# ---------------------------------------------------------------------------

def chunk_doc_id(chunk: Chunk) -> str:
    """Stable collision-safe primary key for a chunk.

    SHA-1 over a canonical JSON encoding of ``(source_path, heading_path,
    dup_index, split_index)`` — the formal uniqueness tuple. JSON quoting makes
    the encoding unambiguous (unlike the ``#``-joined ``Chunk.key`` string), so
    a heading literally containing ``#`` / ``(1)`` / ``:`` cannot forge a clash.
    """
    canonical = json.dumps(
        [chunk.source_path, chunk.heading_path, chunk.dup_index, chunk.split_index],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["heading_path"] = json.loads(d["heading_path"])
    return d


def row_to_chunk(row: sqlite3.Row | dict[str, Any]) -> Chunk:
    """Reconstruct a :class:`Chunk` from a stored row.

    ``embed_input`` and ``key`` are recomputed from the chunk's fields (they are
    ``Chunk`` properties), which is exactly what lets a round-trip verify that
    the stored ``embed_input`` / ``chunk_key`` columns still match.
    """
    d = row if isinstance(row, dict) else _row_to_dict(row)
    return Chunk(
        source_path=d["source_path"],
        heading_path=list(d["heading_path"]),
        text=d["text"],
        line_start=d["line_start"],
        line_end=d["line_end"],
        split_index=d["split_index"],
        split_count=d["split_count"],
        dup_index=d["dup_index"],
    )


# ---------------------------------------------------------------------------
# Chunk CRUD
# ---------------------------------------------------------------------------

def upsert_chunks(
    conn: sqlite3.Connection,
    chunks: list[Chunk],
    item_id: str,
    vault_id: str = "",
) -> int:
    """Insert or replace a file's chunks. Returns the number written.

    Idempotent: a re-run of the same chunks replaces rows by ``doc_id`` rather
    than duplicating. Callers re-indexing a *changed* file should
    :func:`delete_item_chunks` first so stale chunks (e.g. a removed heading)
    don't linger.
    """
    now = _now_iso()
    rows = [
        (
            chunk_doc_id(c),
            item_id,
            vault_id,
            c.source_path,
            c.key,
            json.dumps(c.heading_path, ensure_ascii=False),
            c.text,
            c.embed_input,
            c.line_start,
            c.line_end,
            c.split_index,
            c.split_count,
            c.dup_index,
            now,
        )
        for c in chunks
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO chunks "
        "(doc_id, item_id, vault_id, source_path, chunk_key, heading_path, "
        " text, embed_input, line_start, line_end, split_index, split_count, "
        " dup_index, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_chunks(
    conn: sqlite3.Connection,
    *,
    item_id: str | None = None,
    vault_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load chunk rows, optionally filtered by item or vault.

    Returns dicts with ``heading_path`` parsed from JSON; ordered by ``doc_id``
    for deterministic output.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if item_id is not None:
        clauses.append("item_id = ?")
        params.append(item_id)
    if vault_id is not None:
        clauses.append("vault_id = ?")
        params.append(vault_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM chunks{where} ORDER BY doc_id", params
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_item_chunks(conn: sqlite3.Connection, item_id: str) -> int:
    """Delete a file's chunks and its ``indexed_items`` row. Returns rows deleted."""
    cur = conn.execute("DELETE FROM chunks WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM indexed_items WHERE item_id = ?", (item_id,))
    conn.commit()
    return cur.rowcount


def chunk_count(conn: sqlite3.Connection, vault_id: str | None = None) -> int:
    """Count stored chunks, optionally by vault."""
    if vault_id is not None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE vault_id = ?", (vault_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# Incremental-detection bookkeeping
# ---------------------------------------------------------------------------

def get_indexed_items(
    conn: sqlite3.Connection, vault_id: str | None = None
) -> dict[str, float]:
    """Return ``{item_id: mtime}`` for indexed files, optionally by vault."""
    if vault_id is not None:
        rows = conn.execute(
            "SELECT item_id, mtime FROM indexed_items WHERE vault_id = ?",
            (vault_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT item_id, mtime FROM indexed_items"
        ).fetchall()
    return {r["item_id"]: r["mtime"] for r in rows}


def mark_item_indexed(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    mtime: float,
    vault_id: str = "",
    size: int = 0,
    chunk_count: int = 0,
) -> None:
    """Record that a file was indexed at its current mtime/size."""
    conn.execute(
        "INSERT OR REPLACE INTO indexed_items "
        "(item_id, vault_id, mtime, size, chunk_count, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, vault_id, mtime, size, chunk_count, _now_iso()),
    )
    conn.commit()


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
# Dense vector storage (float16 blobs in the chunk_vectors table)
# ---------------------------------------------------------------------------
# Vectors are a regenerable cache derived from the chunks + Markdown. They live
# in SQLite (DESIGN §5: "chunks + vec(blob)"), one float16 blob per chunk,
# FK-cascaded to the chunks table — NOT a sidecar .npz (the IR engine's
# mechanism; DESIGN §12 says not to copy it). Per-chunk blobs make an
# incremental update O(1) (one row) instead of rewriting a monolithic vector
# file, and durability comes from SQLite's WAL rather than atomic .npz writes.


def upsert_vectors(
    conn: sqlite3.Connection,
    doc_ids: list[str],
    vectors: "np.ndarray",
) -> int:
    """Insert or replace one float16 blob per chunk. Returns rows written."""
    import numpy as np

    rows = [
        (doc_id, int(len(vec)), np.asarray(vec, dtype=np.float16).tobytes())
        for doc_id, vec in zip(doc_ids, vectors)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO chunk_vectors (doc_id, dim, vector) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def chunks_to_encode(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return ``(doc_id, embed_input)`` for chunks that have no vector yet.

    The incremental work-list — a LEFT JOIN yielding only chunks whose ``doc_id``
    is absent from ``chunk_vectors`` (with non-blank ``embed_input``), ordered by
    ``doc_id`` for deterministic batching.
    """
    rows = conn.execute(
        "SELECT c.doc_id, c.embed_input "
        "FROM chunks c LEFT JOIN chunk_vectors v ON c.doc_id = v.doc_id "
        "WHERE v.doc_id IS NULL AND TRIM(c.embed_input) != '' "
        "ORDER BY c.doc_id"
    ).fetchall()
    return [(r["doc_id"], r["embed_input"]) for r in rows]


def vector_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM chunk_vectors").fetchone()["n"]


def delete_all_vectors(conn: sqlite3.Connection) -> int:
    """Drop every vector (used by ``force`` rebuilds). Returns rows deleted."""
    cur = conn.execute("DELETE FROM chunk_vectors")
    conn.commit()
    return cur.rowcount


def load_all_vectors(
    conn: sqlite3.Connection,
) -> tuple["np.ndarray", list[str]] | None:
    """Load every vector into an ``(N, dim)`` float32 matrix + parallel doc_ids.

    Returns ``None`` when no vectors exist. Consumed by search, where the
    matrix is built once and cached.
    """
    import numpy as np

    rows = conn.execute(
        "SELECT doc_id, vector FROM chunk_vectors ORDER BY doc_id"
    ).fetchall()
    if not rows:
        return None
    doc_ids = [r["doc_id"] for r in rows]
    # One bulk frombuffer over the concatenated blobs (all same dim) is far faster
    # than a per-row frombuffer + np.stack at 80k+ rows — the cold matrix load.
    flat = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.float16)
    matrix = flat.reshape(len(doc_ids), -1).astype(np.float32)
    return matrix, doc_ids


# ---------------------------------------------------------------------------
# Lexical search (FTS5)
# ---------------------------------------------------------------------------

def search_lexical(
    conn: sqlite3.Connection,
    query: str,
    *,
    vault_id: str | None = None,
    top_k: int = 50,
) -> dict[str, float]:
    """Lexical FTS5 search → ``{doc_id: score}`` max-normalized to [0, 1].

    The query is reduced to bare word tokens, quoted and OR-combined, so arbitrary
    user input can't trigger an FTS5 ``MATCH`` syntax error. Higher score = better —
    the same shape the IR engine's BM25 produces, ready for ``rrf_fuse``. (RRF only
    uses the rank order; the [0,1] normalization is for the displayed ``bm25_score``.)
    """
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 1]
    if not terms:
        return {}
    match_expr = " OR ".join(f'"{t}"' for t in terms)

    sql = (
        "SELECT c.doc_id AS doc_id, bm25(chunks_fts) AS rank "
        "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ?"
    )
    params: list[Any] = [match_expr]
    if vault_id is not None:
        sql += " AND c.vault_id = ?"
        params.append(vault_id)
    sql += " ORDER BY rank LIMIT ?"  # bm25() ascending: more-negative = better
    params.append(top_k)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return {}
    # bm25() is negative (more negative = better); negate → higher = better.
    raw = {r["doc_id"]: -float(r["rank"]) for r in rows}
    hi = max(raw.values())
    if hi <= 0:
        return {d: 0.0 for d in raw}
    return {d: v / hi for d, v in raw.items()}
