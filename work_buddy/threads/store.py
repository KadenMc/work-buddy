"""SQLite-backed store for Threads and their event log.

Stage 1.3 deliverable: schema, idempotent migration, and *minimum*
CRUD scaffolding for ``threads`` and ``thread_events`` tables. The
behaviour built on top (FSM engine, inference workers, Resolution
Surface publication) lands in Stage 2.

Two-table schema:

- ``threads``        — current-state cache (Thread fields + JSON-blob
                        columns for autonomy_policy, context_items,
                        risk_profile, inciting_event_summary).
- ``thread_events``  — append-only event log; each row is a
                        ``ThreadEvent`` (DESIGN.md §13). Optimistic
                        locking: ``parent_event_id`` on submit must
                        match the latest event for that thread.

Helpers:
- ``get_connection()`` — opens the DB, ensures schema.
- ``insert_thread()`` — INSERT a fresh Thread row.
- ``get_thread()`` — SELECT one Thread by ID.
- ``list_threads()`` — paginated list with optional state filter.
- ``update_thread_state()`` — write the current-state cache.
- ``append_event()`` — insert one event with optimistic-lock check.
- ``list_events()`` — replay a Thread's event log in order.
- ``latest_event_id()`` — fast lookup of the most recent event id.
- ``rebuild_state_from_events()`` — derive current state from log
  (rarely used; the cache is normally authoritative for queries).

See DESIGN.md §13 for the canonical event-log model.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from work_buddy.threads.events import (
    ALL_KINDS,
    OptimisticLockConflict,
    ThreadEvent,
    validate_kind,
)
from work_buddy.threads.models import Thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    """Resolve the threads DB path. Tests monkeypatch this to redirect."""
    from work_buddy.paths import resolve
    return resolve("db/threads")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    -- Core identity & hierarchy
    thread_id                  TEXT PRIMARY KEY,
    parent_id                  TEXT,
    subtype                    TEXT,           -- 'task' | NULL

    -- parent-child relationship discriminator. 'decompose' is
    -- the canonical fanout pattern (parent has a decompose action;
    -- children FSM-execute independently; cascade-on-terminal advances
    -- parent to DONE when all children terminal). 'group' is the new
    -- pattern: parent is a re-organisable container; items can move
    -- between sibling group-parents via the move_thread_to_parent op.
    parent_relationship        TEXT NOT NULL DEFAULT 'decompose',

    -- scope id for sibling-group validation. Parents from one
    -- inference run share an originating_scrape_id (e.g. one Chrome
    -- scrape produces N group-parents, all with the same id). Items
    -- can only move between parents that share this id — preventing
    -- "yesterday's tabs land in today's group" mistakes.
    -- NULL for non-group parents and for legacy data.
    originating_scrape_id      TEXT,

    -- Current-state cache (DESIGN.md says events are canonical;
    -- this column exists for query convenience)
    fsm_state                  TEXT NOT NULL DEFAULT 'proposed',

    -- Optimistic-lock target on the next state transition.
    -- NULL for never-transitioned threads.
    parent_event_id            INTEGER,

    -- JSON blobs (composed/serialised dataclass shapes)
    autonomy_policy_json       TEXT NOT NULL DEFAULT '{}',
    context_items_json         TEXT NOT NULL DEFAULT '[]',
    risk_profile_json          TEXT NOT NULL DEFAULT '{}',
    inciting_event_summary_json TEXT NOT NULL DEFAULT '{}',

    -- Sub-Thread focus pointer (formerly current_action_item_id on
    -- task_metadata; lives on Thread now since action items are
    -- sub-threads). Points at a child Thread.
    current_focus_thread_id    TEXT,

    -- Lifecycle timestamps
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    archived_at                TEXT,

    -- Later mechanic (UX.md §13).
    -- NULL = always visible. ISO 8601 = hide until that time.
    resurface_at               TEXT,

    -- linearization order within siblings (UX.md §8.2).
    -- Computed at WRITE time (decompose, sub-thread spawn). NEVER at
    -- render time. Only meaningful when parent_id is non-NULL.
    order_index                INTEGER NOT NULL DEFAULT 0,

    -- search-blob cache (UX.md §10.2). Denormalised,
    -- substring-searchable text rebuilt on Thread state change.
    search_blob                TEXT NOT NULL DEFAULT '',

    FOREIGN KEY (parent_id) REFERENCES threads(thread_id) ON DELETE CASCADE,
    FOREIGN KEY (current_focus_thread_id) REFERENCES threads(thread_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_parent
    ON threads(parent_id);
CREATE INDEX IF NOT EXISTS idx_threads_state
    ON threads(fsm_state);
CREATE INDEX IF NOT EXISTS idx_threads_subtype
    ON threads(subtype);
CREATE INDEX IF NOT EXISTS idx_threads_parent_order
    ON threads(parent_id, order_index);
CREATE INDEX IF NOT EXISTS idx_threads_resurface
    ON threads(resurface_at);
CREATE INDEX IF NOT EXISTS idx_threads_originating_scrape
    ON threads(originating_scrape_id) WHERE originating_scrape_id IS NOT NULL;


CREATE TABLE IF NOT EXISTS thread_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       TEXT NOT NULL,

    -- Event kind (one of work_buddy.threads.events.ALL_KINDS)
    kind            TEXT NOT NULL,

    -- Who triggered this event (work_buddy.threads.events.ACTOR_*)
    actor           TEXT NOT NULL,

    -- Reasoning tier for inference events (NULL otherwise)
    inference_tier  TEXT,

    -- Wall-clock timestamp (ISO 8601)
    timestamp       TEXT NOT NULL,

    -- Event-specific payload
    data_json       TEXT NOT NULL DEFAULT '{}',

    -- Optimistic-lock target: the latest event ID the actor saw
    -- before deciding. Insert fails (raises OptimisticLockConflict)
    -- if a newer event landed for this thread first.
    parent_event_id INTEGER,

    -- Cross-Thread linked-event marker (e.g. context migration).
    -- Two events from different threads sharing a migration_id
    -- form one logical operation.
    migration_id    TEXT,

    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_thread_events_thread_kind
    ON thread_events(thread_id, kind);
CREATE INDEX IF NOT EXISTS idx_thread_events_thread_id_pk
    ON thread_events(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_thread_events_migration
    ON thread_events(migration_id) WHERE migration_id IS NOT NULL;
"""


