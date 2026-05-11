"""``task_action_items`` CRUD layer.

Per-action-item rows attached to a parent task. Each item carries
its own risk profile + required contexts so the resolver can answer
"who can act on the *current step* now?" rather than just the parent
task — the engage view + the Today tab consume this when the parent
task has ``current_action_item_id`` set.

Safety rule: items with ``authorship == 'agent_unapproved'`` cannot
be executed by the agent — they're proposals waiting on user
approval. The :func:`is_executable` helper enforces this; callers in
the executor + Resolution Surface respect the check.

The ``authorship`` column on ``task_action_items`` is a TEXT enum
holding values from :class:`work_buddy.threads.enums.Authorship`
(``user`` / ``agent_approved`` / ``agent_unapproved``).

Markdown is the eventual canonical surface for action items (the
table is a cache); the markdown round-trip lands in a follow-up so
the table schema can stabilize against real usage first.

See ``tasks/action-items`` for the agent-facing reference.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks import store
from work_buddy.threads.enums import Authorship

logger = get_logger(__name__)


VALID_STATES = {"pending", "in_progress", "done", "skipped"}

VALID_AUTHORSHIPS = {a.value for a in Authorship}
# Authorship values that allow agent execution. ``agent_unapproved``
# is intentionally absent — that's the default state for agent-
# proposed items pending user approval, and the safety contract
# is that such items block.
EXECUTABLE_AUTHORSHIPS = {Authorship.USER.value, Authorship.AGENT_APPROVED.value}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create(
    task_id: str,
    *,
    description: str,
    sequence: int | None = None,
    state: str = "pending",
    risk_profile_json: str | None = None,
    agent_required_contexts: str | None = None,
    user_required_contexts: str | None = None,
    definition_of_done: str | None = None,
    authorship: str = Authorship.AGENT_UNAPPROVED.value,
    handoff_package_path: str | None = None,
) -> dict[str, Any]:
    """Insert a new action item row.

    ``sequence`` is auto-assigned to the next available integer per
    task when None — saves the caller from a round-trip.  Pass an
    explicit value to insert at a specific position (e.g., re-shuffling
    via the develop-at-pickup edit-each-item flow).

    Per the safety rule: agent-proposed items default to
    ``authorship='agent_unapproved'`` — they appear in the Resolution
    Surface as "proposed (needs approval)" and cannot be executed by
    the agent until :func:`approve` flips them to ``agent_approved``.
    User-written items should be created with ``authorship='user'``.
    """
    if state not in VALID_STATES:
        raise ValueError(
            f"Invalid state {state!r}: expected one of {sorted(VALID_STATES)}"
        )
    if authorship not in VALID_AUTHORSHIPS:
        raise ValueError(
            f"Invalid authorship {authorship!r}: expected one of "
            f"{sorted(VALID_AUTHORSHIPS)}"
        )

    now = _now_iso()
    conn = store.get_connection()
    try:
        if sequence is None:
            row = conn.execute(
                """SELECT COALESCE(MAX(sequence), 0) AS s
                   FROM task_action_items WHERE task_id = ?""",
                (task_id,),
            ).fetchone()
            sequence = int(row["s"]) + 1

        cursor = conn.execute(
            """INSERT INTO task_action_items
               (task_id, sequence, description, state,
                risk_profile_json, agent_required_contexts,
                user_required_contexts, definition_of_done,
                authorship, handoff_package_path,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, sequence, description, state,
                risk_profile_json, agent_required_contexts,
                user_required_contexts, definition_of_done,
                authorship, handoff_package_path,
                now, now,
            ),
        )
        item_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "action_item created: task=%s item=%s seq=%d state=%s authorship=%s",
        task_id, item_id, sequence, state, authorship,
    )
    return {"id": item_id, "task_id": task_id, "sequence": sequence,
            "state": state, "authorship": authorship}


