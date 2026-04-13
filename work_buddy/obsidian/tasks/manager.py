"""Task intelligence — briefing, staleness detection, focus suggestions.

Pure functions, no class, no state. Designed for short interventions
that surface what matters and suggest one next action.

Strategic layer: connects daily task selection to contract constraints
via Theory of Constraints thinking. The weekly review produces MITs
(Most Important Tasks) that trace to specific contract bottlenecks.

Task state lifecycle: inbox → mit → focused → done
  - inbox: captured but not triaged
  - mit: selected for this week's plan (implementation intention)
  - focused: actively being worked on right now
  - snoozed: deferred, needs periodic wake-up check
  - done: completed
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks.env import (
    get_overdue_tasks,
    get_task_counts,
    query_tasks,
)
from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks.mutations import TASK_ID_RE

logger = get_logger(__name__)

# ── Staleness thresholds ────────────────────────────────────────

INBOX_STALE_DAYS = 7
SNOOZED_STALE_DAYS = 14
KILL_DECISION_DAYS = 30

MASTER_TASK_FILE = "tasks/master-task-list.md"


# ── Strategic layer ─────────────────────────────────────────────


def weekly_review_data() -> dict[str, Any]:
    """Assemble everything the weekly review agent needs to draft a plan.

    This is the data-gathering step. The agent uses this to propose MITs
    and constraint updates for the user to validate.

    Returns:
        Dict with:
        - contracts: active contracts with constraints and progress
        - wip: WIP limit status
        - task_state: current task counts and distribution
        - current_mits: tasks already in MIT state
        - stale: staleness report
        - inbox_review: inbox tasks with suggested actions
        - overdue: overdue tasks
        - suggested_mits: constraint-informed suggestions for this week
    """
    from work_buddy.contracts import (
        get_constraints,
        check_wip_limit,
        active_contracts,
    )

    contracts = get_constraints()
    wip = check_wip_limit()
    counts = get_task_counts()
    current_mits = get_tasks_by_state("mit")
    stale = stale_check()
    inbox = review_inbox()
    overdue = get_overdue_tasks()
    suggestions = suggest_focus(max_suggestions=5)

    return {
        "contracts": contracts,
        "wip": wip,
        "task_state": {
            "total": counts.get("total", 0),
            "todo": counts.get("todo", 0),
            "done": counts.get("done", 0),
            "by_tag": counts.get("by_tag", {}),
        },
        "current_mits": current_mits,
        "stale": stale,
        "inbox_review": inbox,
        "overdue": overdue.get("tasks", []),
        "suggested_mits": suggestions,
    }


# ── Intelligence functions ──────────────────────────────────────


def daily_briefing() -> dict[str, Any]:
    """Produce a structured task status summary for daily consumption.

    Includes contract constraints so daily task selection is
    strategically informed, not just urgency-driven.

    Returns:
        Dict with:
        - contracts: active contracts with their current constraints
        - mits: tasks in MIT state (this week's plan)
        - focused: tasks currently being worked on
        - overdue: overdue tasks
        - stale: staleness summary (counts only, not full lists)
        - inbox_count: int
        - archive_recommended: bool
        - suggested_focus: constraint-informed suggestions
        - summary_line: one-sentence human summary
    """
    # Contract layer
    try:
        from work_buddy.contracts import get_constraints, check_wip_limit
        contracts = get_constraints()
        wip = check_wip_limit()
    except Exception:
        contracts = []
        wip = {"within_limit": True, "active_count": 0, "limit": 3}

    # Project layer
    projects: list[dict] = []
    try:
        from work_buddy.projects.store import list_projects
        raw_projects = list_projects()
        projects = [
            {
                "slug": p["slug"],
                "name": p.get("name", p["slug"]),
                "status": p["status"],
            }
            for p in raw_projects
            if p.get("status") in ("active", "inferred")
        ]
    except Exception:
        pass

    # Task layer (store-aware)
    counts = get_task_counts()
    store_counts = store.counts_by_state()
    focused_tasks = get_tasks_by_state("focused")
    mit_tasks = get_tasks_by_state("mit")
    overdue = get_overdue_tasks()
    stale = stale_check()
    suggestions = suggest_focus()

    # Estimate done tasks in master list
    master_total = counts.get("by_file", {}).get(MASTER_TASK_FILE, 0)
    master_todo_result = query_tasks(status="TODO", file_path=MASTER_TASK_FILE, limit=500)
    master_todo = master_todo_result.get("total_matched", 0)
    master_done = master_total - master_todo

    # Build summary line
    parts = []
    if contracts:
        parts.append(f"{len(contracts)} active contract{'s' if len(contracts) != 1 else ''}")
    mit_count = len(mit_tasks)
    if mit_count > 0:
        parts.append(f"{mit_count} MIT{'s' if mit_count != 1 else ''}")
    if focused_tasks:
        parts.append(f"{len(focused_tasks)} focused")
    if overdue.get("count", 0) > 0:
        parts.append(f"{overdue['count']} overdue")
    stale_total = (len(stale.get("inbox_stale", []))
                   + len(stale.get("snoozed_forgotten", [])))
    if stale_total > 0:
        parts.append(f"{stale_total} stale")
    inbox_count = get_inbox_count()
    parts.append(f"{inbox_count} inbox")

    summary_line = "Tasks: " + ", ".join(parts) + "." if parts else "No active tasks."

    return {
        "contracts": contracts,
        "wip": wip,
        "projects": projects,
        "mits": mit_tasks,
        "store_counts": store_counts,
        "focused": focused_tasks,
        "overdue": overdue.get("tasks", []),
        "stale": {
            "inbox_stale_count": len(stale.get("inbox_stale", [])),
            "snoozed_forgotten_count": len(stale.get("snoozed_forgotten", [])),
            "focused_no_date_count": len(stale.get("focused_no_date", [])),
            "focused_overdue_count": len(stale.get("focused_overdue", [])),
        },
        "inbox_count": inbox_count,
        "archive_recommended": master_done > 10,
        "suggested_focus": suggestions,
        "counts": {
            "total": counts.get("total", 0),
            "todo": counts.get("todo", 0),
            "done": counts.get("done", 0),
        },
        "summary_line": summary_line,
    }


def stale_check() -> dict[str, Any]:
    """Find tasks that have gone stale — forgotten inbox, abandoned snooze.

    Also checks MIT tasks without due dates (unbounded weekly commitment).

    Returns:
        Dict with:
        - inbox_stale: tasks in inbox that need triage
        - snoozed_forgotten: snoozed tasks with no wake-up
        - mit_no_date: MIT tasks without a due date
        - focused_no_date: focused tasks with no due date
        - focused_overdue: focused tasks past their due date
    """
    today = date.today()

    result = query_tasks(status="TODO", file_path=MASTER_TASK_FILE, limit=500)
    tasks = result.get("tasks", [])

    inbox_stale: list[dict] = []
    snoozed_forgotten: list[dict] = []
    mit_no_date: list[dict] = []
    focused_no_date: list[dict] = []
    focused_overdue: list[dict] = []

    for t in tasks:
        meta = _get_task_meta(t)
        state = meta["state"]
        due = t.get("due_date")
        due_dt = _parse_date(due) if due else None

        if state == "inbox":
            inbox_stale.append({
                "description": t["description"],
                "due_date": due,
                "task_id": meta["task_id"],
                "suggestion": "triage_needed",
            })

        elif state == "snoozed":
            snoozed_forgotten.append({
                "description": t["description"],
                "due_date": due,
                "task_id": meta["task_id"],
                "suggestion": "review_or_kill",
            })

        elif state == "mit":
            if not due_dt:
                mit_no_date.append({
                    "description": t["description"],
                    "task_id": meta["task_id"],
                    "suggestion": "set_due_date",
                })

        elif state == "focused":
            if due_dt and due_dt < today:
                focused_overdue.append({
                    "description": t["description"],
                    "due_date": due,
                    "task_id": meta["task_id"],
                    "days_overdue": (today - due_dt).days,
                    "suggestion": "overdue",
                })
            elif not due_dt:
                focused_no_date.append({
                    "description": t["description"],
                    "task_id": meta["task_id"],
                    "suggestion": "set_due_date",
                })

    return {
        "inbox_stale": inbox_stale,
        "snoozed_forgotten": snoozed_forgotten,
        "mit_no_date": mit_no_date,
        "focused_no_date": focused_no_date,
        "focused_overdue": focused_overdue,
    }


def suggest_focus(max_suggestions: int = 3) -> list[dict[str, Any]]:
    """Recommend which tasks to focus on, informed by contract constraints.

    Priority scoring:
    1. Overdue tasks (highest)
    2. MIT tasks (already selected for the week)
    3. High urgency tasks
    4. Tasks linked to constrained contracts
    5. Due within 3 days
    6. Medium urgency inbox tasks

    Returns:
        List of task dicts with 'reason' and 'score' fields.
    """
    today = date.today()
    soon = today + timedelta(days=3)

    # Get contract constraints for cross-referencing
    constraint_projects: set[str] = set()
    try:
        from work_buddy.contracts import get_constraints
        for c in get_constraints():
            if c.get("constraint") and c["constraint"] != "Not set":
                # Extract project tag from contract title
                title_slug = c["title"].lower().replace(" ", "-").replace("_", "-")
                constraint_projects.add(title_slug)
    except Exception:
        pass

    result = query_tasks(status="TODO", file_path=MASTER_TASK_FILE, limit=500)
    tasks = result.get("tasks", [])

    scored: list[tuple[int, dict]] = []

    for t in tasks:
        meta = _get_task_meta(t)
        state = meta["state"]
        urgency = meta["urgency"]
        contract = meta.get("contract")
        due = t.get("due_date")
        due_dt = _parse_date(due) if due else None

        # Extract project from tags (stays inline)
        project = _extract_tag_value(t.get("tags", []), "#projects/")

        # Skip already-focused tasks
        if state == "focused":
            continue

        reason = ""
        score = 0

        if due_dt and due_dt < today:
            score = 100
            reason = f"Overdue by {(today - due_dt).days} days"
        elif state == "mit":
            score = 90
            reason = "Selected as MIT for this week"
        elif urgency == "high":
            score = 80
            reason = "High urgency"
        elif contract and contract in constraint_projects:
            score = 75
            reason = f"Linked to constrained contract ({contract})"
        elif project and project in constraint_projects:
            score = 75
            reason = f"Linked to constrained contract ({project})"
        elif due_dt and due_dt <= soon:
            score = 70
            days_left = (due_dt - today).days
            reason = f"Due in {days_left} day{'s' if days_left != 1 else ''}"
        elif urgency == "medium" and state == "inbox":
            score = 40
            reason = "Medium urgency, needs triage"

        if score > 0:
            scored.append((score, {
                "description": t["description"],
                "due_date": due,
                "urgency": urgency,
                "state": state,
                "project": project,
                "contract": contract,
                "task_id": meta["task_id"],
                "reason": reason,
                "score": score,
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in scored[:max_suggestions]]


def review_inbox() -> list[dict[str, Any]]:
    """For each inbox task, suggest one action with reasoning.

    Actions:
    - 'mit': high urgency or has a due date -> promote to MIT
    - 'snooze': low urgency, no date -> can wait
    - 'kill': stale, force a keep/kill decision
    - 'needs_date': high urgency but no due date set

    Returns:
        List of task dicts with 'suggested_action' and 'reason' fields.
    """
    tasks = get_tasks_by_state("inbox")

    reviewed: list[dict[str, Any]] = []

    for t in tasks:
        # get_tasks_by_state already resolved metadata
        urgency = t.get("urgency")
        due = t.get("due_date")

        action = "snooze"
        reason = "Low priority, no deadline"

        if urgency == "high" and not due:
            action = "needs_date"
            reason = "High urgency but no due date -- set a deadline or demote"
        elif urgency == "high" or due:
            action = "mit"
            reason = "Has urgency or a deadline -- promote to MIT for this week"
        elif urgency == "low":
            action = "snooze"
            reason = "Low urgency -- snooze until relevant"

        reviewed.append({
            "description": t["description"],
            "due_date": due,
            "urgency": urgency,
            "task_id": t.get("task_id"),
            "contract": t.get("contract"),
            "suggested_action": action,
            "reason": reason,
        })

    return reviewed


# ── Store-aware queries ─────────────────────────────────────────


def get_tasks_by_state(target_state: str) -> list[dict[str, Any]]:
    """Get all TODO tasks in a given state, combining store + tag fallback.

    This is the authoritative way to query by state. It:
    1. Gets all TODO tasks from the master list via plugin cache
    2. Resolves each task's state from store (preferred) or inline tags (fallback)
    3. Returns only those matching target_state
    """
    result = query_tasks(status="TODO", file_path=MASTER_TASK_FILE, limit=500)
    tasks = result.get("tasks", [])

    matched = []
    for t in tasks:
        meta = _get_task_meta(t)
        if meta["state"] == target_state:
            matched.append({
                **t,
                "task_id": meta["task_id"],
                "urgency": meta["urgency"],
                "contract": meta.get("contract"),
                "meta_source": meta["source"],
            })
    return matched


def get_inbox_count() -> int:
    """Get total inbox task count from both store and inline tags."""
    return len(get_tasks_by_state("inbox"))


# ── Helpers ─────────────────────────────────────────────────────


def _parse_date(date_str: str | None) -> date | None:
    """Parse a YYYY-MM-DD string into a date object."""
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return None


def _extract_tag_value(tags: list[str], prefix: str) -> str | None:
    """Extract the value portion of a hierarchical tag.

    E.g., _extract_tag_value(['#tasker/state/inbox'], '#tasker/state/') -> 'inbox'
    """
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return None


def _get_task_meta(task: dict[str, Any]) -> dict[str, str | None]:
    """Get state and urgency for a task, checking store first then inline tags.

    Returns dict with 'state', 'urgency', 'task_id', 'contract'.
    """
    tags = task.get("tags", [])
    desc = task.get("description", "")

    # Try to extract task ID
    task_id = None
    m = TASK_ID_RE.search(desc)
    if m:
        task_id = m.group(1)

    # Check store first (if task has an ID)
    if task_id:
        meta = store.get(task_id)
        if meta:
            return {
                "state": meta["state"],
                "urgency": meta["urgency"],
                "task_id": task_id,
                "contract": meta.get("contract"),
                "complexity": meta.get("complexity"),
                "source": "store",
            }

    # Fallback: inline tags (old tasks without store records)
    return {
        "state": _extract_tag_value(tags, "#tasker/state/"),
        "urgency": _extract_tag_value(tags, "#tasker/urgency/"),
        "task_id": task_id,
        "contract": None,
        "complexity": _extract_tag_value(tags, "#tasker/complexity/"),
        "source": "tags",
    }
