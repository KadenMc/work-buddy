"""``tasks`` context source — currently-active tasks from the vault task store.

Consolidates the task-fetching logic that lived in
:func:`work_buddy.clarify.recommend.build_triage_context`. Items are the
cleaned one-liner task descriptions (from the master task list), not
the full task notes — a future drill-down call via
:func:`work_buddy.context.sources.tasks.TasksSource.drill_down` returns
the note body.

Depth semantics:
  - BRIEF:  top 5 by state priority, title only.
  - NORMAL: top 12, title + state.
  - DEEP:   up to 30, title + state + contract linkage.

``target_date`` support: when the request supplies a past date, we
filter on :func:`work_buddy.obsidian.tasks.store.get_history` —
tasks active on that date based on state transitions. Future dates
snap to "now" (no forecasting).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from work_buddy.context.types import (
    BaseContextSource,
    ContextDepth,
    ContextRequest,
    ContextSection,
)
from work_buddy.context import registry as _registry
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Default state filter + cap — match build_triage_context's defaults so
# the retrofit in recommend.py produces identical output at NORMAL depth.
_DEFAULT_STATES = ("focused", "mit", "inbox")
_STATE_RANK = {"focused": 0, "mit": 1, "inbox": 2}


class TasksSource(BaseContextSource):
    """Active-tasks source. Registered at module import."""

    name = "tasks"

    def collect(self, request: ContextRequest) -> ContextSection:
        custom = request.custom_for(self.name)
        states = tuple(custom.get("states") or _DEFAULT_STATES)
        target = request.target_date

        items = _collect_tasks(states=states, target_date=target)
        items.sort(key=lambda t: _STATE_RANK.get(t.get("state", ""), 99))

        return ContextSection(
            source=self.name,
            items=items,
            metadata={
                "states": list(states),
                "target_date": target.isoformat() if target else None,
                "total_count": len(items),
            },
        )

    def render(self, section: ContextSection, depth: ContextDepth) -> str:
        items = section.items or []
        if not items:
            return ""

        cap = _cap_for_depth(depth)
        rendered = items[:cap]

        lines = [f"### Active Tasks ({len(items)})"]
        for t in rendered:
            tid = t.get("task_id", "?")
            state = t.get("state", "?")
            text = t.get("text", "")
            line = f"- [{tid}] ({state}) {text}"
            if depth >= ContextDepth.DEEP:
                contract = t.get("contract") or ""
                if contract:
                    line += f"  — contract: {contract}"
            lines.append(line)
        if len(items) > cap:
            lines.append(f"- … ({len(items) - cap} more)")
        return "\n".join(lines)

    def is_stale(
        self,
        cached: ContextSection,
        request: ContextRequest,
    ) -> bool:
        """Cheap check: did the task store change since the cache was written?

        Task-store SQLite mtime is our proxy. No database scan — we
        only stat the file. Negligible cost vs the full task query.
        """
        from pathlib import Path
        try:
            from work_buddy.obsidian.tasks.store import _db_path
        except Exception:
            return False
        try:
            path = Path(_db_path())
        except Exception:
            return False
        if not path.exists():
            return False
        store_mtime = path.stat().st_mtime
        cached_at = cached.fetched_at.timestamp()
        return store_mtime > cached_at

    def drill_down(self, item_id: str, field: str) -> dict[str, Any]:
        """Expand one task. ``field='note'`` returns the task-note body.

        ``field='line'`` returns the cleaned one-liner (same shape
        shown in ``items``). Unknown fields raise ``KeyError`` so the
        caller sees a clear error.
        """
        if field == "line":
            from work_buddy.clarify.task_match import _read_task_texts
            texts = _read_task_texts()
            if item_id not in texts:
                raise KeyError(f"Unknown task_id: {item_id!r}")
            return {"task_id": item_id, "line": texts[item_id]}

        if field == "note":
            note = _read_task_note(item_id)
            if note is None:
                raise KeyError(f"No note found for task_id: {item_id!r}")
            return {"task_id": item_id, "note": note}

        raise KeyError(
            f"TasksSource.drill_down: unknown field {field!r}. "
            "Valid: 'line', 'note'."
        )


# ---------------------------------------------------------------------------
# Helpers — kept private so the source's public surface stays the
# ContextSource protocol. Shaped to be easy to swap or extend later.
# ---------------------------------------------------------------------------


def _collect_tasks(
    *,
    states: tuple[str, ...],
    target_date: date | None,
) -> list[dict[str, Any]]:
    """Gather task records matching the states, optionally at ``target_date``.

    Returns one dict per task with keys ``task_id``, ``state``,
    ``text``, ``contract``. Mirrors the shape
    :func:`build_triage_context` produced so callers retrofit cleanly.
    """
    try:
        from work_buddy.threads.models import Task
        from work_buddy.clarify.task_match import _read_task_texts
    except Exception as exc:
        logger.debug("tasks source: deps unavailable (%s)", exc)
        return []

    try:
        task_texts = _read_task_texts()
    except Exception as exc:
        # Best-effort display source: stay resilient (don't break the bundle),
        # but make a transient bridge failure observable rather than silently
        # rendering "0 tasks". The decision-driving callers of _read_task_texts
        # (triage match, drill_down) let the transient propagate instead.
        from work_buddy.obsidian.errors import ObsidianError
        if isinstance(exc, ObsidianError):
            logger.warning(
                "tasks source: bridge transient reading task texts (%s) — "
                "task list shown empty this cycle, will self-heal", exc,
            )
        else:
            logger.debug("tasks source: _read_task_texts failed: %s", exc)
        task_texts = {}

    rows: list[dict[str, Any]] = []
    for state in states:
        try:
            query_rows = [t.row for t in Task.query(state=state)]
        except Exception as exc:
            logger.debug("tasks source: query(state=%r) failed: %s", state, exc)
            continue
        for task in query_rows:
            tid = task.get("task_id")
            if not tid:
                continue
            text = task_texts.get(tid, "")
            if not text:
                continue
            rows.append({
                "task_id": tid,
                "state": state,
                "text": text,
                "contract": task.get("contract", ""),
            })

    if target_date is None:
        return rows
    return _filter_by_target_date(rows, target_date)


def _filter_by_target_date(
    rows: list[dict[str, Any]],
    target: date,
) -> list[dict[str, Any]]:
    """Keep only tasks whose state on ``target`` was in their current states list.

    Uses :func:`task_store.get_history` to reconstruct each task's
    state at ``target``. On missing/unreadable history we assume the
    current state held (conservative — includes rather than drops).
    """
    try:
        from work_buddy.obsidian.tasks.store import get_history
    except Exception:
        return rows

    target_end = datetime.combine(target, datetime.max.time(), tzinfo=timezone.utc)
    filtered: list[dict[str, Any]] = []
    for task in rows:
        tid = task["task_id"]
        try:
            history = get_history(tid) or []
        except Exception:
            history = []
        state_at = _state_at(history, target_end, fallback=task["state"])
        if state_at == task["state"]:
            filtered.append({**task, "state": state_at})
    return filtered


def _state_at(
    history: list[dict[str, Any]],
    when: datetime,
    *,
    fallback: str,
) -> str:
    """Return the task's state as of ``when`` given a newest-first history.

    ``history`` entries have ``changed_at`` (ISO) and ``new_state``.
    We walk from oldest forward, keeping the last transition whose
    ``changed_at <= when``.
    """
    walk = list(reversed(history))  # oldest → newest
    current = fallback
    for row in walk:
        raw = row.get("changed_at", "")
        try:
            ts = datetime.fromisoformat(raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts <= when:
            current = row.get("new_state") or current
    return current


def _read_task_note(task_id: str) -> str | None:
    """Pull a task's note body. Returns None when no note exists.

    Tasks store note UUIDs on each record; the file lives at
    ``tasks/notes/<uuid>.md`` in the vault. We go through the bridge
    so the read is consistent with how other capabilities fetch vault
    content.
    """
    try:
        # Read through the WorkItem family: Task.load carries the row, so
        # .row is the same dict store.get would return (single query).
        from work_buddy.threads.models import Task
        _t = Task.load(task_id)
        task = _t.row if _t is not None else None
    except Exception:
        task = None
    if not task:
        return None
    note_uuid = task.get("note_uuid")
    if not note_uuid:
        return None
    from work_buddy.obsidian import bridge
    # read_file_raw raises a typed ObsidianError on a transient (let it
    # propagate so drill_down surfaces "unavailable" rather than a false "no
    # note found"); a genuine 404 → None means the note really doesn't exist.
    return bridge.read_file_raw(f"tasks/notes/{note_uuid}.md")


def _cap_for_depth(depth: ContextDepth) -> int:
    if depth == ContextDepth.BRIEF:
        return 5
    if depth == ContextDepth.DEEP:
        return 30
    return 12  # NORMAL


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


_registry.register(TasksSource())