_STAGE_4_MIGRATIONS = """
-- Stage 4 added three columns + two indexes. Use ALTER TABLE ADD COLUMN
-- (idempotent only via the try/except below — SQLite has no IF NOT
-- EXISTS for columns).
"""


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    decl: str,
) -> None:
    """Add a column if it's not already present. SQLite-friendly."""
    cols = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_stage_4(conn: sqlite3.Connection) -> None:
    """Add Stage 4 columns to an existing DB (idempotent).

    Indexes referencing the new columns are created via the
    ``_SCHEMA`` script after this runs — so we must add the columns
    *before* ``executescript`` would try to index them.
    """
    # Only run if the threads table already exists (else executescript
    # will create it with all current columns including these).
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='threads'"
    ).fetchone() is not None
    if not has_table:
        return
    _add_column_if_missing(conn, "threads", "resurface_at", "TEXT")
    _add_column_if_missing(
        conn, "threads", "order_index", "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        conn, "threads", "search_blob", "TEXT NOT NULL DEFAULT ''",
    )


def _migrate_stage_5(conn: sqlite3.Connection) -> None:
    """Add Stage 5 columns to an existing DB (idempotent).

    Stage 5 introduces the ``group`` parent-relationship pattern. New
    columns:

    - ``parent_relationship`` — discriminator between 'decompose'
      (the original fanout behaviour, the default) and 'group' (the
      umbrella-with-siblings pattern). Existing rows default to
      'decompose' so behaviour is preserved.
    - ``originating_scrape_id`` — sibling-group scope id (NULL for
      decompose parents and legacy data). Two group-parents with the
      same id are siblings; items can move freely between them.

    Same pattern as ``_migrate_stage_4`` — runs before ``executescript``
    so the index-creation pass at the end of ``_SCHEMA`` doesn't fail
    on missing columns.
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='threads'"
    ).fetchone() is not None
    if not has_table:
        return
    _add_column_if_missing(
        conn, "threads", "parent_relationship",
        "TEXT NOT NULL DEFAULT 'decompose'",
    )
    _add_column_if_missing(
        conn, "threads", "originating_scrape_id", "TEXT",
    )


def get_connection() -> sqlite3.Connection:
    """Open the threads DB with WAL + FK enforcement; ensure schema.

    Order matters: pre-existing tables get Stage-4 / Stage-5 columns
    added before the index-creation pass, so the index DDL doesn't
    fail on missing columns.
    """
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_stage_4(conn)
    _migrate_stage_5(conn)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_json(value: Any) -> str:
    if value is None:
        return "{}"
    return json.dumps(value, default=str)


# ---------------------------------------------------------------------------
# Thread CRUD
# ---------------------------------------------------------------------------


def insert_thread(
    thread: Thread, *, conn: Optional[sqlite3.Connection] = None,
) -> Thread:
    """Insert a fresh Thread row.

    Returns the same Thread (no surprises). The caller is responsible
    for also calling :func:`append_event` with a ``thread_created``
    event to start the event log; this helper does NOT auto-write
    that event because the parent_event_id semantics depend on
    whether an inciting_event was already recorded.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO threads
               (thread_id, parent_id, subtype,
                parent_relationship, originating_scrape_id,
                fsm_state, parent_event_id,
                autonomy_policy_json, context_items_json,
                risk_profile_json, inciting_event_summary_json,
                current_focus_thread_id,
                created_at, updated_at, archived_at,
                resurface_at, order_index, search_blob)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                thread.thread_id,
                thread.parent_id,
                thread.subtype,
                getattr(thread, "parent_relationship", "decompose"),
                getattr(thread, "originating_scrape_id", None),
                thread.fsm_state.value,
                thread.parent_event_id,
                _dump_json(thread.autonomy_policy.to_dict()),
                _dump_json([c.to_dict() for c in thread.context_items]),
                _dump_json(thread.risk_profile),
                _dump_json(thread.inciting_event_summary),
                thread.current_focus_thread_id,
                thread.created_at,
                thread.updated_at,
                thread.archived_at,
                getattr(thread, "resurface_at", None),
                getattr(thread, "order_index", 0),
                getattr(thread, "search_blob", ""),
            ),
        )
        conn.commit()
        # best-effort initial search-blob population.
        # Lazy-import to avoid cycles. Inciting summary alone is
        # enough yield for the first index entry.
        try:
            from work_buddy.threads.search import update_search_blob
            update_search_blob(thread.thread_id, conn=conn)
        except Exception:
            pass  # non-fatal
        return thread
    finally:
        if own_conn:
            conn.close()


