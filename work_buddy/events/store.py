"""``EventStore`` — the durable SQLite spine (log + dedup + offsets + DLQ).

The only thing that touches ``db/events``. Three tables (created idempotently
at connect time, the house pattern — there is no central migration ladder):

* ``events``           — the append-only log; ``UNIQUE(source, id)`` is the
                         inbox (the insert *is* the dedup claim). Carries a
                         per-row ``expires_at`` (computed at append from a
                         per-type TTL) so the artifact lifecycle can reap it.
* ``consumer_offsets`` — one watermark per durable consumer (restart replay).
* ``event_dlq``        — poison events, per consumer (skip-and-record).

Thread-safety: a fresh connection per call (WAL + ``busy_timeout``) so the
``event-drain`` thread and producers can hit the DB concurrently. Mirrors
``work_buddy/llm/queue.py`` / ``work_buddy/threads/work_item_events.py``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from work_buddy.events.envelope import Event


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    """Resolve the events DB path. Tests monkeypatch this."""
    from work_buddy.paths import resolve

    return resolve("db/events")


# --- Per-type retention ----------------------------------------------------
# ``expires_at`` is computed at append time from this map, so a noisy internal
# type self-reaps fast while meaningful domain events linger. The artifact's
# retention predicate (see artifact.py) still pins undelivered / DLQ / (external)
# in-dedup-window rows regardless of this TTL.
_DEFAULT_TTL_DAYS = 14.0
_TTL_DAYS_BY_TYPE: dict[str, float] = {
    "ai.workbuddy.schedule.tick": 3.0 / 24.0,  # ~3h — high-volume, self-reaps
}


def _expires_at_for(event_type: str, received_at_iso: str) -> str:
    days = _TTL_DAYS_BY_TYPE.get(event_type, _DEFAULT_TTL_DAYS)
    base = datetime.fromisoformat(received_at_iso)
    return (base + timedelta(days=days)).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- total order (single writer)
  id           TEXT NOT NULL,
  source       TEXT NOT NULL,
  type         TEXT NOT NULL,
  subject      TEXT,
  time         TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  modality     TEXT NOT NULL,
  dedup_key    TEXT NOT NULL,
  traceparent  TEXT,
  expires_at   TEXT,
  data         TEXT NOT NULL,             -- JSON
  ext          TEXT NOT NULL DEFAULT '{}',-- JSON (non-promoted envelope fields)
  UNIQUE(source, id)                      -- the inbox: insert IS the dedup claim
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_dedup ON events(dedup_key);

CREATE TABLE IF NOT EXISTS consumer_offsets (
  consumer_id  TEXT PRIMARY KEY,
  last_seq     INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_dlq (
  seq          INTEGER NOT NULL,
  consumer_id  TEXT NOT NULL,
  attempts     INTEGER NOT NULL,
  last_error   TEXT,
  dead_at      TEXT NOT NULL,
  PRIMARY KEY (seq, consumer_id)
);
"""


def _to_row(event: Event, received_at: str, expires_at: str) -> dict[str, object]:
    # Promoted columns drive routing/dedup; the rest of the envelope rides in
    # the ``ext`` JSON blob so the row round-trips a full Event.
    ext_blob: dict[str, object] = {
        "specversion": event.specversion,
        "datacontenttype": event.datacontenttype,
        "dataschema": event.dataschema,
        "idempotency_key": event.idempotency_key,
        "wb_session": event.wb_session,
        "workflow_run_id": event.workflow_run_id,
    }
    ext_blob.update(event.ext or {})
    return {
        "id": event.id,
        "source": event.source,
        "type": event.type,
        "subject": event.subject,
        "time": event.time,
        "received_at": received_at,
        "modality": event.modality,
        "dedup_key": event.dedup_key,
        "traceparent": event.traceparent,
        "expires_at": expires_at,
        "data": json.dumps(event.data, default=str),
        "ext": json.dumps(ext_blob, default=str),
    }


