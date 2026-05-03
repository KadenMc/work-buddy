"""Read-only aggregator: synthesize v4 entities as v5 Threads.

Stage 1.10 deliverable. The aggregator lets v5 query paths see v4
data WITHOUT the v4 entities being migrated yet. Stage 3 (cutover)
performs the real migration; until then the aggregator is the
bridge.

Sources mapped:
- ``task_metadata`` → Thread(subtype='task').
- ``task_action_items`` → Thread(parent_id=task's aggregated id).
- ClarifyPool entries (``data/clarify/triage_pool/pool.json``) →
  Thread in ``awaiting_*_clarification``-ish state.

Thread IDs follow a deterministic ``agg-`` prefix scheme so the
aggregator returns stable references across calls:
- ``agg-task-<task_id>``
- ``agg-ai-<id>`` (action-item DB id)
- ``agg-pool-<run_id>:<item_id>``

This module is **read-only**. It never writes back to the v4
tables. It does NOT read from the v5 ``threads`` table — that is
separate.

DESIGN.md §16.2 (migration sequencing principle), Stage 3 of
IMPLEMENTATION-PLAN.md.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from work_buddy.threads.enums import Authorship, FSMState
from work_buddy.threads.models import AutonomyPolicy, ContextItem, Task, Thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Aggregated-Thread ID prefix scheme
# ---------------------------------------------------------------------------

_TASK_PREFIX = "agg-task-"
_ACTION_ITEM_PREFIX = "agg-ai-"
_POOL_PREFIX = "agg-pool-"


def is_aggregated_id(thread_id: str) -> bool:
    """True if ``thread_id`` came from the aggregator (vs. a real v5 row)."""
    return thread_id.startswith((_TASK_PREFIX, _ACTION_ITEM_PREFIX, _POOL_PREFIX))


# ---------------------------------------------------------------------------
# v4 → v5 state mapping
# ---------------------------------------------------------------------------


def _task_state_to_fsm(task_row: dict[str, Any]) -> FSMState:
    """Map a v4 task_metadata row to a v5 FSMState."""
    if task_row.get("archived_at"):
        return FSMState.DONE
    state = task_row.get("state", "inbox")
    if state == "done":
        return FSMState.DONE
    if state in ("focused", "mit"):
        return FSMState.AWAITING_CONFIRMATION
    if state == "snoozed":
        # Snoozed = deferred re-entry; not really a v5 state. Map to
        # AWAITING_REDIRECT as the closest "waiting on user" state.
        return FSMState.AWAITING_REDIRECT
    return FSMState.PROPOSED


def _action_item_authorship(item_row: dict[str, Any]) -> str:
    """Resolve a row's authorship string, preferring the post-PR-70
    enum but falling back to derive from the pre-PR-70 columns
    (``user_authored`` + ``approved_at``).

    Returns one of ``Authorship`` values.
    """
    val = item_row.get("authorship")
    if isinstance(val, str) and val:
        return val
    # Pre-PR-70 fallback
    user_authored = item_row.get("user_authored")
    approved_at = item_row.get("approved_at")
    if user_authored:
        return Authorship.USER.value
    if approved_at:
        return Authorship.AGENT_APPROVED.value
    return Authorship.AGENT_UNAPPROVED.value


def _action_item_state_to_fsm(item_row: dict[str, Any]) -> FSMState:
    """Map an action-item row's state + authorship to a v5 FSMState."""
    state = item_row.get("state", "pending")
    if state == "done":
        return FSMState.DONE
    if state == "skipped":
        return FSMState.DISMISSED
    if state == "in_progress":
        return FSMState.EXECUTING
    # Pending — distinguish by authorship.
    authorship = _action_item_authorship(item_row)
    if authorship == Authorship.AGENT_UNAPPROVED.value:
        # Awaiting human confirmation before it can run.
        return FSMState.AWAITING_CONFIRMATION
    # User-authored or agent-approved → ready to execute (resolution
    # is "done" from the planning perspective; execution is what's
    # pending). Map to AWAITING_CONFIRMATION too — Stage 2's FSM
    # engine will narrow based on the actual run.
    return FSMState.AWAITING_CONFIRMATION


def _pool_state_to_fsm(entry: Any) -> FSMState:
    """Map a ClarifyEntry's state to a v5 FSMState."""
    state = getattr(entry, "state", "pending")
    review_outcome = getattr(entry, "review_outcome", None)
    if state == "reviewed":
        return FSMState.DONE if review_outcome == "approved" else FSMState.DISMISSED
    if state == "quarantined":
        return FSMState.DISMISSED
    if state == "expired":
        return FSMState.DISMISSED
    # Pending → the agent has produced a verdict (sitting in the
    # entry's ``verdict`` field) waiting for the user to confirm.
    # That's an intent-confirmation in v5 vocabulary.
    return FSMState.AWAITING_INTENT_CONFIRMATION