def get_thread(
    thread_id: str, *, conn: Optional[sqlite3.Connection] = None,
) -> Optional[Thread]:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return Thread.from_row(dict(row)) if row else None
    finally:
        if own_conn:
            conn.close()


def list_threads(
    *,
    state: Optional[str] = None,
    subtype: Optional[str] = None,
    parent_id: Optional[str] = None,
    limit: int = 100,
    conn: Optional[sqlite3.Connection] = None,
) -> list[Thread]:
    """List threads with sensible default ordering:

    - Sub-threads (parent_id given) sort by ``order_index ASC``
      (the linearization order from Stage 4.7).
    - Top-level threads sort by ``resurface_at DESC NULLS LAST``
      then ``updated_at DESC`` (the Later mechanic from §13).
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("fsm_state = ?")
            params.append(state)
        if subtype is not None:
            clauses.append("subtype IS ?")
            params.append(subtype)
        if parent_id is not None:
            clauses.append("parent_id IS ?")
            params.append(parent_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        if parent_id is not None:
            order = "ORDER BY order_index ASC, updated_at DESC"
        else:
            # NULLS LAST trick: ORDER BY resurface_at IS NULL,
            # resurface_at DESC, updated_at DESC
            order = (
                "ORDER BY (resurface_at IS NULL) ASC, "
                "resurface_at DESC, updated_at DESC"
            )
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM threads{where} {order} LIMIT ?",
            params,
        ).fetchall()
        return [Thread.from_row(dict(r)) for r in rows]
    finally:
        if own_conn:
            conn.close()


def find_open_umbrella_by_dedup_key(
    dedup_key: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """Return the id of an open top-level umbrella whose
    ``inciting_event_summary.dedup_key`` matches.

    "Open" = ``fsm_state == 'monitoring'`` (umbrellas spawned by
    :mod:`work_buddy.pipelines.runner` rest in MONITORING). Terminal
    umbrellas (DONE / DISMISSED) deliberately do NOT match — a
    rescheduled run on the same scope should produce a fresh umbrella
    the next day.

    Cardinality of live umbrellas is small (one per source per day at
    most), so the dedup key is a JSON-blob field rather than an indexed
    column: list the candidates and filter in Python.

    Returns the matching umbrella's ``thread_id``, or ``None`` if no
    open umbrella carries the key.
    """
    if not dedup_key:
        return None
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT thread_id, inciting_event_summary_json FROM threads "
            "WHERE parent_id IS NULL AND fsm_state = ? "
            "ORDER BY updated_at DESC",
            ("monitoring",),
        ).fetchall()
        for r in rows:
            try:
                summary = json.loads(
                    r["inciting_event_summary_json"] or "{}"
                )
            except (json.JSONDecodeError, TypeError):
                continue
            if summary.get("dedup_key") == dedup_key:
                return r["thread_id"]
        return None
    finally:
        if own_conn:
            conn.close()


_UPDATE_SENTINEL = object()


def update_thread_state(
    thread_id: str,
    *,
    fsm_state: Optional[str] = None,
    parent_event_id: Optional[int] = None,
    current_focus_thread_id: Optional[str] = None,
    archived_at: Optional[str] = None,
    resurface_at: Any = _UPDATE_SENTINEL,
    order_index: Optional[int] = None,
    search_blob: Optional[str] = None,
    parent_id: Any = _UPDATE_SENTINEL,
    parent_relationship: Optional[str] = None,
    originating_scrape_id: Any = _UPDATE_SENTINEL,
    context_items: Any = _UPDATE_SENTINEL,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Update mutable fields of the current-state cache.

    Returns True if a row was updated.

    NOTE: this is a *cache* update. The canonical state lives in the
    event log; the cache exists for query convenience. The Stage 2
    FSM engine will update the cache as part of writing each
    transition event; ad-hoc callers should generally not use this
    directly.

    ``resurface_at`` / ``parent_id`` / ``originating_scrape_id`` /
    ``context_items`` accept ``None`` explicitly (clear / empty
    tuple) — use the sentinel to distinguish "don't touch" from
    "set empty".

    ``parent_id`` writes are how the move-between-groups op
    rewrites a child's parent pointer.

    ``context_items`` writes are how
    ``threads.group.move_item`` rewrites the cached items tuple on
    src + dest sibling group children. Pass any iterable of
    ``ContextItem`` (or ``None`` to clear); the value is serialised
    to ``context_items_json``.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [_now_iso()]
        if fsm_state is not None:
            sets.append("fsm_state = ?")
            params.append(fsm_state)
        if parent_event_id is not None:
            sets.append("parent_event_id = ?")
            params.append(parent_event_id)
        if current_focus_thread_id is not None:
            sets.append("current_focus_thread_id = ?")
            params.append(current_focus_thread_id)
        if archived_at is not None:
            sets.append("archived_at = ?")
            params.append(archived_at)
        if resurface_at is not _UPDATE_SENTINEL:
            sets.append("resurface_at = ?")
            params.append(resurface_at)
        if order_index is not None:
            sets.append("order_index = ?")
            params.append(order_index)
        if search_blob is not None:
            sets.append("search_blob = ?")
            params.append(search_blob)
        if parent_id is not _UPDATE_SENTINEL:
            sets.append("parent_id = ?")
            params.append(parent_id)
        if parent_relationship is not None:
            sets.append("parent_relationship = ?")
            params.append(parent_relationship)
        if originating_scrape_id is not _UPDATE_SENTINEL:
            sets.append("originating_scrape_id = ?")
            params.append(originating_scrape_id)
        if context_items is not _UPDATE_SENTINEL:
            sets.append("context_items_json = ?")
            if context_items is None:
                params.append("[]")
            else:
                params.append(
                    json.dumps([
                        c.to_dict() if hasattr(c, "to_dict") else c
                        for c in context_items
                    ])
                )
        params.append(thread_id)
        cur = conn.execute(
            f"UPDATE threads SET {', '.join(sets)} WHERE thread_id = ?",
            params,
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def append_event(
    event: ThreadEvent,
    *,
    expect_parent_event_id: Any = "USE_EVENT_FIELD",
    conn: Optional[sqlite3.Connection] = None,
) -> ThreadEvent:
    """Append an event to a Thread's log with optimistic-lock check.

    The lock target is taken from ``event.parent_event_id`` unless the
    caller passes an explicit ``expect_parent_event_id`` (use the
    sentinel value to opt out of the check entirely).

    Raises:
        :class:`work_buddy.threads.events.OptimisticLockConflict` if
        a newer event has landed for this thread.
        ``ValueError`` if ``event.kind`` is not in the canonical
        catalog.

    Returns the same event with ``event.id`` populated.
    """
    validate_kind(event.kind)

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        # Optimistic-lock check
        if expect_parent_event_id == "USE_EVENT_FIELD":
            expect_parent_event_id = event.parent_event_id
        if expect_parent_event_id is not None:
            latest = latest_event_id(event.thread_id, conn=conn)
            if latest != expect_parent_event_id:
                raise OptimisticLockConflict(
                    f"Thread {event.thread_id} latest event "
                    f"is {latest!r}, expected "
                    f"{expect_parent_event_id!r}; re-read and retry.",
                )

        cur = conn.execute(
            """INSERT INTO thread_events
               (thread_id, kind, actor, inference_tier, timestamp,
                data_json, parent_event_id, migration_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.thread_id,
                event.kind,
                event.actor,
                event.inference_tier,
                event.timestamp,
                _dump_json(event.data),
                event.parent_event_id,
                event.migration_id,
            ),
        )
        event.id = cur.lastrowid
        conn.commit()
        return event
    finally:
        if own_conn:
            conn.close()