def _from_row(row: sqlite3.Row) -> tuple[int, Event]:
    d = dict(row)
    ext = json.loads(d.get("ext") or "{}")
    seq = int(d["seq"])
    evt = Event(
        id=d["id"],
        source=d["source"],
        type=d["type"],
        data=json.loads(d.get("data") or "{}"),
        time=d["time"],
        subject=d.get("subject"),
        modality=d.get("modality") or "internal",
        dedup_key=d.get("dedup_key"),
        traceparent=d.get("traceparent"),
        specversion=ext.pop("specversion", "1.0") or "1.0",
        datacontenttype=ext.pop("datacontenttype", "application/json")
        or "application/json",
        dataschema=ext.pop("dataschema", None),
        idempotency_key=ext.pop("idempotency_key", None),
        wb_session=ext.pop("wb_session", None),
        workflow_run_id=ext.pop("workflow_run_id", None),
        ext=ext,
    )
    return seq, evt


class EventStore:
    """All DB access for the spine. Stateless beyond the DB path."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._explicit_path = db_path

    def _path(self) -> Path:
        return self._explicit_path if self._explicit_path is not None else _db_path()

    def _conn(self) -> sqlite3.Connection:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # cheap appends, safe under WAL
        conn.executescript(_SCHEMA)
        return conn

    # ----------------------------------------------------------- the log

    def append(self, event: Event) -> int | None:
        """Append a durable event. Returns its ``seq``, or ``None`` if a row
        with the same ``(source, id)`` already exists (the dedup claim)."""
        received = _now_iso()
        row = _to_row(event, received, _expires_at_for(event.type, received))
        conn = self._conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events "
                "(id, source, type, subject, time, received_at, modality, "
                " dedup_key, traceparent, expires_at, data, ext) "
                "VALUES (:id,:source,:type,:subject,:time,:received_at,:modality,"
                ":dedup_key,:traceparent,:expires_at,:data,:ext)",
                row,
            )
            conn.commit()
            if cur.rowcount == 0:
                return None  # (source, id) duplicate
            return int(cur.lastrowid)
        finally:
            conn.close()

    def read_since(self, last_seq: int, limit: int = 200) -> list[tuple[int, Event]]:
        """Return up to ``limit`` events with ``seq > last_seq``, ascending."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                (last_seq, limit),
            ).fetchall()
        finally:
            conn.close()
        return [_from_row(r) for r in rows]

    # ------------------------------------------------------- offsets

    def get_offset(self, consumer_id: str) -> int:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT last_seq FROM consumer_offsets WHERE consumer_id = ?",
                (consumer_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row["last_seq"]) if row else 0

    def ensure_offset(self, consumer_id: str, default: int = 0) -> None:
        """Create the offset row if absent — so a freshly-registered consumer
        pins the log (retention must not reap ahead of it) before it delivers."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO consumer_offsets "
                "(consumer_id, last_seq, updated_at) VALUES (?, ?, ?)",
                (consumer_id, default, _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    def commit_offset(self, consumer_id: str, seq: int) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO consumer_offsets (consumer_id, last_seq, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(consumer_id) DO UPDATE SET "
                "last_seq = excluded.last_seq, updated_at = excluded.updated_at",
                (consumer_id, seq, _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    def min_live_offset(self) -> int:
        """Lowest committed offset across consumers — the high-water mark
        below which every consumer has been delivered. With **no** registered
        consumers, returns ``sys.maxsize`` so retention pins nothing."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT MIN(last_seq) AS m, COUNT(*) AS c FROM consumer_offsets"
            ).fetchone()
        finally:
            conn.close()
        if not row or row["c"] == 0:
            return sys.maxsize
        return int(row["m"])

    # ------------------------------------------------------- DLQ

    def dead_letter(self, seq: int, consumer_id: str, attempts: int, err: str) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO event_dlq "
                "(seq, consumer_id, attempts, last_error, dead_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (seq, consumer_id, attempts, (err or "")[:2000], _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    def dlq_seqs(self) -> set[int]:
        """The set of seqs with a live DLQ row (for the retention predicate)."""
        conn = self._conn()
        try:
            rows = conn.execute("SELECT DISTINCT seq FROM event_dlq").fetchall()
        finally:
            conn.close()
        return {int(r["seq"]) for r in rows}

    # ------------------------------------------------------- introspection

    def max_seq(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT MAX(seq) AS m FROM events").fetchone()
        finally:
            conn.close()
        return int(row["m"]) if row and row["m"] is not None else 0

    def count(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()
        finally:
            conn.close()
        return int(row["c"]) if row else 0
