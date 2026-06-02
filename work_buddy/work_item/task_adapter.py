"""Task write port — the one-way bridge from a ``Task`` to the task mutation layer.

The single place that translates "write intent against a task" into a call on
``work_buddy.obsidian.tasks.mutations``. ``Task(WorkItem)`` reaches the markdown
master list + the ``task_metadata`` store *through* these functions rather than
calling the mutation layer directly, so the task system's write path runs through
the WorkItem family.

Design rules this module honours:

* **Stateless, id-keyed.** Every function takes a ``task_id`` (or, for
  :func:`create`, the new task's text) plus field values — never a ``Task``
  instance. The module has no dependency on ``work_buddy.threads``, which keeps
  the dependency one-way (``Task`` → adapter → ``mutations``) and free of import
  cycles.
* **Pure pass-through.** Each function forwards to its ``mutations`` counterpart
  and returns that result unchanged. The mutation layer owns validation, the
  atomic dual-surface (markdown + store) write, plugin-marker preservation,
  consent, bridge-retry, and event emission (``_publish_task_event`` →
  dashboard + ``work_item_events``). This port adds none of those and emits
  nothing of its own, so routing a write through it changes no behaviour.
* **Import-light.** ``mutations`` (transitively ``sqlite3``) is imported inside
  each function, never at module top, per ``architecture/mcp-import-discipline``.
"""

from __future__ import annotations

from typing import Any


def create(
    task_text: str,
    *,
    urgency: str = "medium",
    project: str | None = None,
    due_date: str | None = None,
    contract: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a new task. Forwards to ``mutations.create_task``.

    The long GTD/risk/context keyword tail of ``create_task`` (``task_kind``,
    ``density``, ``creation_provenance``, ``user_involvement``,
    ``risk_profile_json``, …) is forwarded verbatim through ``**kwargs``.
    Returns the raw ``create_task`` result dict (the minted ``task_id`` +
    verification state callers consume).
    """
    from work_buddy.obsidian.tasks import mutations

    return mutations.create_task(
        task_text,
        urgency=urgency,
        project=project,
        due_date=due_date,
        contract=contract,
        summary=summary,
        tags=tags,
        **kwargs,
    )


def toggle(
    task_id: str,
    *,
    done: bool | None = None,
    file_path: str | None = None,
    done_date: str | None = None,
) -> dict[str, Any]:
    """Toggle a task's completion. Forwards to ``mutations.toggle_task``."""
    from work_buddy.obsidian.tasks import mutations

    return mutations.toggle_task(
        task_id, done=done, file_path=file_path, done_date=done_date,
    )


def update(
    task_id: str | None = None,
    *,
    description_match: str | None = None,
    state: str | None = None,
    urgency: str | None = None,
    complexity: str | None = None,
    contract: str | None = None,
    snooze_until: str | None = None,
    due_date: str | None = None,
    reason: str | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Update task metadata. Forwards to ``mutations.update_task``.

    ``description_match`` (a substring fallback for tasks without an id) is
    carried for full parity with the underlying mutation — instance callers
    always have an id and pass ``task_id``, but the ``task_change_state`` op
    exposes the fallback. Cannot set ``state='done'`` — ``mutations.update_task``
    rejects it; use :func:`toggle` for completion.
    """
    from work_buddy.obsidian.tasks import mutations

    return mutations.update_task(
        task_id=task_id,
        description_match=description_match,
        state=state,
        urgency=urgency,
        complexity=complexity,
        contract=contract,
        snooze_until=snooze_until,
        due_date=due_date,
        reason=reason,
        file_path=file_path,
    )


def set_description(
    task_id: str, new_description: str, *, file_path: str | None = None,
) -> dict[str, Any]:
    """Rewrite a task's description text.

    Forwards to ``mutations.update_task_description`` (which preserves every
    structural token — checkbox, ``#todo``, tags, wikilinks, 🆔, plugin emojis).
    """
    from work_buddy.obsidian.tasks import mutations

    return mutations.update_task_description(
        task_id, new_description, file_path=file_path,
    )


def set_tags(task_id: str, namespace_tags: list[str]) -> dict[str, Any]:
    """Replace a task line's user-modifiable tags.

    Forwards to ``mutations.set_task_tags_on_line`` (pass the complete desired
    tag list; anything omitted is removed).
    """
    from work_buddy.obsidian.tasks import mutations

    return mutations.set_task_tags_on_line(task_id, namespace_tags)


def delete(task_id: str) -> dict[str, Any]:
    """Delete a task — line, note file, and store record (soft-delete).

    Forwards to ``mutations.delete_task``.
    """
    from work_buddy.obsidian.tasks import mutations

    return mutations.delete_task(task_id)


def assign(task_id: str) -> dict[str, Any]:
    """Claim a task for the current agent session and return its full context.

    Forwards to ``mutations.assign_task``.
    """
    from work_buddy.obsidian.tasks import mutations

    return mutations.assign_task(task_id)
