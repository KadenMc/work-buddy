"""``tasks`` context source — active tasks from the selected authority.

After native cutover, structured task rows come directly from
:class:`work_buddy.tasks.store.TaskStore`, and note drill-down returns the
linked projection-free Co-work knowledge document.  The vault task store and
task-note adapter remain reachable only in the guarded pre-cutover branch.

Depth semantics:
  - BRIEF:  top 5 by state priority, title only.
  - NORMAL: top 12, title + state.
  - DEEP:   up to 30, title + state + contract linkage.

``target_date`` support uses the active authority's state history to retain
tasks active on that date. Future dates snap to "now" (no forecasting).
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
            from work_buddy.tasks.runtime import native_authority_active

            if native_authority_active():
                from work_buddy.tasks.store import default_task_db_path

                path = Path(default_task_db_path())
            else:
                from work_buddy.obsidian.tasks.store import _db_path

                path = Path(_db_path())
        except Exception:
            return False
        if not path.exists():
            return False
        store_mtime = path.stat().st_mtime
        cached_at = cached.fetched_at.timestamp()
        return store_mtime > cached_at

    def drill_down(self, item_id: str, field: str) -> dict[str, Any]:
        """Expand one task. ``field='note'`` returns its knowledge content.

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
    from work_buddy.tasks.runtime import native_authority_active

    rows: list[dict[str, Any]] = []
    if native_authority_active():
        # Do not route native reads through the transitional WorkItem facade:
        # querying the owned store directly makes it impossible for this
        # context source to drift back onto a retained Obsidian task adapter.
        try:
            from work_buddy.tasks.models import TaskQuery
            from work_buddy.tasks.store import TaskStore

            store = TaskStore()
            for state in states:
                for task in store.list(
                    TaskQuery(
                        state=state,
                        include_done=state == "done",
                        include_archived=False,
                        include_snoozed=state == "snoozed",
                        limit=5000,
                    )
                ):
                    if not task.description:
                        continue
                    rows.append(
                        {
                            "task_id": task.task_id,
                            "state": task.state,
                            "text": task.description,
                            "contract": task.contract or "",
                        }
                    )
        except Exception as exc:
            logger.debug("tasks source: native task query failed: %s", exc)
            return []
    else:
        try:
            from work_buddy.threads.models import Task
            from work_buddy.clarify.task_match import _read_task_texts
        except Exception as exc:
            logger.debug("tasks source: legacy deps unavailable (%s)", exc)
            return []

        try:
            task_texts = _read_task_texts()
        except Exception as exc:
            # This source is display-only, so a transient legacy bridge error
            # degrades the bundle without being mistaken for authority data.
            from work_buddy.obsidian.errors import ObsidianError

            if isinstance(exc, ObsidianError):
                logger.warning(
                    "tasks source: legacy bridge transient reading task texts "
                    "(%s) — task list shown empty this cycle, will self-heal",
                    exc,
                )
            else:
                logger.debug("tasks source: legacy task text read failed: %s", exc)
            task_texts = {}

        for state in states:
            try:
                query_rows = [t.row for t in Task.query(state=state)]
            except Exception as exc:
                logger.debug(
                    "tasks source: legacy query(state=%r) failed: %s", state, exc
                )
                continue
            for task in query_rows:
                tid = task.get("task_id")
                if not tid:
                    continue
                text = task_texts.get(tid, "")
                if not text:
                    continue
                rows.append(
                    {
                        "task_id": tid,
                        "state": state,
                        "text": text,
                        "contract": task.get("contract", ""),
                    }
                )

    if target_date is None:
        return rows
    return _filter_by_target_date(rows, target_date)


def _filter_by_target_date(
    rows: list[dict[str, Any]],
    target: date,
) -> list[dict[str, Any]]:
    """Keep only tasks whose state on ``target`` was in their current states list.

    Uses the selected authority's history to reconstruct each task's state at
    ``target``. On missing/unreadable history we assume the current state held
    (conservative — includes rather than drops).
    """
    from work_buddy.tasks.runtime import native_authority_active

    if native_authority_active():
        from work_buddy.tasks.store import TaskStore

        task_store = TaskStore()

        def get_history(task_id: str) -> list[dict[str, Any]]:
            return [entry.to_dict() for entry in task_store.history(task_id)]
    else:
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
    """Pull a task's knowledge content. Returns ``None`` when absent.

    Native authority resolves the active Co-work document binding through
    ``task_read``.  Only the pre-cutover compatibility branch resolves a
    retained task-note UUID through the vault adapter.
    """
    from work_buddy.tasks.runtime import native_authority_active

    if native_authority_active():
        from work_buddy.tasks.capabilities import task_read

        payload = task_read(task_id)
        if not payload.get("success"):
            return None
        return payload.get("note_content")

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
    from work_buddy.task_notes import get_task_note_adapter

    # The task-note adapter preserves the same typed transient behavior while
    # selecting the per-note authority epoch.  A genuine absence remains None.
    return get_task_note_adapter(bridge_client=bridge).read(
        note_uuid,
        filesystem_fallback=False,
        strict_bridge=True,
    )


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
