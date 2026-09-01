"""Bridge-free capability implementations for native task authority."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from .models import Task, TaskQuery
from .runtime import mutation_actor, originating_session
from .service import TaskApplicationService
from .store import TaskStore


def _task_dict(task: Task) -> dict[str, Any]:
    return task.to_dict()


def _document_context(store: TaskStore, task: Task) -> dict[str, Any]:
    link = store.get_task_document_link(task.task_id)
    if link is None:
        return {
            "note_uuid": task.note_uuid,
            "note_content": None,
            "knowledge_document": None,
        }
    content: str | None = None
    try:
        from work_buddy.tasks.documents import (
            TaskDocumentStoreManager,
            project_live_markdown,
        )

        cowork_store = TaskDocumentStoreManager().open_existing()
        if cowork_store.store_id == link.store_id:
            content = project_live_markdown(cowork_store, link.document_id)
    except Exception:
        content = None
    return {
        "note_uuid": link.note_uuid,
        # Compatibility alias: these are Co-work document contents, never a
        # task Markdown file or projection path.
        "note_content": content,
        "knowledge_document": {
            **link.to_dict(),
            "href": (
                f"/app/cowork?store_id={link.store_id}"
                f"&document_id={link.document_id}"
            ),
        },
    }


def task_read(task_id: str) -> dict[str, Any]:
    store = TaskStore()
    task = store.get(task_id)
    if task is None:
        return {"success": False, "error": "task_not_found", "task_id": task_id}
    sessions = [
        {"task_id": task_id, **assignment}
        for assignment in store.get_sessions(task_id)
    ]
    return {
        "success": True,
        "task_id": task.task_id,
        "task_text": task.description,
        "state": task.state,
        "urgency": task.urgency,
        "complexity": task.complexity,
        "contract": task.contract,
        "due_date": task.due_date,
        "has_deadline": task.has_deadline,
        "deadline_date": task.deadline_date,
        "metadata": _task_dict(task),
        "assigned_sessions": sessions,
        "created_by": task.created_by_session,
        **_document_context(store, task),
    }


def task_list(
    *,
    state: str | None = None,
    include_done: bool = False,
    include_archived: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    tasks = TaskStore().list(
        TaskQuery(
            state=state,
            include_done=include_done or state == "done",
            include_archived=include_archived,
            include_snoozed=state == "snoozed",
            limit=limit,
        )
    )
    return {"count": len(tasks), "tasks": [_task_dict(task) for task in tasks]}


def task_search(
    query: str,
    *,
    limit: int = 50,
    include_archived: bool = False,
    include_done: bool = True,
) -> dict[str, Any]:
    tasks = TaskStore().search(
        query,
        limit=limit,
        include_done=include_done,
        include_archived=include_archived,
    )
    return {
        "query": query,
        "count": len(tasks),
        "tasks": [_task_dict(task) for task in tasks],
    }


def review_inbox() -> list[dict[str, Any]]:
    tasks = TaskStore().list(TaskQuery(state="inbox", limit=5000))
    reviewed: list[dict[str, Any]] = []
    for task in tasks:
        action, reason = "snooze", "Low priority, no deadline"
        if task.urgency == "high" and not task.due_date:
            action, reason = "needs_date", "High urgency but no due date"
        elif task.urgency == "high" or task.due_date:
            action, reason = "mit", "Has urgency or a due date"
        reviewed.append(
            {
                "description": task.description,
                "due_date": task.due_date,
                "urgency": task.urgency,
                "task_id": task.task_id,
                "contract": task.contract,
                "suggested_action": action,
                "reason": reason,
            }
        )
    return reviewed


def stale_check() -> dict[str, Any]:
    today = date.today()
    tasks = TaskStore().list(
        TaskQuery(include_snoozed=True, include_done=False, limit=5000)
    )
    result: dict[str, list[dict[str, Any]]] = {
        "inbox_stale": [],
        "snoozed_forgotten": [],
        "mit_no_date": [],
        "focused_no_date": [],
        "focused_overdue": [],
    }
    for task in tasks:
        item = {
            "description": task.description,
            "task_id": task.task_id,
            "due_date": task.due_date,
        }
        created = datetime.fromisoformat(task.created_at.replace("Z", "+00:00")).date()
        if task.state == "inbox" and created <= today - timedelta(days=7):
            result["inbox_stale"].append({**item, "suggestion": "triage_needed"})
        elif task.state == "snoozed" and (
            not task.snooze_until or task.snooze_until <= today.isoformat()
        ):
            result["snoozed_forgotten"].append(
                {**item, "suggestion": "review_or_resume"}
            )
        elif task.state == "mit" and not task.due_date:
            result["mit_no_date"].append({**item, "suggestion": "set_due_date"})
        elif task.state == "focused" and not task.due_date:
            result["focused_no_date"].append(item)
        elif (
            task.state == "focused"
            and task.due_date
            and task.due_date < today.isoformat()
        ):
            result["focused_overdue"].append(item)
    return result


def daily_briefing() -> dict[str, Any]:
    store = TaskStore()
    tasks = store.list(
        TaskQuery(include_done=True, include_snoozed=True, limit=5000)
    )
    open_tasks = [task for task in tasks if task.state != "done"]
    by_state = Counter(task.state for task in tasks)
    stale = stale_check()
    overdue = [
        _task_dict(task)
        for task in open_tasks
        if task.due_date and task.due_date < date.today().isoformat()
    ]
    try:
        from work_buddy.contracts import check_wip_limit, get_constraints

        contracts = get_constraints()
        wip = check_wip_limit()
    except Exception:
        contracts, wip = [], {"within_limit": True}
    parts = [
        f"{by_state.get('mit', 0)} MITs",
        f"{by_state.get('focused', 0)} focused",
        f"{len(overdue)} overdue",
        f"{by_state.get('inbox', 0)} inbox",
    ]
    return {
        "contracts": contracts,
        "wip": wip,
        "mits": [_task_dict(task) for task in open_tasks if task.state == "mit"],
        "focused": [
            _task_dict(task) for task in open_tasks if task.state == "focused"
        ],
        "overdue": overdue,
        "stale": {f"{key}_count": len(value) for key, value in stale.items()},
        "inbox_count": by_state.get("inbox", 0),
        "archive_recommended": sum(
            1 for task in tasks if task.state == "done" and not task.archived_at
        )
        > 10,
        "store_counts": dict(by_state),
        "counts": {
            "total": len(tasks),
            "todo": len(open_tasks),
            "done": by_state.get("done", 0),
        },
        "summary_line": "Tasks: " + ", ".join(parts) + ".",
    }


def weekly_review_data() -> dict[str, Any]:
    briefing = daily_briefing()
    return {
        "contracts": briefing["contracts"],
        "wip": briefing["wip"],
        "task_state": briefing["counts"],
        "current_mits": briefing["mits"],
        "stale": stale_check(),
        "inbox_review": review_inbox(),
        "overdue": briefing["overdue"],
        "suggested_mits": [],
    }


def archive_completed(older_than_days: int = 7) -> dict[str, Any]:
    from .runtime import assert_task_mutations_allowed

    assert_task_mutations_allowed()
    store = TaskStore()
    service = TaskApplicationService(store)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, older_than_days))
    candidates = store.list(
        TaskQuery(include_done=True, include_archived=False, limit=5000)
    )
    batch_id = uuid.uuid4().hex
    archived: list[str] = []
    for task in candidates:
        if task.state != "done" or not task.completed_at:
            continue
        completed = datetime.fromisoformat(task.completed_at.replace("Z", "+00:00"))
        if completed > cutoff:
            continue
        result = service.archive(
            task.task_id,
            expected_revision=task.revision,
            client_mutation_id=f"native-archive:{batch_id}:{task.task_id}",
            actor=mutation_actor(),
            session_id=originating_session(),
        )
        if result.changed:
            archived.append(task.task_id)
    from .events import publish_pending_async

    publish_pending_async(store)
    return {
        "success": True,
        "archived_count": len(archived),
        "task_ids": archived,
        "older_than_days": max(0, older_than_days),
    }


def task_creation_reconcile(limit: int = 25) -> dict[str, Any]:
    """Boundedly roll hidden task/document aggregates through crash recovery."""

    from .aggregate_creation import reconcile_task_creation_intents
    from .runtime import assert_task_mutations_allowed

    assert_task_mutations_allowed()
    return reconcile_task_creation_intents(limit=max(1, min(int(limit), 100)))


def task_provenance(task_id: str) -> dict[str, Any]:
    store = TaskStore()
    task = store.get(task_id, include_deleted=True)
    if task is None:
        return {"task_id": task_id, "created_by": None, "assigned": [], "developed_by": []}
    assigned = [
        {"task_id": task_id, **assignment}
        for assignment in store.get_sessions(task_id)
    ]
    assigned_ids = {item["session_id"] for item in assigned}
    readers = {
        item["session_id"]: item
        for item in task_note_readers(task_id, note_uuid=task.note_uuid)
    }
    developed_by: list[dict[str, Any]] = []
    try:
        from work_buddy.conversation_observability import commits as commits_mod

        commit_rows = commits_mod.query_commits_for_task(task_id)
    except Exception:
        commit_rows = []
    by_session: dict[str, list[dict[str, Any]]] = {}
    for commit in commit_rows:
        session_id = str(commit.get("session_id") or "").strip()
        if session_id:
            by_session.setdefault(session_id, []).append(commit)
    for session_id, commits in by_session.items():
        assigned_session = session_id in assigned_ids
        awareness = (
            "assigned"
            if assigned_session
            else readers.get(session_id, {}).get("awareness", "not_computed")
        )
        developed_by.append(
            {
                "session_id": session_id,
                "rung": 1 if assigned_session else 2,
                "confidence": "high",
                "provenance": "assigned+commit" if assigned_session else "commit-ref",
                "awareness": awareness,
                "classification": (
                    "informed"
                    if awareness in {"assigned", "read_note"}
                    else "unknown"
                ),
                "evidence": [
                    {
                        "kind": "commit",
                        "sha": commit.get("hash"),
                        "committed_at": commit.get("timestamp"),
                        "message_excerpt": (commit.get("message") or "")
                        .strip()
                        .split("\n")[0][:120],
                    }
                    for commit in commits
                ],
            }
        )
    developed_by.sort(key=lambda item: (item["rung"], item["session_id"]))
    return {
        "task_id": task_id,
        "created_by": task.created_by_session,
        "assigned": assigned,
        "developed_by": developed_by,
        "intent_attribution": {
            "computed": False,
            "hook": "wb-task-completeness",
            "reason": (
                "Intent-level attribution without a task-id/assignment signal "
                "requires the completeness investigation workflow."
            ),
        },
        "authority": "native",
    }


def task_note_readers(
    task_id: str,
    note_uuid: str | None = None,
    include_saw_id: bool = False,
) -> list[dict[str, Any]]:
    # The durable table records explicit task_read/task_assign calls as well
    # as historical note opens.  Native mode consumes those receipts without
    # consulting task Markdown or scanning the retained tree.
    try:
        from work_buddy.conversation_observability import note_reads

        rows = note_reads.query_reads_for_task(task_id)
    except Exception:
        return []
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        session_id = str(row.get("session_id") or "").strip()
        if not session_id:
            continue
        slot = grouped.setdefault(
            session_id,
            {
                "session_id": session_id,
                "awareness": "read_note",
                "sources": {},
                "first_seen": None,
                "last_seen": None,
            },
        )
        source = str(row.get("source") or "task_read_mcp")
        slot["sources"][source] = {
            "first": row.get("first_seen_at"),
            "last": row.get("last_seen_at"),
            "count": row.get("occurrence_count", 1),
        }
        first = row.get("first_seen_at")
        last = row.get("last_seen_at")
        if first and (slot["first_seen"] is None or first < slot["first_seen"]):
            slot["first_seen"] = first
        if last and (slot["last_seen"] is None or last > slot["last_seen"]):
            slot["last_seen"] = last
    result = list(grouped.values())
    result.sort(key=lambda item: item["last_seen"] or "", reverse=True)
    return result


def session_task_roles(session_id: str) -> dict[str, Any]:
    """Return native task relationships for a dashboard/session rail."""

    store = TaskStore()
    assigned_at = {
        item["task_id"]: item.get("assigned_at")
        for item in store.get_tasks_for_session(session_id)
    }
    created_ids: set[str] = set()
    conn = store.connect()
    try:
        created_ids = {
            str(row["task_id"])
            for row in conn.execute(
                "SELECT task_id FROM task_metadata "
                "WHERE created_by_session = ? AND deleted_at IS NULL",
                (session_id,),
            )
        }
    finally:
        conn.close()
    developed_ids: set[str] = set()
    try:
        from work_buddy.conversation_observability import commits as commits_mod

        for commit in commits_mod.query_session_commits(session_id=session_id):
            developed_ids.update(re.findall(r"t-[0-9a-f]{8}", commit.get("message") or ""))
    except Exception:
        pass
    items: list[dict[str, Any]] = []
    for task_id in sorted(created_ids | set(assigned_at) | developed_ids):
        task = store.get(task_id)
        roles: list[str] = []
        if task_id in created_ids:
            roles.append("created")
        if task_id in assigned_at:
            roles.append("assigned")
        if task_id in developed_ids:
            roles.append("developed")
        items.append(
            {
                "task_id": task_id,
                "task_text": None if task is None else task.description,
                "state": None if task is None else task.state,
                "roles": roles,
                "assigned_at": assigned_at.get(task_id),
            }
        )
    return {"session_id": session_id, "tasks": items}


def session_tasks_get(session_id: str) -> dict[str, Any]:
    store = TaskStore()
    items: list[dict[str, Any]] = []
    for assignment in store.get_tasks_for_session(session_id):
        task = store.get(str(assignment["task_id"]), include_deleted=True)
        items.append(
            {
                "task_id": assignment["task_id"],
                "assigned_at": assignment.get("assigned_at"),
                "task_text": None if task is None else task.description,
                "state": None if task is None else task.state,
            }
        )
    return {"tasks": items}


def _namespace_counts() -> Counter[str]:
    tasks = TaskStore().list(
        TaskQuery(
            include_done=True,
            include_archived=True,
            include_snoozed=True,
            limit=5000,
        )
    )
    return Counter(tag for task in tasks for tag in task.namespace_tags)


def namespace_lookup(query: str, *, limit: int = 5) -> dict[str, Any]:
    needle = query.strip().strip("#/").casefold()
    counts = _namespace_counts()
    ranked = sorted(
        counts,
        key=lambda tag: (
            -SequenceMatcher(None, needle, tag.casefold()).ratio(),
            -counts[tag],
            tag,
        ),
    )[: max(1, min(limit, 50))]
    matches = [
        {
            "tag": tag,
            "count": counts[tag],
            "recent_count": 0,
            "score": round(
                SequenceMatcher(None, needle, tag.casefold()).ratio(), 4
            ),
            "method": "tokens",
            "exists": True,
        }
        for tag in ranked
    ]
    return {
        "query": query.strip().lstrip("#"),
        "exact_match": any(tag.casefold() == needle for tag in counts),
        "universe_size": len(counts),
        "service_used": "tokens" if matches else "none",
        "matches": [
            item for item in matches
        ],
    }


def task_namespace_suggest(
    task_text: str,
    *,
    contract: str | None = None,
    project: str | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    tokens = set(re.findall(r"[a-z0-9]+", task_text.casefold()))
    tokens.update(re.findall(r"[a-z0-9]+", (contract or "").casefold()))
    tokens.update(re.findall(r"[a-z0-9]+", (project or "").casefold()))
    counts = _namespace_counts()
    scored: list[tuple[float, str]] = []
    for tag, count in counts.items():
        tag_tokens = set(re.findall(r"[a-z0-9]+", tag.casefold()))
        overlap = len(tokens & tag_tokens) / max(1, len(tokens | tag_tokens))
        scored.append((overlap + min(count, 20) / 1000, tag))
    scored.sort(key=lambda item: (-item[0], item[1]))
    suggestions = [
        {
            "tag": tag,
            "score": round(score, 4),
            "count": counts[tag],
            "recent_count": 0,
            "method": "tokens",
            "exists": True,
        }
        for score, tag in scored[: max(1, min(limit, 25))]
        if score > 0
    ]
    return {
        "suggestions": suggestions,
        "universe_size": len(counts),
        "service_used": "tokens" if suggestions else "none",
    }


def _project_status(plan: dict[str, Any], counts: Counter[str]) -> dict[str, Any]:
    """Return project-registry and existing-subtree context for task-new."""
    try:
        from work_buddy.projects.store import list_projects

        known = list_projects()
    except Exception:
        known = []

    proposed_slug: str | None = None
    raw_project = plan.get("project")
    if isinstance(raw_project, str) and raw_project.strip():
        proposed_slug = raw_project.strip().casefold()

    full_project_tag: str | None = None
    proposed_tags = plan.get("proposed_tags")
    if isinstance(proposed_tags, list):
        for raw in proposed_tags:
            if not isinstance(raw, str):
                continue
            tag = raw.strip().lstrip("#").casefold()
            if not tag.startswith("projects/"):
                continue
            full_project_tag = tag
            if proposed_slug is None:
                parts = tag.split("/", 2)
                proposed_slug = parts[1] if len(parts) > 1 else None
            break

    slug_exists = bool(
        proposed_slug
        and any(
            str(project.get("slug") or "").casefold() == proposed_slug
            for project in known
        )
    )
    prefix = f"projects/{proposed_slug}/" if proposed_slug else None
    near_subtrees = sorted(
        tag for tag in counts if prefix is not None and tag.casefold().startswith(prefix)
    )
    subtree_matches: list[dict[str, Any]] = []
    if full_project_tag and full_project_tag.count("/") >= 2:
        subtree_matches = namespace_lookup(full_project_tag, limit=5)["matches"]
    return {
        "known_projects": [
            {
                "slug": project.get("slug"),
                "name": project.get("name"),
                "status": project.get("status"),
            }
            for project in known
            if project.get("slug")
        ],
        "proposed_slug": proposed_slug,
        "slug_exists": slug_exists,
        "near_subtrees": near_subtrees,
        "subtree_matches": subtree_matches,
    }


def enrich_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Enrich task-new input from native task tags and the project registry."""
    empty_project = {
        "known_projects": [],
        "proposed_slug": None,
        "slug_exists": False,
        "near_subtrees": [],
        "subtree_matches": [],
    }
    if not isinstance(plan, dict):
        return {
            "plan": {},
            "suggestions": [],
            "tag_status": {},
            "project_status": empty_project,
            "universe_size": 0,
            "error": f"plan must be a dict, got {type(plan).__name__}",
        }

    counts = _namespace_counts()
    suggestions = task_namespace_suggest(
        str(plan.get("task_text") or "").strip(),
        contract=plan.get("contract"),
        project=plan.get("project"),
        limit=5,
    )
    proposed = plan.get("proposed_tags")
    tag_status: dict[str, Any] = {}
    if isinstance(proposed, list):
        for raw in proposed:
            if not isinstance(raw, str):
                continue
            tag = raw.strip().lstrip("#")
            if not tag:
                continue
            lookup = namespace_lookup(tag, limit=3)
            tag_status[tag] = {
                "exists": bool(lookup["exact_match"]),
                "near_matches": lookup["matches"],
            }
    return {
        "plan": plan,
        "suggestions": suggestions["suggestions"],
        "tag_status": tag_status,
        "project_status": _project_status(plan, counts),
        "universe_size": len(counts),
        "service_used": suggestions["service_used"],
    }


def retired_legacy_surface(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "success": False,
        "retired": True,
        "error": "This older task command is no longer available. Use the Tasks view instead.",
    }


__all__ = [
    "archive_completed",
    "daily_briefing",
    "enrich_plan",
    "namespace_lookup",
    "retired_legacy_surface",
    "review_inbox",
    "session_task_roles",
    "session_tasks_get",
    "stale_check",
    "task_list",
    "task_namespace_suggest",
    "task_note_readers",
    "task_provenance",
    "task_read",
    "task_search",
    "weekly_review_data",
]
