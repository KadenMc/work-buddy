"""``SqliteLwwLog`` — the SQLite-backed :class:`LwwLog`.

Persists write-event provenance to an ``lww_meta`` table living *inside*
the entity's own database. Co-locating the sidecar with the entity DB
means it travels with backups + restores (the
``architecture/backups`` tarball already snapshots each vital DB) with
no extra wiring.

## Append-only

The table is genuinely append-only — every :meth:`record` call is an
``INSERT``; nothing is ever updated or deleted. An autoincrement ``id``
is the primary key (not a natural ``(table, pk, field, ts, surface)``
key) so two events that happen to share a microsecond timestamp cannot
collide. LWW reads take the newest row; a future CRDT resolver can
replay the whole ordered history.

## Schema

::

    CREATE TABLE lww_meta (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name    TEXT NOT NULL,   -- the entity table (e.g. 'projects')
        row_pk        TEXT NOT NULL,   -- entity primary key
        field         TEXT NOT NULL,   -- logical field name
        ts            TEXT NOT NULL,   -- ISO-8601 UTC write time
        actor         TEXT NOT NULL,   -- JSON array; OR-set of actor candidates
        process       TEXT NOT NULL,   -- 'mutation' | 'drift' | 'materialize' | …
        from_surface  TEXT,            -- nullable
        to_surface    TEXT NOT NULL    -- which surface this row records
    )

``actor`` is a JSON array so the OR-set semantics survive the round
trip; an empty array is the honest "fully unknown" value.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Callable

from work_buddy.logging_config import get_logger
from work_buddy.markdown_db.lww import LwwEntry
from work_buddy.markdown_db.types import Surface, WriteProvenance

logger = get_logger(__name__)


# DDL shared by SqliteLwwLog's defensive ensure-exists AND the per-DB
# migration steps (tasks v10, the projects schema). Keeping one literal
# means the table is identical however it gets created.
LWW_META_DDL = """\
CREATE TABLE IF NOT EXISTS lww_meta (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name    TEXT NOT NULL,
    row_pk        TEXT NOT NULL,
    field         TEXT NOT NULL,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT '[]',
    process       TEXT NOT NULL,
    from_surface  TEXT,
    to_surface    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lww_meta_latest
    ON lww_meta(table_name, row_pk, field, to_surface, ts);
"""


def ensure_lww_meta(conn: sqlite3.Connection) -> None:
    """Create the ``lww_meta`` table + index if absent. Idempotent."""
    conn.executescript(LWW_META_DDL)


class SqliteLwwLog:
    """A :class:`LwwLog` persisted to an ``lww_meta`` table.

    Construct with a ``connection_factory`` — a zero-arg callable
    returning a fresh :class:`sqlite3.Connection` to the entity's
    database (e.g. ``work_buddy.projects.store.get_connection``). Each
    operation opens, uses, and closes its own connection, matching the
    short-lived-connection discipline of the work-buddy stores.
    """

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connection_factory
        # Defensive: ensure the table exists even if the owning DB's
        # migration ladder has not been extended yet (e.g. in tests).
        conn = self._connect()
        try:
            ensure_lww_meta(conn)
            conn.commit()
        finally:
            conn.close()

    def record(
        self,
        *,
        table: str,
        pk: str,
        field: str,
        ts: datetime,
        provenance: WriteProvenance,
        to_surface: Surface,
    ) -> None:
        """Append one write event. Pure INSERT — never updates."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO lww_meta "
                "(table_name, row_pk, field, ts, actor, process, "
                " from_surface, to_surface) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    table,
                    pk,
                    field,
                    ts.isoformat(),
                    json.dumps(sorted(provenance.actor)),
                    provenance.process,
                    provenance.from_surface,
                    to_surface,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def latest(
        self, *, table: str, pk: str, field: str, surface: Surface,
    ) -> LwwEntry | None:
        """Most recent event for ``table.pk.field`` on ``surface``.

        Ordered by ``ts`` then ``id`` — the autoincrement id breaks ties
        deterministically when two events share a timestamp.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT ts, actor, process, from_surface, to_surface "
                "FROM lww_meta "
                "WHERE table_name = ? AND row_pk = ? AND field = ? "
                "  AND to_surface = ? "
                "ORDER BY ts DESC, id DESC LIMIT 1",
                (table, pk, field, surface),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        ts_raw, actor_raw, process, from_surface, to_surface = row
        try:
            actor = frozenset(json.loads(actor_raw)) if actor_raw else frozenset()
        except (ValueError, TypeError):
            actor = frozenset()
        return LwwEntry(
            ts=datetime.fromisoformat(ts_raw),
            provenance=WriteProvenance(
                actor=actor, process=process, from_surface=from_surface,
            ),
            to_surface=to_surface,
        )

    # ── Introspection helpers (audit / dashboard) ───────────────────

    def history(
        self, *, table: str, pk: str, field: str | None = None,
    ) -> list[dict]:
        """Full append-only history for an entity (or one of its fields).

        Ordered oldest-first. Useful for "who has been touching this
        field" audit views and as the replay source for a future CRDT
        resolver.
        """
        conn = self._connect()
        try:
            if field is None:
                rows = conn.execute(
                    "SELECT table_name, row_pk, field, ts, actor, process, "
                    "       from_surface, to_surface "
                    "FROM lww_meta WHERE table_name = ? AND row_pk = ? "
                    "ORDER BY ts ASC, id ASC",
                    (table, pk),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT table_name, row_pk, field, ts, actor, process, "
                    "       from_surface, to_surface "
                    "FROM lww_meta WHERE table_name = ? AND row_pk = ? "
                    "  AND field = ? ORDER BY ts ASC, id ASC",
                    (table, pk, field),
                ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            out.append({
                "table_name": r[0], "row_pk": r[1], "field": r[2],
                "ts": r[3], "actor": json.loads(r[4]) if r[4] else [],
                "process": r[5], "from_surface": r[6], "to_surface": r[7],
            })
        return out