# ---------------------------------------------------------------------------
# Conversion functions
# ---------------------------------------------------------------------------


def _task_row_to_thread(row: dict[str, Any]) -> Thread:
    """Synthesize a Task(Thread) from a task_metadata row."""
    task_id = row["task_id"]
    inciting = {
        "source": "v4_task_metadata",
        "task_id": task_id,
        "creation_provenance": row.get("creation_provenance"),
    }
    risk_profile = row.get("risk_profile_json")
    if risk_profile and isinstance(risk_profile, str):
        import json
        try:
            risk_profile = json.loads(risk_profile)
        except Exception:
            risk_profile = {}
    elif not isinstance(risk_profile, dict):
        risk_profile = {}

    return Task(
        thread_id=f"{_TASK_PREFIX}{task_id}",
        parent_id=None,
        fsm_state=_task_state_to_fsm(row),
        autonomy_policy=AutonomyPolicy(),
        context_items=(),
        risk_profile=risk_profile,
        inciting_event_summary=inciting,
        created_at=row.get("created_at") or "",
        updated_at=row.get("updated_at") or "",
        archived_at=row.get("archived_at"),
        current_focus_thread_id=(
            f"{_ACTION_ITEM_PREFIX}{row['current_action_item_id']}"
            if row.get("current_action_item_id") is not None
            else None
        ),
    )


def _action_item_row_to_thread(row: dict[str, Any]) -> Thread:
    """Synthesize a sub-Thread (parent=Task) from an action-item row."""
    item_id = row["id"]
    task_id = row["task_id"]
    inciting = {
        "source": "v4_task_action_items",
        "item_id": item_id,
        "task_id": task_id,
        "description": row.get("description"),
        "authorship": _action_item_authorship(row),
        "sequence": row.get("sequence"),
    }
    return Thread(
        thread_id=f"{_ACTION_ITEM_PREFIX}{item_id}",
        parent_id=f"{_TASK_PREFIX}{task_id}",
        subtype=None,  # NOT a Task — action items become plain sub-Threads
        fsm_state=_action_item_state_to_fsm(row),
        autonomy_policy=AutonomyPolicy(),
        context_items=(),
        inciting_event_summary=inciting,
        created_at=row.get("created_at") or "",
        updated_at=row.get("updated_at") or "",
        archived_at=None,
    )


