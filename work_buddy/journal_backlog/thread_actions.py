"""Per-thread journal actions — wrappers that walk a thread's
``context_items`` and route each one through the existing journal-
backlog primitives (``route.create_task``, ``route.create_consideration``,
``route.append_to_note``).

Backs the journal-specific entries in the per-source action library
(``work_buddy/pipelines/journal.py:JOURNAL_ACTIONS``). Each function
here is registered as a capability with ``is_action=True`` so the
LLM cluster-refinement step + the dashboard action chip can both pick
it as a proposal.

Continue-on-error semantics: each item is routed independently; a
single failure doesn't block the others. Returns a per-item result
list so the dashboard can surface partial success / failure counts
in the standard cascade-approve toast.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from work_buddy.threads import store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JournalThreadActionError(ValueError):
    """A journal thread-action precondition failed (thread not found,
    no items on thread, vault root missing, etc.)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_thread_or_raise(thread_id: str):
    thread = store.get_thread(thread_id)
    if thread is None:
        raise JournalThreadActionError(f"Thread {thread_id!r} not found")
    return thread


def _resolve_vault_root(vault_root: str | Path | None) -> Path:
    """Resolve a vault root path, falling back to ``config.yaml``'s
    ``vault_root`` setting if no explicit argument is given."""
    if vault_root is not None:
        return Path(vault_root)
    try:
        from work_buddy.config import load_config
        cfg = load_config()
        configured = cfg.get("vault_root")
        if not configured:
            raise JournalThreadActionError(
                "vault_root not provided and config.yaml's "
                "``vault_root`` is unset",
            )
        return Path(configured)
    except (ImportError, FileNotFoundError) as e:
        raise JournalThreadActionError(
            f"vault_root not provided and config not loadable: {e}",
        ) from e


def _items_from_thread(thread) -> list[dict[str, Any]]:
    """Return the thread's context items as a list of dicts. Empty
    list when the thread has none."""
    return [
        {
            "id": ci.id,
            "label": ci.label or "",
            "raw_text": (ci.payload or {}).get("raw_text") or "",
            "source_dates": (ci.payload or {}).get("source_dates"),
        }
        for ci in (thread.context_items or ())
    ]


# ---------------------------------------------------------------------------
# journal_route_to_tasks
# ---------------------------------------------------------------------------


def journal_route_to_tasks(
    thread_id: str,
    *,
    vault_root: str | Path | None = None,
    urgency: str = "medium",
    project: str | None = None,
) -> dict[str, Any]:
    """Create one task per ``context_item`` on ``thread_id``.

    Returns ``{"created": [...], "failed": [{"item_id", "error"}, ...],
    "thread_id": str}``.
    """
    from work_buddy.journal_backlog.route import _create_task_impl

    thread = _get_thread_or_raise(thread_id)
    items = _items_from_thread(thread)
    if not items:
        return {
            "thread_id": thread_id,
            "created": [],
            "failed": [],
            "skipped_empty": True,
        }
    root = _resolve_vault_root(vault_root)

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in items:
        text = item["label"] or item["raw_text"][:120] or item["id"]
        try:
            result = _create_task_impl(
                task_text=text,
                vault_root=root,
                urgency=urgency,
                project=project,
                due_date=None,
            )
            if result.get("success"):
                created.append({
                    "item_id": item["id"],
                    "task_line": result.get("task_line"),
                })
            else:
                failed.append({
                    "item_id": item["id"],
                    "error": result.get("message", "create_task returned success=False"),
                })
        except Exception as e:
            logger.warning(
                "journal_route_to_tasks: item %s failed: %s",
                item["id"], e,
            )
            failed.append({"item_id": item["id"], "error": str(e)})

    return {
        "thread_id": thread_id,
        "created": created,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# journal_route_to_considerations
# ---------------------------------------------------------------------------


def journal_route_to_considerations(
    thread_id: str,
    *,
    vault_root: str | Path | None = None,
    project: str = "inbox",
    type: str = "consideration",
    status: str = "open",
) -> dict[str, Any]:
    """Create one consideration note per ``context_item`` on
    ``thread_id``. The item's label becomes the title; raw_text
    becomes the body.

    Returns ``{"created": [...], "failed": [{...}], "thread_id": str}``.
    """
    from work_buddy.journal_backlog.route import _create_consideration_impl

    thread = _get_thread_or_raise(thread_id)
    items = _items_from_thread(thread)
    if not items:
        return {
            "thread_id": thread_id,
            "created": [],
            "failed": [],
            "skipped_empty": True,
        }
    root = _resolve_vault_root(vault_root)

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in items:
        title = item["label"] or item["raw_text"][:120] or item["id"]
        try:
            result = _create_consideration_impl(
                title=title,
                vault_root=root,
                project=project,
                type=type,
                status=status,
                body=item["raw_text"] or "",
                review_date=None,
            )
            if result.get("success"):
                created.append({
                    "item_id": item["id"],
                    "file": result.get("file"),
                })
            else:
                failed.append({
                    "item_id": item["id"],
                    "error": result.get("message", "create_consideration returned success=False"),
                })
        except Exception as e:
            logger.warning(
                "journal_route_to_considerations: item %s failed: %s",
                item["id"], e,
            )
            failed.append({"item_id": item["id"], "error": str(e)})

    return {
        "thread_id": thread_id,
        "created": created,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# journal_append_to_note
# ---------------------------------------------------------------------------


def journal_append_to_note(
    thread_id: str,
    *,
    note_path: str,
    vault_root: str | Path | None = None,
    bullet_prefix: str = "- ",
) -> dict[str, Any]:
    """Append all items from ``thread_id`` as bullets to a single
    existing vault note.

    Useful for journal items that are project notes / observations
    rather than tasks — append them all to the project's main note.

    Returns ``{"appended": [...], "failed": [{...}], "thread_id":
    str, "note_path": str}``.
    """
    from work_buddy.journal_backlog.route import _append_to_note_impl

    thread = _get_thread_or_raise(thread_id)
    items = _items_from_thread(thread)
    if not items:
        return {
            "thread_id": thread_id,
            "note_path": note_path,
            "appended": [],
            "failed": [],
            "skipped_empty": True,
        }
    root = _resolve_vault_root(vault_root)

    # Build one combined content blob with all items as bullets.
    # Each bullet uses the item's first line; the raw_text falls back
    # if the label is empty.
    bullets: list[str] = []
    for item in items:
        first = item["label"] or item["raw_text"].splitlines()[0:1] or [item["id"]]
        text = first if isinstance(first, str) else (first[0] if first else item["id"])
        bullets.append(f"{bullet_prefix}{text}")
    content = "\n".join(bullets)

    try:
        result = _append_to_note_impl(content, root, note_path)
    except Exception as e:
        logger.warning(
            "journal_append_to_note: failed: %s", e,
        )
        return {
            "thread_id": thread_id,
            "note_path": note_path,
            "appended": [],
            "failed": [
                {"item_id": item["id"], "error": str(e)}
                for item in items
            ],
        }

    if result.get("success"):
        return {
            "thread_id": thread_id,
            "note_path": note_path,
            "appended": [{"item_id": item["id"]} for item in items],
            "failed": [],
        }
    error = result.get("message", "append_to_note returned success=False")
    return {
        "thread_id": thread_id,
        "note_path": note_path,
        "appended": [],
        "failed": [
            {"item_id": item["id"], "error": error}
            for item in items
        ],
    }