def get(item_id: int, *, include_deleted: bool = False) -> dict[str, Any] | None:
    """Return an action item by ID, or None.

    Soft-deleted items (``deleted_at IS NOT NULL``) are invisible by
    default. Pass ``include_deleted=True`` for recovery contexts.
    """
    conn = store.get_connection()
    try:
        if include_deleted:
            row = conn.execute(
                "SELECT * FROM task_action_items WHERE id = ?", (item_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM task_action_items "
                "WHERE id = ? AND deleted_at IS NULL",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_for_task(
    task_id: str,
    *,
    include_done: bool = True,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Return all action items for a task, ordered by sequence ascending.

    Soft-deleted items are excluded by default; pass
    ``include_deleted=True`` for recovery / audit contexts.
    """
    clauses = ["task_id = ?"]
    params: list[Any] = [task_id]
    if not include_done:
        clauses.append("state != 'done'")
    if not include_deleted:
        clauses.append("deleted_at IS NULL")

    where = " AND ".join(clauses)
    conn = store.get_connection()
    try:
        rows = conn.execute(
            f"""SELECT * FROM task_action_items
                WHERE {where}
                ORDER BY sequence ASC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


_SENTINEL = object()


def update(
    item_id: int,
    *,
    description: str | object = _SENTINEL,
    state: str | None = None,
    sequence: int | None = None,
    risk_profile_json: str | None | object = _SENTINEL,
    agent_required_contexts: str | None | object = _SENTINEL,
    user_required_contexts: str | None | object = _SENTINEL,
    definition_of_done: str | None | object = _SENTINEL,
    authorship: str | None = None,
    completed_at: str | None | object = _SENTINEL,
    handoff_package_path: str | None | object = _SENTINEL,
) -> dict[str, Any]:
    """Patch one or more fields on an action item.

    ``state='done'`` auto-stamps ``completed_at`` if the caller didn't
    pass one — same convention as ``store.update``.

    ``authorship`` is validated against ``VALID_AUTHORSHIPS`` (the
    enum from ``work_buddy.threads.enums.Authorship``). Use
    :func:`approve` rather than passing ``authorship='agent_approved'``
    directly — it's the public approval entry point.
    """
    sets: list[str] = []
    params: list[Any] = []

    if description is not _SENTINEL:
        sets.append("description = ?")
        params.append(description)

    if state is not None:
        if state not in VALID_STATES:
            raise ValueError(f"Invalid state {state!r}")
        sets.append("state = ?")
        params.append(state)
        if state == "done" and completed_at is _SENTINEL:
            sets.append("completed_at = ?")
            params.append(_now_iso())

    if sequence is not None:
        sets.append("sequence = ?")
        params.append(int(sequence))

    if risk_profile_json is not _SENTINEL:
        sets.append("risk_profile_json = ?")
        params.append(risk_profile_json)

    if agent_required_contexts is not _SENTINEL:
        sets.append("agent_required_contexts = ?")
        params.append(agent_required_contexts)

    if user_required_contexts is not _SENTINEL:
        sets.append("user_required_contexts = ?")
        params.append(user_required_contexts)

    if definition_of_done is not _SENTINEL:
        sets.append("definition_of_done = ?")
        params.append(definition_of_done)

    if authorship is not None:
        if authorship not in VALID_AUTHORSHIPS:
            raise ValueError(
                f"Invalid authorship {authorship!r}: expected one of "
                f"{sorted(VALID_AUTHORSHIPS)}"
            )
        sets.append("authorship = ?")
        params.append(authorship)

    if completed_at is not _SENTINEL:
        sets.append("completed_at = ?")
        params.append(completed_at)

    if handoff_package_path is not _SENTINEL:
        sets.append("handoff_package_path = ?")
        params.append(handoff_package_path)

    if not sets:
        return {"id": item_id, "changed": False}

    sets.append("updated_at = ?")
    params.append(_now_iso())
    params.append(item_id)

    conn = store.get_connection()
    try:
        conn.execute(
            f"UPDATE task_action_items SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": item_id, "changed": True}


def delete(item_id: int) -> bool:
    """Soft-delete an action item. Returns True if the row existed and was flipped.

    Soft operation — ``deleted_at`` is set to a timestamp; the row
    stays in ``task_action_items``. Query paths default-filter
    ``WHERE deleted_at IS NULL``. Inverse: :func:`restore`.

    Calling on an already-soft-deleted row is a no-op (returns False).
    """
    conn = store.get_connection()
    try:
        row = conn.execute(
            "SELECT deleted_at FROM task_action_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not row or row["deleted_at"] is not None:
            return False
        now = _now_iso()
        cursor = conn.execute(
            "UPDATE task_action_items SET deleted_at = ?, updated_at = ? "
            "WHERE id = ?",
            (now, now, item_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def restore(item_id: int) -> bool:
    """Clear ``deleted_at`` on a soft-deleted action item.

    Inverse of :func:`delete`. Returns True iff the row existed and
    had ``deleted_at`` set.
    """
    conn = store.get_connection()
    try:
        row = conn.execute(
            "SELECT deleted_at FROM task_action_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not row or row["deleted_at"] is None:
            return False
        now = _now_iso()
        cursor = conn.execute(
            "UPDATE task_action_items SET deleted_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (now, item_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def approve(item_id: int) -> dict[str, Any]:
    """Mark an agent-proposed action item as user-approved.

    Flips ``authorship`` from ``'agent_unapproved'`` to
    ``'agent_approved'`` so the safety check (:func:`is_executable`)
    admits future executions.

    Idempotent: calling on an already-user-authored or
    already-agent-approved item is a no-op (the update only changes
    ``authorship`` when the new value differs from the current one,
    which it won't for those cases here — we always write
    ``'agent_approved'``).
    """
    return update(item_id, authorship=Authorship.AGENT_APPROVED.value)


def set_current(task_id: str, item_id: int | None) -> None:
    """Point ``task_metadata.current_action_item_id`` at this item.

    Pass ``item_id=None`` to clear (the task has no current step,
    e.g., when the user closes the develop view without picking one).
    """
    conn = store.get_connection()
    try:
        conn.execute(
            "UPDATE task_metadata SET current_action_item_id = ?, "
            "updated_at = ? WHERE task_id = ?",
            (item_id, _now_iso(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Safety rule
# ---------------------------------------------------------------------------


def is_executable(item: dict[str, Any]) -> bool:
    """Per ROADMAP §7: agent may only execute approved items.

    Returns True iff the item's ``authorship`` is in
    :data:`EXECUTABLE_AUTHORSHIPS` (``'user'`` or ``'agent_approved'``)
    AND its ``state`` is non-terminal (``'pending'`` or
    ``'in_progress'``). ``'agent_unapproved'`` items always block;
    ``'done'`` and ``'skipped'`` items always block.
    """
    state = item.get("state")
    if state in {"done", "skipped"}:
        return False
    return item.get("authorship") in EXECUTABLE_AUTHORSHIPS


def position_in_task(item: dict[str, Any]) -> tuple[int, int]:
    """Return (current_index, total) for the master-list "step N of M" badge.

    ``current_index`` is 1-based.  Total counts every action item on
    the parent task regardless of state (the badge shows progress
    against the original plan, not the live remainder).
    """
    task_id = item.get("task_id")
    if not task_id:
        return (1, 1)
    rows = list_for_task(task_id, include_done=True)
    total = len(rows) or 1
    for i, r in enumerate(rows, start=1):
        if r.get("id") == item.get("id"):
            return (i, total)
    return (1, total)
