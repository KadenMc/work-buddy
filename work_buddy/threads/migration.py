"""Stage 3 cutover: migrate v4 entities to v5 Threads.

Per IMPLEMENTATION-PLAN.md Stage 3, this module reads v4 sources
(task_metadata, task_action_items, ClarifyPool) and writes
equivalent v5 Threads + thread_events.

Two modes:
- ``dry_run=True`` (DEFAULT): runs against a sandboxed v5 DB (caller
  passes the path), produces a :class:`MigrationReport`, does NOT
  touch real data. The pre-flight rehearsal.
- ``dry_run=False``: writes to the live v5 DB (whatever
  ``store._db_path()`` resolves). Should only be run after a DB
  dump and after the dry-run output looks clean.

The state mapping reuses ``work_buddy.threads.aggregator`` so the
synthesised Threads match what the aggregator already produces.
The aggregator stays in place after migration — it just becomes a
no-op for migrated entities (they're real Threads now).

ID strategy
-----------

Migrated Threads get **fresh** ``th-`` IDs (not the
``agg-task-<id>`` synthetic IDs the aggregator returns) so that
post-migration code can't accidentally treat a real row as a
synthetic one. The migration records the v4-id → v5-id mapping in
a side table (``migration_id_map``) for audit and rollback.

DESIGN.md §16.2 (migration sequencing principle).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from work_buddy.threads import aggregator, store
from work_buddy.threads.enums import Authorship, FSMState
from work_buddy.threads.events import (
    ACTOR_INCITING,
    KIND_INCITING_EVENT,
    KIND_THREAD_CREATED,
    ThreadEvent,
)
from work_buddy.threads.models import (
    AutonomyPolicy,
    ContextItem,
    Task,
    Thread,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping table
# ---------------------------------------------------------------------------
#
# Records which v4 entity each migrated Thread corresponds to.
# Lives in the v5 threads DB (a sibling table).
# ---------------------------------------------------------------------------


_MAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_id_map (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    v4_kind         TEXT NOT NULL,        -- 'task' | 'action_item' | 'pool_entry'
    v4_id           TEXT NOT NULL,
    v5_thread_id    TEXT NOT NULL,
    migrated_at     TEXT NOT NULL,
    notes           TEXT,
    UNIQUE (v4_kind, v4_id)
);

CREATE INDEX IF NOT EXISTS idx_migration_v5_thread
    ON migration_id_map(v5_thread_id);
"""


def _ensure_map_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_MAP_SCHEMA)


