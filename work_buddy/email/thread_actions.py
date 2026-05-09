"""Per-thread Email actions — wrappers that walk an Email-group
thread's ``context_items`` and route each message through the existing
task-creation primitives.

Backs the ``email_close``, ``email_create_tasks``,
``email_create_umbrella_task``, and ``email_record_into_task`` entries
in the Email pipeline's action library. All take a ``thread_id`` and
read its ContextItems (source ``"email_message"``) — the pipeline's
spawn step has already attached one ContextItem per email to the
group sub-thread.

Continue-on-error semantics: each item is routed independently; a
single failure doesn't block the others.

Why not bridge mutations? The Thunderbird bridge is read-first in v1
(see ``email/`` integration unit). ``email_close`` is therefore
*advisory* — it dismisses the Thread without touching the underlying
mailbox. Once the bridge grows ``archive`` / ``move`` / ``delete``
permissions, the closer side effect would join here behind a consent
gate.
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


# ---------------------------------------------------------------------------
# email_record_into_task — file the cluster onto an existing task's note
# ---------------------------------------------------------------------------


def email_record_into_task(
    thread_id: str,
    *,
    target_task_id: str,
    section_heading: str | None = None,
) -> dict[str, Any]:
    """Append the cluster's emails as a section in an existing task's note.

    Use when the email cluster is *context for ongoing work* rather than
    a new task in itself — e.g., reply threads on an active deliverable,
    PR-review notifications about a task you're already tracking. The
    cluster's emails are appended as a bulleted section to the target
    task's linked note.

    The target task must exist AND have a linked note. If the task
    has no note yet, returns ``{"appended": False, "error": ...}``
    rather than implicitly creating one — adding a note to a task is
    a different decision the user should make explicitly.

    Args:
        thread_id: Email-cluster sub-thread carrying the emails to record.
        target_task_id: Task ID (e.g. ``"t-a3f8c1e2"``) the cluster
            should be filed against. Must already have a note attached.
        section_heading: Optional override for the section heading
            written into the note. Defaults to ``"Emails recorded"``.

    Returns:
        ``{"thread_id": str, "target_task_id": str, "appended": bool,
           "email_count": int, "note_path": str | None,
           "error": str | None, "skipped_empty": bool}``.
    """
    from pathlib import Path

    from work_buddy.config import load_config
    from work_buddy.journal_backlog.route import _append_to_note_impl
    from work_buddy.obsidian.tasks.mutations import read_task

    thread = _get_thread_or_raise(thread_id)
    emails = _emails_from_thread(thread)
    if not emails:
        return {
            "thread_id": thread_id,
            "target_task_id": target_task_id,
            "appended": False,
            "email_count": 0,
            "note_path": None,
            "skipped_empty": True,
        }

    # Resolve target task + its linked note path.
    try:
        task_payload = read_task(target_task_id)
    except Exception as exc:
        raise EmailThreadActionError(
            f"Could not read target task {target_task_id!r}: {exc}",
        ) from exc

    if not task_payload or not task_payload.get("success"):
        raise EmailThreadActionError(
            f"Target task {target_task_id!r} not found",
        )

    note_path = task_payload.get("note_path")
    if not note_path:
        # Don't implicitly create a note — that's a separate decision
        # the user should make explicitly via task_assign or similar.
        return {
            "thread_id": thread_id,
            "target_task_id": target_task_id,
            "appended": False,
            "email_count": len(emails),
            "note_path": None,
            "error": (
                f"Target task {target_task_id!r} has no linked note. "
                "Attach a note first (e.g. via task_assign) and retry."
            ),
        }

    # Compose the section.
    heading = (section_heading or "Emails recorded").strip()
    bullet_lines = _email_summary_lines(emails)
    section = f"## {heading}\n\n" + "\n".join(bullet_lines)

    # Append directly via the journal_backlog primitive (which does the
    # path-traversal guard + .md check + filesystem write). The call
    # site here is consent-gated at the capability level
    # (email_record_into_task has mutates_state=True + requires obsidian)
    # so going through the consent-wrapped append_to_note here would
    # double-prompt for the same user-initiated click.
    cfg = load_config() or {}
    vault_root = Path(cfg.get("vault_root") or "")
    if not vault_root or not vault_root.exists():
        return {
            "thread_id": thread_id,
            "target_task_id": target_task_id,
            "appended": False,
            "email_count": len(emails),
            "note_path": note_path,
            "error": "vault_root not configured or missing",
        }

    try:
        result = _append_to_note_impl(section, vault_root, note_path)
    except Exception as exc:
        logger.warning(
            "email_record_into_task: append failed for %s: %s",
            target_task_id, exc,
        )
        return {
            "thread_id": thread_id,
            "target_task_id": target_task_id,
            "appended": False,
            "email_count": len(emails),
            "note_path": note_path,
            "error": str(exc),
        }

    if not result.get("success"):
        return {
            "thread_id": thread_id,
            "target_task_id": target_task_id,
            "appended": False,
            "email_count": len(emails),
            "note_path": note_path,
            "error": result.get("message") or "append_to_note failed",
        }

    return {
        "thread_id": thread_id,
        "target_task_id": target_task_id,
        "appended": True,
        "email_count": len(emails),
        "note_path": note_path,
    }


