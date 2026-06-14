"""IndexStore — the consolidated index's shared SQLite substrate.

One DB (``index.db_path``, default ``db/index-consolidated``) holding ALL partitions
(fork F-STORE: one shared DB, ``partition`` column). Generalizes
``vault_index/store.py`` to whole ``Document``s with named ``fields`` (BM25) +
``projections`` (dense), and lifts the IR engine's JSON ``metadata`` filtering.

Tables:
- ``documents`` — one row per doc (fields/projections/metadata as JSON; ``content_hash``
  for change detection; ``timestamp`` for recency).
- ``doc_fts`` — a STANDALONE FTS5 index over canonical ``(title, body, tags)`` text
  (not external-content: avoids rowid/trigger coupling and lets us delete by ``doc_id``).
  Per-field importance is preserved via FTS5's native ``bm25(doc_fts, wt, wb, wg)``
  column weights. Partition/metadata scoping is a JOIN to ``documents``.
- ``doc_vectors`` — one float16 blob per ``(doc_id, projection)``, FK-cascaded to
  ``documents`` (incremental O(1) writes; vectors auto-deleted with their doc).
- ``indexed_items`` — the change-detection ledger (mtime + content_hash per item).
- ``index_meta`` — KV (per-partition ``build_version`` etc.).

Thread-safety: a fresh connection per public method (sqlite3 connections aren't
shareable across threads; the embedding service is multi-threaded). ``CREATE … IF NOT
EXISTS`` runs each open — cheap and idempotent, matching ``vault_index/store.py``.

Write contention: SQLite allows ONE writer per DB. Builds serialize on a DB-wide
advisory gate (``build.py``), but writers can still live in different processes
(sidecar jobs, the embedding service, the CLI), so every write method additionally
rides through transient ``database is locked`` with a bounded backoff-retry. Each
write is a small self-contained connect→commit→close transaction, which is what
makes the retry safe (idempotent INSERT OR REPLACE / DELETE).
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.index.model import Document, Projection
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    partition     TEXT NOT NULL,
    item_id       TEXT NOT NULL DEFAULT '',
    fields        TEXT NOT NULL DEFAULT '{}',   -- JSON {name: text}
    projections   TEXT NOT NULL DEFAULT '{}',   -- JSON {key: {"text": str | list[str]}}
    display_text  TEXT NOT NULL DEFAULT '',
    metadata      TEXT NOT NULL DEFAULT '{}',   -- JSON, json_extract-filterable
    content_hash  TEXT NOT NULL DEFAULT '',
    timestamp     REAL,                          -- epoch seconds, nullable (recency)
    indexed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_partition ON documents(partition);
CREATE INDEX IF NOT EXISTS idx_documents_item ON documents(item_id);

-- One row per (doc_id, projection, sub). ``sub`` lets a single projection carry
-- MULTIPLE vectors per doc (e.g. one per alias) which are pooled (max/mean) at query
-- time — preserving the knowledge alias signal. Scalar projections use sub=0 only.
CREATE TABLE IF NOT EXISTS doc_vectors (
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    projection  TEXT NOT NULL,
    sub         INTEGER NOT NULL DEFAULT 0,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    PRIMARY KEY (doc_id, projection, sub)
);
CREATE INDEX IF NOT EXISTS idx_doc_vectors_proj ON doc_vectors(projection);

CREATE TABLE IF NOT EXISTS indexed_items (
    item_id       TEXT NOT NULL,
    partition     TEXT NOT NULL DEFAULT '',
    mtime         REAL NOT NULL DEFAULT 0,
    content_hash  TEXT NOT NULL DEFAULT '',
    doc_count     INTEGER NOT NULL DEFAULT 0,
    indexed_at    TEXT NOT NULL,
    PRIMARY KEY (item_id, partition)
);

CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Standalone FTS5 (NOT external-content): canonical title/body/tags columns +
-- an UNINDEXED doc_id so we can delete by id and JOIN back to documents.
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    doc_id UNINDEXED, title, body, tags
);
"""

# Default FTS5 bm25() column weights (title > tags > body), overridable per partition.
_DEFAULT_FTS_WEIGHTS = (3.0, 1.0, 2.0)  # (title, body, tags)

# Excludes retained-but-source-gone docs (retention "retain"/"ttl"). A NULL or non-
# "orphaned" lifecycle_state is a live doc. Appended to a query whose alias for the
# documents table is ``d``.
_ORPHAN_EXCLUSION_SQL = (
    " AND (json_extract(d.metadata, '$.lifecycle_state') IS NULL "
    "OR json_extract(d.metadata, '$.lifecycle_state') != 'orphaned')"
)

