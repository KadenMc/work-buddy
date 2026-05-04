"""Per-thread Chrome actions — wrappers that walk a Chrome-group
thread's ``context_items`` and route each tab through the existing
task-creation primitives.

Backs the ``chrome_route_to_tasks`` + ``chrome_route_to_umbrella_task``
entries in the Chrome action library. The other Chrome actions
(close-tabs / group / move) call the chrome_collector mutators
directly — they don't need a thread-level wrapper because their
target is the tab id, not the thread.

Continue-on-error semantics: each item is routed independently; a
single failure doesn't block the others.
"""

from __future__ import annotations

import logging
from typing import Any

from work_buddy.threads import store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChromeThreadActionError(ValueError):
    """A Chrome thread-action precondition failed (thread not found,
    no tabs on thread, etc.)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_thread_or_raise(thread_id: str):
    thread = store.get_thread(thread_id)
    if thread is None:
        raise ChromeThreadActionError(f"Thread {thread_id!r} not found")
    return thread


def _tabs_from_thread(thread) -> list[dict[str, Any]]:
    """Return the thread's context items as a list of tab dicts.
    Filters to items with source=='chrome_tab' so a stray non-tab
    ContextItem doesn't poison the action."""
    out: list[dict[str, Any]] = []
    for ci in (thread.context_items or ()):
        if ci.source != "chrome_tab":
            continue
        payload = ci.payload or {}
        out.append({
            "id": ci.id,
            "title": payload.get("title") or ci.label,
            "url": payload.get("url") or "",
            "tab_id": payload.get("tab_id"),
        })
    return out


# ---------------------------------------------------------------------------
# chrome_route_to_tasks — one task per tab
# ---------------------------------------------------------------------------


def chrome_route_to_tasks(
    thread_id: str,
    *,
    urgency: str = "medium",
    project: str | None = None,
) -> dict[str, Any]:
    """Walk a Chrome-group thread's tabs and create one task per
    tab. The task text uses the tab title; the URL goes into the
    task's summary (linked note).

    Returns ``{"created": [...], "failed": [{...}], "thread_id": str}``.
    """
    from work_buddy.obsidian.tasks.mutations import create_task

    thread = _get_thread_or_raise(thread_id)
    tabs = _tabs_from_thread(thread)
    if not tabs:
        return {
            "thread_id": thread_id,
            "created": [],
            "failed": [],
            "skipped_empty": True,
        }

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for tab in tabs:
        text = (tab["title"] or tab["url"] or tab["id"])[:120]
        summary = f"From Chrome tab: {tab['url']}" if tab["url"] else None
        try:
            result = create_task(
                task_text=text,
                urgency=urgency,
                project=project,
                summary=summary,
                creation_provenance="agent_inferred_from_chrome",
                user_involvement="medium",
            )
            if result.get("success"):
                created.append({
                    "item_id": tab["id"],
                    "task_line": result.get("task_line"),
                })
            else:
                failed.append({
                    "item_id": tab["id"],
                    "error": result.get("message", "create_task returned success=False"),
                })
        except Exception as e:
            logger.warning(
                "chrome_route_to_tasks: tab %s failed: %s",
                tab["id"], e,
            )
            failed.append({"item_id": tab["id"], "error": str(e)})

    return {
        "thread_id": thread_id,
        "created": created,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# chrome_route_to_umbrella_task — one task for the whole group
# ---------------------------------------------------------------------------


def chrome_route_to_umbrella_task(
    thread_id: str,
    *,
    urgency: str = "medium",
    project: str | None = None,
    title_override: str | None = None,
) -> dict[str, Any]:
    """Create a single task representing the whole Chrome group.

    Task text uses the cluster's label (or ``title_override``);
    description lists every tab's title + URL so the user can pick
    up the context later.
    """
    from work_buddy.obsidian.tasks.mutations import create_task

    thread = _get_thread_or_raise(thread_id)
    tabs = _tabs_from_thread(thread)
    if not tabs:
        return {
            "thread_id": thread_id,
            "created": None,
            "failed": [],
            "skipped_empty": True,
        }

    if title_override:
        text = title_override
    else:
        # Prefer the thread's inciting summary title (cluster label)
        # over a generic fallback.
        ie = thread.inciting_event_summary or {}
        text = ie.get("title") or ie.get("description") or "Chrome session"

    bullet_lines = []
    for tab in tabs:
        title = tab["title"] or tab["id"]
        url = tab["url"]
        if url:
            bullet_lines.append(f"- [{title}]({url})")
        else:
            bullet_lines.append(f"- {title}")
    summary = "Chrome tabs in this group:\n\n" + "\n".join(bullet_lines)

    try:
        result = create_task(
            task_text=text[:120],
            urgency=urgency,
            project=project,
            summary=summary,
            creation_provenance="agent_inferred_from_chrome",
            user_involvement="medium",
        )
    except Exception as e:
        logger.warning(
            "chrome_route_to_umbrella_task: %s", e,
        )
        return {
            "thread_id": thread_id,
            "created": None,
            "failed": [{"item_id": "umbrella", "error": str(e)}],
        }

    if not result.get("success"):
        return {
            "thread_id": thread_id,
            "created": None,
            "failed": [{
                "item_id": "umbrella",
                "error": result.get("message", "create_task returned success=False"),
            }],
        }
    return {
        "thread_id": thread_id,
        "created": {
            "task_line": result.get("task_line"),
            "tab_count": len(tabs),
        },
        "failed": [],
    }