def _record_mapping(
    conn: sqlite3.Connection,
    v4_kind: str,
    v4_id: str,
    v5_thread_id: str,
    notes: Optional[str] = None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO migration_id_map
           (v4_kind, v4_id, v5_thread_id, migrated_at, notes)
           VALUES (?, ?, ?, ?, ?)""",
        (v4_kind, v4_id, v5_thread_id, _now_iso(), notes),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Migration report
# ---------------------------------------------------------------------------


@dataclass
class MigrationReport:
    """Summary of what migration did (or would do, in dry-run)."""

    started_at: str = ""
    finished_at: str = ""
    dry_run: bool = True

    # Counts
    tasks_seen: int = 0
    tasks_migrated: int = 0
    action_items_seen: int = 0
    action_items_migrated: int = 0
    pool_entries_seen: int = 0
    pool_entries_migrated: int = 0

    # State histograms (post-migration FSM state distribution)
    task_state_histogram: dict[str, int] = field(default_factory=dict)
    action_item_state_histogram: dict[str, int] = field(default_factory=dict)
    pool_state_histogram: dict[str, int] = field(default_factory=dict)

    # Issues
    orphan_action_items: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def total_seen(self) -> int:
        return self.tasks_seen + self.action_items_seen + self.pool_entries_seen

    def total_migrated(self) -> int:
        return (
            self.tasks_migrated
            + self.action_items_migrated
            + self.pool_entries_migrated
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        """Human-readable text report."""
        lines: list[str] = []
        lines.append(
            f"=== v5 Migration Report ({'DRY RUN' if self.dry_run else 'LIVE'}) ===",
        )
        lines.append(f"Started:  {self.started_at}")
        lines.append(f"Finished: {self.finished_at}")
        lines.append("")
        lines.append("Counts:")
        lines.append(f"  Tasks:        seen={self.tasks_seen}  migrated={self.tasks_migrated}")
        lines.append(f"  Action items: seen={self.action_items_seen}  migrated={self.action_items_migrated}")
        lines.append(f"  Pool entries: seen={self.pool_entries_seen}  migrated={self.pool_entries_migrated}")
        lines.append(f"  TOTAL:        seen={self.total_seen()}  migrated={self.total_migrated()}")
        lines.append("")
        if self.task_state_histogram:
            lines.append("Task state distribution:")
            for k, v in sorted(self.task_state_histogram.items()):
                lines.append(f"  {k}: {v}")
            lines.append("")
        if self.action_item_state_histogram:
            lines.append("Action-item state distribution:")
            for k, v in sorted(self.action_item_state_histogram.items()):
                lines.append(f"  {k}: {v}")
            lines.append("")
        if self.pool_state_histogram:
            lines.append("Pool-entry state distribution:")
            for k, v in sorted(self.pool_state_histogram.items()):
                lines.append(f"  {k}: {v}")
            lines.append("")
        if self.orphan_action_items:
            lines.append(
                f"ORPHAN action items (parent task missing): "
                f"{len(self.orphan_action_items)}",
            )
            for oid in self.orphan_action_items[:10]:
                lines.append(f"  - {oid}")
            if len(self.orphan_action_items) > 10:
                lines.append(f"  ... and {len(self.orphan_action_items) - 10} more")
            lines.append("")
        if self.skipped:
            lines.append(f"SKIPPED: {len(self.skipped)}")
            for s in self.skipped[:10]:
                lines.append(f"  - {s}")
            lines.append("")
        if self.errors:
            lines.append(f"ERRORS: {len(self.errors)}")
            for e in self.errors[:10]:
                lines.append(f"  - {e}")
            lines.append("")
        if not (self.skipped or self.errors or self.orphan_action_items):
            lines.append("No issues. [OK]")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-entity migration helpers
# ---------------------------------------------------------------------------


def _new_thread_id() -> str:
    return f"th-{uuid.uuid4().hex[:8]}"


def _migrate_task_row(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    v4_to_v5: dict[str, str],
    report: MigrationReport,
) -> Optional[str]:
    """Migrate one task_metadata row to a v5 Task(Thread). Returns
    the new thread_id or None on error."""
    v4_id = row["task_id"]
    if v4_id in v4_to_v5:
        # Already migrated — idempotent
        return v4_to_v5[v4_id]

    # Synthesise via aggregator's logic so state mapping is consistent.
    synth = aggregator._task_row_to_thread(row)
    new_id = _new_thread_id()
    task = Task(
        thread_id=new_id,
        parent_id=None,
        fsm_state=synth.fsm_state,
        autonomy_policy=AutonomyPolicy(),
        context_items=(),
        risk_profile=synth.risk_profile,
        inciting_event_summary={
            "source": "v4_task_metadata",
            "task_id": v4_id,
            "creation_provenance": row.get("creation_provenance"),
            "migrated_from_v4_at": _now_iso(),
        },
        created_at=row.get("created_at") or _now_iso(),
        updated_at=row.get("updated_at") or _now_iso(),
        archived_at=row.get("archived_at"),
        # current_focus_thread_id is wired up after action items migrate
    )
    store.insert_thread(task, conn=conn)

    # Inciting event + thread_created event
    inciting = store.append_event(
        ThreadEvent(
            thread_id=new_id,
            kind=KIND_INCITING_EVENT,
            actor=ACTOR_INCITING,
            data={
                "source": "v4_task_metadata",
                "task_id": v4_id,
                "description": row.get("description"),
            },
            timestamp=row.get("created_at") or _now_iso(),
        ),
        conn=conn,
    )
    store.append_event(
        ThreadEvent(
            thread_id=new_id,
            kind=KIND_THREAD_CREATED,
            actor=ACTOR_INCITING,
            data={"migrated_from_v4": True, "v4_kind": "task", "v4_id": v4_id},
            parent_event_id=inciting.id,
        ),
        conn=conn,
    )
    # Update parent_event_id cache so downstream transitions don't
    # see stale lock targets.
    store.update_thread_state(
        new_id,
        parent_event_id=store.latest_event_id(new_id, conn=conn),
        conn=conn,
    )

    _record_mapping(conn, "task", v4_id, new_id, notes=row.get("description"))
    v4_to_v5[v4_id] = new_id

    state_label = synth.fsm_state.value
    report.task_state_histogram[state_label] = (
        report.task_state_histogram.get(state_label, 0) + 1
    )
    report.tasks_migrated += 1
    return new_id


def _migrate_action_item_row(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    v4_to_v5: dict[str, str],
    report: MigrationReport,
) -> Optional[str]:
    """Migrate one task_action_items row to a sub-Thread. Returns
    the new thread_id or None if the parent task is missing."""
    v4_item_id = str(row["id"])
    v4_task_id = row["task_id"]
    parent_v5_id = v4_to_v5.get(v4_task_id)
    if parent_v5_id is None:
        # Orphan — parent task wasn't migrated for some reason.
        report.orphan_action_items.append(v4_item_id)
        return None

    map_key = f"action_item:{v4_item_id}"
    if map_key in v4_to_v5:
        return v4_to_v5[map_key]

    synth = aggregator._action_item_row_to_thread(row)
    new_id = _new_thread_id()
    sub = Thread(
        thread_id=new_id,
        parent_id=parent_v5_id,
        subtype=None,  # NOT a Task — sub-Thread
        fsm_state=synth.fsm_state,
        autonomy_policy=AutonomyPolicy(),
        context_items=(),
        inciting_event_summary={
            "source": "v4_task_action_items",
            "item_id": v4_item_id,
            "task_v4_id": v4_task_id,
            "task_v5_id": parent_v5_id,
            "description": row.get("description"),
            "authorship": aggregator._action_item_authorship(row),
            "sequence": row.get("sequence"),
            "migrated_from_v4_at": _now_iso(),
        },
        created_at=row.get("created_at") or _now_iso(),
        updated_at=row.get("updated_at") or _now_iso(),
    )
    store.insert_thread(sub, conn=conn)
    inciting = store.append_event(
        ThreadEvent(
            thread_id=new_id,
            kind=KIND_INCITING_EVENT,
            actor=ACTOR_INCITING,
            data={
                "source": "v4_task_action_items",
                "v4_item_id": v4_item_id,
                "v4_task_id": v4_task_id,
                "description": row.get("description"),
            },
            timestamp=row.get("created_at") or _now_iso(),
        ),
        conn=conn,
    )
    store.append_event(
        ThreadEvent(
            thread_id=new_id,
            kind=KIND_THREAD_CREATED,
            actor=ACTOR_INCITING,
            data={
                "migrated_from_v4": True,
                "v4_kind": "action_item",
                "v4_id": v4_item_id,
            },
            parent_event_id=inciting.id,
        ),
        conn=conn,
    )
    store.update_thread_state(
        new_id,
        parent_event_id=store.latest_event_id(new_id, conn=conn),
        conn=conn,
    )

    _record_mapping(conn, "action_item", v4_item_id, new_id,
                    notes=row.get("description"))
    v4_to_v5[map_key] = new_id

    state_label = synth.fsm_state.value
    report.action_item_state_histogram[state_label] = (
        report.action_item_state_histogram.get(state_label, 0) + 1
    )
    report.action_items_migrated += 1
    return new_id


# ---------------------------------------------------------------------------
# ClarifyPool entries — INTENTIONALLY NOT MIGRATED (UX.md §14)
#
# Per the design conversation, PoolEntries are a v4 caching layer
# between source scanners and the Triage Review surface. They are
# NOT inciting events for v5 Threads — the inciting source is the
# underlying journal/inline/chrome/email source. So pool entries
# are dropped on cutover; the source scanners run post-cutover and
# re-create as v5 Threads with proper inciting sources.
#
# The function below is preserved as a SHIM that records the
# decision in the report rather than migrating. Stage 4.14
# (UX.md §14) revises the migration runbook accordingly.
# ---------------------------------------------------------------------------


def _migrate_pool_entry(
    conn: sqlite3.Connection,
    entry: Any,
    *,
    v4_to_v5: dict[str, str],
    report: MigrationReport,
) -> Optional[str]:
    """pool entries are NOT migrated.

    Per the v5 architectural correction (UX.md §14), ClarifyPool
    entries are a v4 caching layer; the inciting source for v5
    is the underlying scanner output (journal note, chrome tab,
    inline TODO). Post-cutover, source scanners run via the
    spawn helpers in ``source_pipelines.py`` and create v5
    Threads with correct inciting provenance.

    This function returns None and records the skip in the
    report's ``skipped`` list for audit.
    """
    v4_id = f"{getattr(entry, 'run_id', '?')}:{getattr(entry, 'item_id', '?')}"
    report.skipped.append({
        "kind": "pool_entry",
        "v4_id": v4_id,
        "reason": (
            "pool entries are dropped per UX.md §14 — the source "
            "scanners (journal, chrome, inline) re-create as v5 "
            "Threads with proper inciting provenance"
        ),
    })
    return None


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def run_migration(
    *,
    dry_run: bool = True,
    v5_db_path: Optional[Path] = None,
    include_pool_entries: bool = True,
    monkeypatch_threads_db: bool = True,
) -> MigrationReport:
    """Run the migration. Default is dry_run=True.

    Parameters
    ----------
    dry_run:
        If True, ``v5_db_path`` is required and writes go there
        instead of the live v5 DB. The v4 sources are read live.
    v5_db_path:
        Sandboxed v5 DB path for dry runs. Required when
        dry_run=True.
    include_pool_entries:
        If False, skip the ClarifyPool sweep (useful for fast tests
        that don't have a vault available).
    monkeypatch_threads_db:
        Internal: when dry_run, redirect store._db_path() to
        v5_db_path. Tests that already monkeypatch can pass False.
    """
    if dry_run and v5_db_path is None:
        raise ValueError("dry_run=True requires v5_db_path")

    report = MigrationReport(
        started_at=_now_iso(), dry_run=dry_run,
    )

    # When dry_run, we redirect the v5 DB path so the live one
    # stays untouched.
    if dry_run and monkeypatch_threads_db:
        original_path = store._db_path
        store._db_path = lambda: v5_db_path  # type: ignore[assignment]
    else:
        original_path = None

    try:
        conn = store.get_connection()
        try:
            _ensure_map_schema(conn)
            v4_to_v5: dict[str, str] = {}

            # Pre-load any prior mapping so the migration is idempotent
            for row in conn.execute(
                "SELECT v4_kind, v4_id, v5_thread_id FROM migration_id_map"
            ).fetchall():
                key = (
                    row["v4_id"]
                    if row["v4_kind"] == "task"
                    else f"{row['v4_kind']}:{row['v4_id']}"
                )
                v4_to_v5[key] = row["v5_thread_id"]

            # 1. Tasks
            try:
                from work_buddy.obsidian.tasks import store as task_store
                task_rows = task_store.query()  # all
            except Exception as e:
                report.errors.append({"phase": "task_query", "error": str(e)})
                task_rows = []
            report.tasks_seen = len(task_rows)
            for row in task_rows:
                try:
                    _migrate_task_row(
                        conn, row,
                        v4_to_v5=v4_to_v5, report=report,
                    )
                except Exception as e:
                    report.errors.append({
                        "phase": "task", "v4_id": row.get("task_id"),
                        "error": f"{type(e).__name__}: {e}",
                    })

            # 2. Action items (only after tasks so parents resolve)
            try:
                from work_buddy.obsidian.tasks import action_items
                ai_rows = []
                for t in task_rows:
                    ai_rows.extend(
                        action_items.list_for_task(t["task_id"], include_done=True)
                    )
            except Exception as e:
                report.errors.append({"phase": "action_item_query", "error": str(e)})
                ai_rows = []
            report.action_items_seen = len(ai_rows)
            for row in ai_rows:
                try:
                    _migrate_action_item_row(
                        conn, row,
                        v4_to_v5=v4_to_v5, report=report,
                    )
                except Exception as e:
                    report.errors.append({
                        "phase": "action_item", "v4_id": row.get("id"),
                        "error": f"{type(e).__name__}: {e}",
                    })

            # 3. Pool entries
            if include_pool_entries:
                try:
                    from work_buddy.clarify.background import ClarifyPool
                    pool = ClarifyPool.default()
                    entries = pool.all_entries()
                except Exception as e:
                    report.errors.append({
                        "phase": "pool_query", "error": str(e),
                    })
                    entries = []
                report.pool_entries_seen = len(entries)
                for entry in entries:
                    try:
                        _migrate_pool_entry(
                            conn, entry,
                            v4_to_v5=v4_to_v5, report=report,
                        )
                    except Exception as e:
                        report.errors.append({
                            "phase": "pool_entry",
                            "v4_id": getattr(entry, "item_id", None),
                            "error": f"{type(e).__name__}: {e}",
                        })

            # 4. Wire current_focus_thread_id from v4
            # current_action_item_id (now refers to v5 child id)
            for row in task_rows:
                v4_task_id = row["task_id"]
                v4_focus = row.get("current_action_item_id")
                if v4_focus is None:
                    continue
                v5_task_id = v4_to_v5.get(v4_task_id)
                v5_child_id = v4_to_v5.get(f"action_item:{v4_focus}")
                if v5_task_id and v5_child_id:
                    store.update_thread_state(
                        v5_task_id,
                        current_focus_thread_id=v5_child_id,
                        conn=conn,
                    )

            conn.commit()
        finally:
            conn.close()
    finally:
        if original_path is not None:
            store._db_path = original_path  # type: ignore[assignment]

    report.finished_at = _now_iso()
    return report
