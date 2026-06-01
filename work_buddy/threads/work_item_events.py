"""WorkItem base event log — durable, append-only audit of WorkItem
lifecycle events, spanning subtypes.

The base owns provenance/history for any WorkItem. ``Thread`` already has its rich
``thread_events`` log — but that table's ``thread_id`` foreign-keys
``threads(thread_id)``, and a ``Task`` does not live in the threads table.
So this is the **additive** log for the other subtypes (today: ``Task``),
deliberately a *separate* store keyed by an opaque ``work_item_id`` with
**no foreign key**, so any subtype (Thread, Task, or a future kind) can
emit into it. This is the additive emission path the plan recommended over
a shadow threads row.

Why it matters: it makes the task backlog auditable — every create / state
change / toggle becomes a recorded WorkItem event, which is exactly what
was missing when agents shipped work but "forgot to toggle" the task
(the "agent forgot to toggle" blind spot, where work shipped but the task
record was never closed).

Design rules:
- **Additive + best-effort.** ``emit()`` never raises into its caller — a
  missed audit event must never break a task mutation or a reconcile.
- **Markdown stays source of truth.** This log records *that* a change
  happened (provenance/history); it is NOT authoritative task content.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    """Resolve the work-item events DB path. Tests monkeypatch this."""
    from work_buddy.paths import resolve

    return resolve("db/work_item_events")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_item_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id  TEXT NOT NULL,
    subtype       TEXT,
    kind          TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'agent',
    origin        TEXT NOT NULL DEFAULT 'system',
    data_json     TEXT NOT NULL DEFAULT '{}',
    timestamp     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wie_work_item ON work_item_events(work_item_id);
"""


def get_connection() -> sqlite3.Connection:
    """Open the events DB, ensuring the schema. ``CREATE … IF NOT EXISTS``
    is idempotent + cheap; emit/list are low-frequency (task mutations, not
    a render hot path), so per-connection ensure is fine and keeps tests —
    which monkeypatch ``_db_path`` to a fresh tmp file each — working."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


@dataclass(frozen=True)
class WorkItemEvent:
    work_item_id: str
    kind: str
    actor: str = "agent"
    origin: str = "system"
    subtype: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now_iso)
    id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "work_item_id": self.work_item_id,
            "subtype": self.subtype,
            "kind": self.kind,
            "actor": self.actor,
            "origin": self.origin,
            "data": self.data,
            "timestamp": self.timestamp,
        }


def emit(
    work_item_id: str,
    kind: str,
    *,
    subtype: Optional[str] = None,
    actor: str = "agent",
    origin: str = "system",
    data: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """Append a WorkItem event. **Best-effort: never raises into the
    caller.** Returns the new row id, or ``None`` on failure."""
    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO work_item_events "
                "(work_item_id, subtype, kind, actor, origin, data_json, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    work_item_id,
                    subtype,
                    kind,
                    actor,
                    origin,
                    json.dumps(data or {}),
                    _now_iso(),
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - audit must never break the caller
        logger.exception(
            "work_item_events: emit failed for %r/%r", work_item_id, kind,
        )
        return None


def list_events(work_item_id: str) -> list[dict[str, Any]]:
    """Return this work item's events oldest-first. Best-effort: returns
    ``[]`` on any read failure."""
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM work_item_events WHERE work_item_id = ? "
                "ORDER BY id",
                (work_item_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("work_item_events: list_events failed for %r", work_item_id)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.pop("data_json", None)
        try:
            d["data"] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            d["data"] = {}
        out.append(d)
    return out
