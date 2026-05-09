"""Per-thread Email actions — wrappers that walk an Email-group
thread's ``context_items`` and route each message through the existing
task-creation primitives.

Backs the ``email_close``, ``email_create_tasks``, and
``email_create_umbrella_task`` entries in the Email pipeline's action
library. All take a ``thread_id`` and read its ContextItems (source
``"email_message"``) — the pipeline's spawn step has already attached
one ContextItem per email to the group sub-thread.

Continue-on-error semantics: each item is routed independently; a
single failure doesn't block the others.

Why not bridge mutations? The Thunderbird bridge is read-first in v1
(see ``email/`` integration unit). ``email_close`` is therefore
*advisory* — it dismisses the Thread without touching the underlying
mailbox. Once the bridge grows ``archive`` / ``move`` / ``delete``
permissions, the closer side effect would join here behind a consent
gate.

A fourth action, ``email_record_into_task`` (file the cluster as
context on an existing task), is a near-future addition. It needs a
per-task note-append primitive that's currently missing from the
shared task primitives surface; out of scope for Phase 1.
"""

from __future__ import annotations

import logging
from typing import Any

from work_buddy.threads import store
from work_buddy.threads.universal_actions import thread_dismiss

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmailThreadActionError(ValueError):
    """An Email thread-action precondition failed (thread not found,
    no email items on thread, target task missing, etc.)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_thread_or_raise(thread_id: str):
    thread = store.get_thread(thread_id)
    if thread is None:
        raise EmailThreadActionError(f"Thread {thread_id!r} not found")
    return thread


def _emails_from_thread(thread) -> list[dict[str, Any]]:
    """Return the thread's context items as a list of email dicts.
    Filters to items with source=='email_message' so a stray non-email
    ContextItem doesn't poison the action."""
    out: list[dict[str, Any]] = []
    for ci in (thread.context_items or ()):
        if ci.source != "email_message":
            continue
        payload = ci.payload or {}
        out.append({
            "id": ci.id,
            "label": ci.label or payload.get("subject") or ci.id,
            "subject": payload.get("subject") or ci.label or "(no subject)",
            "sender": payload.get("sender") or "",
            "date": payload.get("date") or "",
            "stable_key": payload.get("stable_key"),
            "rfc_message_id": payload.get("rfc_message_id"),
            "provider_message_id": payload.get("provider_message_id"),
            "folder_path": payload.get("folder_path") or "",
        })
    return out


def _email_summary_lines(emails: list[dict[str, Any]]) -> list[str]:
    """Format a bullet list of emails for inclusion in a task note."""
    lines: list[str] = []
    for e in emails:
        subject = (e["subject"] or "(no subject)")[:120]
        sender = e["sender"]
        date = e["date"]
        bits = [subject]
        if sender:
            bits.append(f"from {sender}")
        if date:
            bits.append(f"({date})")
        lines.append(f"- {' '.join(bits)}")
    return lines


# ---------------------------------------------------------------------------
# email_close — advisory dismiss of the cluster
# ---------------------------------------------------------------------------


def email_close(
    thread_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark an email-cluster Thread as not actionable.

    Routes through the universal :func:`thread_dismiss` so the Thread
    transitions cleanly. The underlying mail itself is NOT mutated —
    the bridge is read-first in v1. Once the extension supports
    archive/move, this is where that side effect would attach behind
    consent.

    Returns ``{"thread_id": str, "previous_state": str, "new_state": "dismissed"}``.
    """
    return thread_dismiss(
        thread_id,
        reason=reason or "email_close: cluster marked not actionable",
    )


# ---------------------------------------------------------------------------
# email_create_tasks — one task per email in the cluster
# ---------------------------------------------------------------------------


def email_create_tasks(
    thread_id: str,
    *,
    urgency: str = "medium",
    project: str | None = None,
) -> dict[str, Any]:
    """Walk an email-cluster thread and create one task per email.

    Each task's text uses the email subject; the sender + date land
    in the linked summary note for context.

    Returns ``{"thread_id": str, "created": [...], "failed": [...]}``.
    """
    from work_buddy.obsidian.tasks.mutations import create_task

    thread = _get_thread_or_raise(thread_id)
    emails = _emails_from_thread(thread)
    if not emails:
        return {
            "thread_id": thread_id,
            "created": [],
            "failed": [],
            "skipped_empty": True,
        }

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for email in emails:
        text = (email["subject"] or email["id"])[:120]
        summary_parts = []
        if email["sender"]:
            summary_parts.append(f"From: {email['sender']}")
        if email["date"]:
            summary_parts.append(f"Date: {email['date']}")
        if email["folder_path"]:
            summary_parts.append(f"Folder: {email['folder_path']}")
        summary = "\n".join(summary_parts) if summary_parts else None

        try:
            result = create_task(
                task_text=text,
                urgency=urgency,
                project=project,
                summary=summary,
                creation_provenance="agent_inferred_from_email",
                user_involvement="medium",
            )
            if result.get("success"):
                created.append({
                    "item_id": email["id"],
                    "task_line": result.get("task_line"),
                })
            else:
                failed.append({
                    "item_id": email["id"],
                    "error": result.get("message", "create_task returned success=False"),
                })
        except Exception as e:
            logger.warning(
                "email_create_tasks: email %s failed: %s",
                email["id"], e,
            )
            failed.append({"item_id": email["id"], "error": str(e)})

    return {
        "thread_id": thread_id,
        "created": created,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# email_create_umbrella_task — one task representing the whole cluster
# ---------------------------------------------------------------------------


def email_create_umbrella_task(
    thread_id: str,
    *,
    urgency: str = "medium",
    project: str | None = None,
    title_override: str | None = None,
) -> dict[str, Any]:
    """Create a single task representing the whole email cluster.

    Task text uses the cluster's label (or ``title_override``);
    description lists every email's subject + sender + date so the
    user has the full bundle of context on one task.
    """
    from work_buddy.obsidian.tasks.mutations import create_task

    thread = _get_thread_or_raise(thread_id)
    emails = _emails_from_thread(thread)
    if not emails:
        return {
            "thread_id": thread_id,
            "created": None,
            "failed": [],
            "skipped_empty": True,
        }

    if title_override:
        text = title_override
    else:
        ie = thread.inciting_event_summary or {}
        text = ie.get("title") or ie.get("description") or "Email cluster"

    bullet_lines = _email_summary_lines(emails)
    summary = "Emails in this cluster:\n\n" + "\n".join(bullet_lines)

    try:
        result = create_task(
            task_text=text[:120],
            urgency=urgency,
            project=project,
            summary=summary,
            creation_provenance="agent_inferred_from_email",
            user_involvement="medium",
        )
    except Exception as e:
        logger.warning("email_create_umbrella_task: %s", e)
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
            "email_count": len(emails),
        },
        "failed": [],
    }