def list_events(
    thread_id: str,
    *,
    kinds: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[ThreadEvent]:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        clauses = ["thread_id = ?"]
        params: list[Any] = [thread_id]
        if kinds:
            placeholders = ", ".join("?" * len(list(kinds)))
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        sql = (
            f"SELECT * FROM thread_events WHERE {' AND '.join(clauses)} "
            f"ORDER BY id ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [ThreadEvent.from_row(dict(r)) for r in rows]
    finally:
        if own_conn:
            conn.close()


def latest_event_id(
    thread_id: str, *, conn: Optional[sqlite3.Connection] = None,
) -> Optional[int]:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM thread_events WHERE thread_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row["id"] if row else None
    finally:
        if own_conn:
            conn.close()


def get_linked_events(
    migration_id: str, *, conn: Optional[sqlite3.Connection] = None,
) -> list[ThreadEvent]:
    """Return the linked events sharing a ``migration_id``.

    Used for cross-Thread audit (e.g. a context-migration produces
    one ``context_removed`` on the source Thread + one
    ``context_added`` on the destination, both sharing a single
    migration_id).
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM thread_events WHERE migration_id = ? "
            "ORDER BY id ASC",
            (migration_id,),
        ).fetchall()
        return [ThreadEvent.from_row(dict(r)) for r in rows]
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Auto-init on first import (best-effort; failures are non-fatal so
# this module can be imported in environments without a writable
# data dir, e.g. doc generation).
# ---------------------------------------------------------------------------


def _init_schema_safe() -> None:
    try:
        conn = get_connection()
        conn.close()
    except Exception as e:
        logger.warning("Threads store schema init skipped: %s", e)


_init_schema_safe()