def _pool_entry_to_thread(entry: Any) -> Thread:
    """Synthesize a Thread from a ClarifyEntry (clarify pool)."""
    run_id = getattr(entry, "run_id", "?")
    item_id = getattr(entry, "item_id", "?")
    item = getattr(entry, "item", {}) or {}
    inciting = {
        "source": "v4_clarify_pool",
        "adapter": getattr(entry, "adapter", None),
        "pool_source": getattr(entry, "source", None),
        "item_id": item_id,
        "verdict": getattr(entry, "verdict", {}) or {},
        "state": getattr(entry, "state", None),
    }
    label = item.get("title") or item.get("text") or item_id
    ctx_item = ContextItem(
        id=item_id,
        source=getattr(entry, "source", "unknown"),
        type=item.get("type", "pool_item"),
        label=str(label),
        payload=item,
    )
    return Thread(
        thread_id=f"{_POOL_PREFIX}{run_id}:{item_id}",
        parent_id=None,
        subtype=None,
        fsm_state=_pool_state_to_fsm(entry),
        autonomy_policy=AutonomyPolicy(),
        context_items=(ctx_item,),
        inciting_event_summary=inciting,
        created_at=getattr(entry, "created_at", "") or "",
        updated_at=(
            getattr(entry, "state_changed_at", None)
            or getattr(entry, "reviewed_at", None)
            or getattr(entry, "created_at", "")
            or ""
        ),
        archived_at=None,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_aggregated_tasks() -> list[Thread]:
    """Return all v4 tasks as v5 Tasks. Read-only."""
    try:
        from work_buddy.obsidian.tasks import store as _task_store
    except Exception as e:
        logger.warning("Aggregator: cannot load task store: %s", e)
        return []
    try:
        rows = _task_store.query()  # all tasks
    except Exception as e:
        logger.warning("Aggregator: task query failed: %s", e)
        return []
    return [_task_row_to_thread(r) for r in rows]


def list_aggregated_action_items(
    *, task_id: Optional[str] = None,
) -> list[Thread]:
    """Return action items as sub-Threads.

    If ``task_id`` is the AGGREGATED id (``agg-task-<id>``), list
    items under that task. If it's a raw v4 task_id (no prefix),
    list items under that. If None, list across all tasks.
    """
    try:
        from work_buddy.obsidian.tasks import action_items as _ai
        from work_buddy.obsidian.tasks import store as _task_store
    except Exception as e:
        logger.warning("Aggregator: cannot load action_items: %s", e)
        return []

    if task_id is not None and task_id.startswith(_TASK_PREFIX):
        task_id = task_id[len(_TASK_PREFIX):]

    if task_id is not None:
        try:
            rows = _ai.list_for_task(task_id, include_done=True)
        except Exception as e:
            logger.warning("Aggregator: action_items.list_for_task failed: %s", e)
            return []
        return [_action_item_row_to_thread(r) for r in rows]

    # No task filter → walk all tasks
    out: list[Thread] = []
    try:
        tasks = _task_store.query()
    except Exception:
        tasks = []
    for t in tasks:
        try:
            rows = _ai.list_for_task(t["task_id"], include_done=True)
        except Exception:
            continue
        out.extend(_action_item_row_to_thread(r) for r in rows)
    return out


def list_aggregated_pool_entries() -> list[Thread]:
    """Return ClarifyPool entries as Threads."""
    try:
        from work_buddy.clarify.background import ClarifyPool
    except Exception as e:
        logger.warning("Aggregator: cannot load ClarifyPool: %s", e)
        return []
    try:
        pool = ClarifyPool.default()
        entries = pool.all_entries()
    except Exception as e:
        logger.warning("Aggregator: pool listing failed: %s", e)
        return []
    return [_pool_entry_to_thread(e) for e in entries]


def list_threads_aggregated(
    *,
    parent_id: Optional[str] = None,
    subtype: Optional[str] = None,
    fsm_state: Optional[FSMState] = None,
) -> list[Thread]:
    """Unified read-only listing across all v4 sources.

    Filters apply post-conversion. Stage 1: simple in-memory filter.
    Stage 2 may add pushdown filtering for performance.
    """
    out: list[Thread] = []
    if parent_id is None or not parent_id.startswith(_TASK_PREFIX):
        # Tasks (top-level) are excluded if the caller is asking for
        # children of a specific Task.
        if parent_id is None:
            out.extend(list_aggregated_tasks())
            out.extend(list_aggregated_pool_entries())

    # Action items
    if parent_id is None or parent_id.startswith(_TASK_PREFIX):
        out.extend(list_aggregated_action_items(task_id=parent_id))

    # Apply post-filters
    if subtype is not None:
        out = [t for t in out if (t.subtype or None) == (subtype or None)]
    if fsm_state is not None:
        out = [t for t in out if t.fsm_state == fsm_state]
    if parent_id is not None:
        out = [t for t in out if t.parent_id == parent_id]
    return out


def get_thread_aggregated(thread_id: str) -> Optional[Thread]:
    """Resolve an aggregated thread ID back to a synthesized Thread."""
    try:
        if thread_id.startswith(_TASK_PREFIX):
            task_id = thread_id[len(_TASK_PREFIX):]
            from work_buddy.obsidian.tasks import store as _task_store
            row = _task_store.get(task_id)
            return _task_row_to_thread(row) if row else None
        if thread_id.startswith(_ACTION_ITEM_PREFIX):
            item_id = thread_id[len(_ACTION_ITEM_PREFIX):]
            from work_buddy.obsidian.tasks import action_items as _ai
            try:
                row = _ai.get(int(item_id))
            except (ValueError, TypeError):
                return None
            return _action_item_row_to_thread(row) if row else None
        if thread_id.startswith(_POOL_PREFIX):
            from work_buddy.clarify.background import ClarifyPool
            pool = ClarifyPool.default()
            for entry in pool.all_entries():
                pid = f"{_POOL_PREFIX}{entry.run_id}:{entry.item_id}"
                if pid == thread_id:
                    return _pool_entry_to_thread(entry)
            return None
    except Exception as e:
        logger.warning(
            "Aggregator: get_thread_aggregated(%s) failed: %s", thread_id, e,
        )
        return None
    return None


def aggregator_summary() -> dict[str, int]:
    """Lightweight head-count for diagnostics."""
    return {
        "tasks": len(list_aggregated_tasks()),
        "action_items": len(list_aggregated_action_items()),
        "pool_entries": len(list_aggregated_pool_entries()),
    }