# How long one connection waits on a held write lock before sqlite raises
# ``database is locked`` (a large item — e.g. a many-thousand-span conversation
# session — can legitimately hold the writer for seconds).
_BUSY_TIMEOUT_S = 30.0

# Backoff between write attempts after a locked error. Combined with the busy
# timeout this rides out a concurrent writer's longest single transaction
# instead of killing a multi-hour build over one collision.
_WRITE_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0)


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _write_retry(fn):
    """Retry a write method through transient lock contention.

    Safe because every write method is one self-contained connect→commit→close
    transaction of idempotent statements — a failed attempt leaves nothing behind
    and a repeat converges to the same rows.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        last: sqlite3.OperationalError | None = None
        for attempt, delay in enumerate((0.0, *_WRITE_RETRY_DELAYS_S)):
            if delay:
                time.sleep(delay)
            try:
                return fn(self, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc):
                    raise
                last = exc
                logger.warning(
                    "index store: %s contended (%s); attempt %d/%d",
                    fn.__name__, exc, attempt + 1, 1 + len(_WRITE_RETRY_DELAYS_S),
                )
        assert last is not None
        raise last
    return wrapper


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fts_columns(fields: dict[str, str]) -> tuple[str, str, str]:
    """Map a Document's generic ``fields`` into canonical (title, body, tags) text.

    Convention: ``title``/``name`` → title; ``tags`` → tags; every other field value
    → body. Partition-agnostic — partitions just use conventional field keys.
    """
    title = (fields.get("title") or fields.get("name") or "").strip()
    tags = (fields.get("tags") or "").strip()
    body = " ".join(
        str(v) for k, v in fields.items()
        if k not in ("title", "name", "tags") and v
    ).strip()
    return title, body, tags


def _fts_match_expr(query: str) -> str | None:
    """Reduce arbitrary user text to a safe FTS5 MATCH expr (OR of quoted tokens)."""
    terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def _metadata_where(filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """Build a SQL fragment + params for json_extract metadata filtering.

    Scalar value → equality; list value → set-membership (IN). Keys are the
    metadata dict keys. Returns ``("", [])`` for no filters.
    """
    if not filters:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    for key, val in filters.items():
        path = f"$.{key}"
        if isinstance(val, (list, tuple, set)):
            vals = list(val)
            if not vals:
                clauses.append("0")  # empty set matches nothing
                continue
            placeholders = ",".join("?" * len(vals))
            clauses.append(f"json_extract(d.metadata, ?) IN ({placeholders})")
            params.append(path)
            params.extend(str(v) for v in vals)
        else:
            clauses.append("json_extract(d.metadata, ?) = ?")
            params.append(path)
            params.append(str(val))
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


class IndexStore:
    """Connection-per-operation SQLite store for the consolidated index."""

    def __init__(
        self, db_path: str | Path, *, busy_timeout_s: float = _BUSY_TIMEOUT_S
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_s = busy_timeout_s

    @property
    def db_path(self) -> Path:
        return self._db_path

    # -- connection -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=self._busy_timeout_s)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        return conn

    # -- writes -----------------------------------------------------------
    @_write_retry
    def upsert_documents(self, docs: list[Document], item_id: str = "") -> int:
        """Insert/replace documents (tagged with ``item_id``) and refresh FTS rows.

        ``item_id`` is the source item the docs came from (``parse(item_id)``), used
        for per-item deletes. Returns the number of docs written.
        """
        if not docs:
            return 0
        conn = self._connect()
        try:
            now = _now_iso()
            for d in docs:
                d.ensure_hash()
                conn.execute(
                    "INSERT OR REPLACE INTO documents "
                    "(doc_id, partition, item_id, fields, projections, display_text, "
                    " metadata, content_hash, timestamp, indexed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        d.doc_id, d.partition, item_id,
                        json.dumps(d.fields),
                        json.dumps({k: {"text": p.text} for k, p in d.projections.items()}),
                        d.display_text, json.dumps(d.metadata), d.content_hash,
                        d.timestamp, now,
                    ),
                )
                # refresh FTS (standalone → manage explicitly)
                conn.execute("DELETE FROM doc_fts WHERE doc_id = ?", (d.doc_id,))
                title, body, tags = _fts_columns(d.fields)
                conn.execute(
                    "INSERT INTO doc_fts (doc_id, title, body, tags) VALUES (?,?,?,?)",
                    (d.doc_id, title, body, tags),
                )
            conn.commit()
            return len(docs)
        finally:
            conn.close()

    @_write_retry
    def upsert_vectors(
        self, projection: str, rows: list[tuple[str, "Any"]]
    ) -> int:
        """Insert/replace vectors for a projection. Returns rows written.

        Each item is ``(doc_id, vecs)`` where ``vecs`` is a 1-D vector (scalar
        projection → one sub) or a 2-D array / list-of-vectors (pooled projection →
        one sub per row). Existing rows for each ``(doc_id, projection)`` are replaced.
        """
        if not rows:
            return 0
        import numpy as np
        conn = self._connect()
        try:
            written = 0
            for doc_id, vecs in rows:
                arr = np.asarray(vecs, dtype=np.float16)
                if arr.ndim == 1:
                    arr = arr.reshape(1, -1)
                conn.execute(
                    "DELETE FROM doc_vectors WHERE doc_id = ? AND projection = ?",
                    (doc_id, projection),
                )
                payload = [
                    (doc_id, projection, i, int(arr.shape[1]), arr[i].tobytes())
                    for i in range(arr.shape[0])
                ]
                conn.executemany(
                    "INSERT INTO doc_vectors (doc_id, projection, sub, dim, vector) "
                    "VALUES (?,?,?,?,?)",
                    payload,
                )
                written += len(payload)
            conn.commit()
            return written
        finally:
            conn.close()

    @_write_retry
    def delete_item_docs(self, item_id: str, partition: str | None = None) -> int:
        """Delete all docs for an item (FTS rows + cascaded vectors). Returns rows."""
        conn = self._connect()
        try:
            sql = "SELECT doc_id FROM documents WHERE item_id = ?"
            args: list[Any] = [item_id]
            if partition is not None:
                sql += " AND partition = ?"
                args.append(partition)
            doc_ids = [r["doc_id"] for r in conn.execute(sql, args).fetchall()]
            for did in doc_ids:
                conn.execute("DELETE FROM doc_fts WHERE doc_id = ?", (did,))
            cur = conn.execute(
                "DELETE FROM documents WHERE item_id = ?"
                + (" AND partition = ?" if partition is not None else ""),
                args,
            )
            if partition is not None:
                conn.execute(
                    "DELETE FROM indexed_items WHERE item_id = ? AND partition = ?",
                    (item_id, partition),
                )
            else:
                conn.execute("DELETE FROM indexed_items WHERE item_id = ?", (item_id,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    @_write_retry
    def delete_partition(self, partition: str) -> int:
        """Drop an entire partition (FTS + docs + cascaded vectors + ledger)."""
        conn = self._connect()
        try:
            doc_ids = [
                r["doc_id"] for r in conn.execute(
                    "SELECT doc_id FROM documents WHERE partition = ?", (partition,)
                ).fetchall()
            ]
            for did in doc_ids:
                conn.execute("DELETE FROM doc_fts WHERE doc_id = ?", (did,))
            cur = conn.execute("DELETE FROM documents WHERE partition = ?", (partition,))
            conn.execute("DELETE FROM indexed_items WHERE partition = ?", (partition,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # -- retention -------------------------------------------------------
    @_write_retry
    def mark_items_orphaned(self, item_ids: list[str], partition: str) -> int:
        """Retain items whose source dropped them: stamp ``lifecycle_state="orphaned"`` on
        their docs and FORGET the change-ledger entry. Docs (FTS + vectors) stay searchable;
        forgetting the ledger means the next build won't re-detect them as "deleted" (no
        re-stamp churn), and if the source later restores an item it re-indexes fresh.
        Returns the number of items affected."""
        if not item_ids:
            return 0
        conn = self._connect()
        try:
            n = 0
            for iid in item_ids:
                conn.execute(
                    "UPDATE documents SET metadata = "
                    "json_set(metadata, '$.lifecycle_state', 'orphaned') "
                    "WHERE item_id = ? AND partition = ?",
                    (iid, partition),
                )
                conn.execute(
                    "DELETE FROM indexed_items WHERE item_id = ? AND partition = ?",
                    (iid, partition),
                )
                n += 1
            conn.commit()
            return n
        finally:
            conn.close()

    @_write_retry
    def prune_orphans_older_than(self, partition: str, cutoff_ts: float) -> int:
        """TTL sweep: delete orphaned docs whose ``timestamp`` is older than ``cutoff_ts``
        (FTS rows cleared explicitly; vectors FK-cascade). Bounds orphan growth under the
        ``ttl`` retention mode. Returns docs deleted."""
        conn = self._connect()
        try:
            where = (
                "partition = ? "
                "AND json_extract(metadata, '$.lifecycle_state') = 'orphaned' "
                "AND COALESCE(timestamp, 0) < ?"
            )
            doc_ids = [
                r["doc_id"] for r in conn.execute(
                    f"SELECT doc_id FROM documents WHERE {where}", (partition, cutoff_ts)
                ).fetchall()
            ]
            for did in doc_ids:
                conn.execute("DELETE FROM doc_fts WHERE doc_id = ?", (did,))
            cur = conn.execute(
                f"DELETE FROM documents WHERE {where}", (partition, cutoff_ts)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # -- change-detection ledger -----------------------------------------
    def get_indexed_items(self, partition: str) -> dict[str, tuple[float, str]]:
        """``{item_id: (mtime, content_hash)}`` for a partition."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT item_id, mtime, content_hash FROM indexed_items WHERE partition = ?",
                (partition,),
            ).fetchall()
            return {r["item_id"]: (r["mtime"], r["content_hash"]) for r in rows}
        finally:
            conn.close()

    @_write_retry
    def mark_item_indexed(
        self, item_id: str, partition: str, *, mtime: float = 0.0,
        content_hash: str = "", doc_count: int = 0,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO indexed_items "
                "(item_id, partition, mtime, content_hash, doc_count, indexed_at) "
                "VALUES (?,?,?,?,?,?)",
                (item_id, partition, mtime, content_hash, doc_count, _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    # -- meta + versioning ------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    @_write_retry
    def set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def build_version(self, partition: str) -> int:
        v = self.get_meta(f"build_version:{partition}")
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def bump_version(self, partition: str) -> int:
        nxt = self.build_version(partition) + 1
        self.set_meta(f"build_version:{partition}", str(nxt))
        return nxt

    # -- reads ------------------------------------------------------------
    def search_lexical(
        self, query: str, *, partition: str | None = None,
        filters: dict[str, Any] | None = None, scope: str | None = None,
        weights: tuple[float, float, float] | None = None, top_k: int = 50,
        exclude_orphaned: bool = False,
    ) -> dict[str, float]:
        """FTS5 bm25 lexical search → ``{doc_id: score}`` max-normalized to [0,1].

        Higher score = better (same shape as the IR engine's BM25 → ready for RRF).
        ``weights`` are the (title, body, tags) bm25 column weights; ``None`` uses
        ``_DEFAULT_FTS_WEIGHTS``. ``exclude_orphaned`` drops retained-but-source-gone
        docs (a live-only view).
        """
        match = _fts_match_expr(query)
        if match is None:
            return {}
        wt, wb, wg = weights or _DEFAULT_FTS_WEIGHTS
        meta_sql, meta_params = _metadata_where(filters)
        conn = self._connect()
        try:
            # bm25() takes one weight per FTS column IN ORDER, including the leading
            # UNINDEXED doc_id (col 0) — so a 0.0 placeholder precedes title/body/tags.
            sql = (
                f"SELECT f.doc_id AS doc_id, bm25(doc_fts, 0.0, {wt}, {wb}, {wg}) AS rank "
                "FROM doc_fts f JOIN documents d ON d.doc_id = f.doc_id "
                "WHERE doc_fts MATCH ?"
            )
            params: list[Any] = [match]
            if partition is not None:
                sql += " AND d.partition = ?"
                params.append(partition)
            if scope:
                sql += " AND d.doc_id LIKE ?"
                params.append(scope + "%")
            sql += meta_sql
            params.extend(meta_params)
            if exclude_orphaned:
                sql += _ORPHAN_EXCLUSION_SQL
            sql += " ORDER BY rank LIMIT ?"  # bm25 ascending: more-negative = better
            params.append(top_k)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        if not rows:
            return {}
        raw = {r["doc_id"]: -float(r["rank"]) for r in rows}  # negate → higher=better
        hi = max(raw.values())
        if hi <= 0:
            return {d: 0.0 for d in raw}
        return {d: v / hi for d, v in raw.items()}

    def load_documents(
        self, *, partition: str | None = None, doc_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None, scope: str | None = None,
        exclude_orphaned: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Load documents (parsed JSON) keyed by doc_id, with optional filters.

        ``exclude_orphaned`` drops retained-but-source-gone docs (a live-only view) — used
        to build the dense allow-set for a live-only search."""
        meta_sql, meta_params = _metadata_where(filters)
        conn = self._connect()
        try:
            sql = (
                "SELECT d.doc_id, d.partition, d.item_id, d.fields, d.projections, "
                "d.display_text, d.metadata, d.content_hash, d.timestamp "
                "FROM documents d WHERE 1=1"
            )
            params: list[Any] = []
            if partition is not None:
                sql += " AND d.partition = ?"
                params.append(partition)
            if doc_ids is not None:
                if not doc_ids:
                    return {}
                sql += f" AND d.doc_id IN ({','.join('?' * len(doc_ids))})"
                params.extend(doc_ids)
            if scope:
                sql += " AND d.doc_id LIKE ?"
                params.append(scope + "%")
            sql += meta_sql
            params.extend(meta_params)
            if exclude_orphaned:
                sql += _ORPHAN_EXCLUSION_SQL
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            out[r["doc_id"]] = {
                "doc_id": r["doc_id"],
                "partition": r["partition"],
                "item_id": r["item_id"],
                "fields": json.loads(r["fields"] or "{}"),
                "projections": json.loads(r["projections"] or "{}"),
                "display_text": r["display_text"] or "",
                "metadata": json.loads(r["metadata"] or "{}"),
                "content_hash": r["content_hash"] or "",
                "timestamp": r["timestamp"],
            }
        return out

    def load_all_vectors(
        self, partition: str, projection: str
    ) -> "tuple[Any, list[str]] | None":
        """Load every vector for (partition, projection) → (matrix, doc_ids) | None.

        For pooled projections ``doc_ids`` repeats (one entry per sub-vector, parallel
        to the matrix rows); the caller max/mean-pools per doc_id at query time.
        """
        import numpy as np
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT v.doc_id AS doc_id, v.vector AS vector "
                "FROM doc_vectors v JOIN documents d ON d.doc_id = v.doc_id "
                "WHERE v.projection = ? AND d.partition = ? ORDER BY v.doc_id, v.sub",
                (projection, partition),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        doc_ids = [r["doc_id"] for r in rows]
        flat = np.frombuffer(b"".join(r["vector"] for r in rows), dtype=np.float16)
        matrix = flat.reshape(len(doc_ids), -1).astype(np.float32)
        return matrix, doc_ids

    def docs_missing_vectors(
        self, partition: str, projection: str
    ) -> list[tuple[str, Any]]:
        """``(doc_id, projection_text)`` for docs in the partition lacking a vector.

        The incremental/resumable encode work-list. ``projection_text`` is read from
        the document's stored ``projections`` JSON (scalar or list).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT d.doc_id AS doc_id, d.projections AS projections "
                "FROM documents d "
                "LEFT JOIN doc_vectors v ON v.doc_id = d.doc_id AND v.projection = ? "
                "WHERE d.partition = ? AND v.doc_id IS NULL ORDER BY d.doc_id",
                (projection, partition),
            ).fetchall()
        finally:
            conn.close()
        out: list[tuple[str, Any]] = []
        for r in rows:
            projs = json.loads(r["projections"] or "{}")
            entry = projs.get(projection)
            if entry is None:
                continue
            text = entry.get("text") if isinstance(entry, dict) else entry
            if text:
                out.append((r["doc_id"], text))
        return out

    # -- counts -----------------------------------------------------------
    def doc_count(self, partition: str | None = None) -> int:
        conn = self._connect()
        try:
            if partition is None:
                return conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            return conn.execute(
                "SELECT COUNT(*) AS n FROM documents WHERE partition = ?", (partition,)
            ).fetchone()["n"]
        finally:
            conn.close()

    def vector_count(self, partition: str, projection: str | None = None) -> int:
        """Count DISTINCT docs with ≥1 vector (pooled projections have many rows/doc)."""
        conn = self._connect()
        try:
            if projection is None:
                return conn.execute(
                    "SELECT COUNT(DISTINCT v.doc_id) AS n FROM doc_vectors v "
                    "JOIN documents d ON d.doc_id = v.doc_id WHERE d.partition = ?",
                    (partition,),
                ).fetchone()["n"]
            return conn.execute(
                "SELECT COUNT(DISTINCT v.doc_id) AS n FROM doc_vectors v "
                "JOIN documents d ON d.doc_id = v.doc_id "
                "WHERE d.partition = ? AND v.projection = ?",
                (partition, projection),
            ).fetchone()["n"]
        finally:
            conn.close()

    def partitions(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT partition FROM documents ORDER BY partition"
            ).fetchall()
            return [r["partition"] for r in rows]
        finally:
            conn.close()
