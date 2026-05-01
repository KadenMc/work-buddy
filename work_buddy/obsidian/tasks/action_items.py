"""Slice 7: ``task_action_items`` CRUD layer.

Per-action-item rows attached to a parent task.  Each item carries
its own risk profile + required contexts so the resolver can answer
"who can act on the *current step* now?" rather than just the parent
task — the engage view + the Today tab consume this when the parent
task has ``current_action_item_id`` set.

Safety rule (per ROADMAP §7, refined in PR #70 fix #2): items with
``authorship = 'agent_unapproved'`` cannot be executed by the agent —
they're proposals waiting on user approval.  The :func:`is_executable`
helper enforces this; callers in the executor + Resolution Surface
respect the check.

Markdown is the eventual canonical surface for action items (per
ROADMAP §8 task index footnote: "After slice 7, the markdown lists
migrate to the structured ``task_action_items`` table; Obsidian
markdown remains canonical, the table is a cache").  Slice 7 ships
the table; the markdown round-trip lands in a follow-up so the table
schema can stabilize against real usage first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks import store

logger = get_logger(__name__)


VALID_STATES = {"pending", "in_progress", "done", "skipped"}

# Slice 7 PR #70 fix #2: authorship enum.
#   'user'              -- user wrote it from scratch
#   'agent_approved'    -- agent proposed, user accepted
#   'agent_unapproved'  -- agent proposed, no user approval (gate-blocked)
VALID_AUTHORSHIP = frozenset({"user", "agent_approved", "agent_unapproved"})


def _validate_authorship(authorship: str | None) -> str:
    """Coerce to a valid authorship value or raise.

    None defaults to ``'agent_unapproved'`` -- the safe option that
    gates execution.  Any other value raises ValueError so typos
    surface at the call site rather than silently producing
    gate-bypassing rows.
    """
    if authorship is None:
        return "agent_unapproved"
    if authorship not in VALID_AUTHORSHIP:
        raise ValueError(
            f"Invalid authorship {authorship!r}: expected one of "
            f"{sorted(VALID_AUTHORSHIP)}"
        )
    return authorship


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
    authorship: str | None = None,
    handoff_package_path: str | None = None,
) -> dict[str, Any]:
    """Insert a new action item row.

    ``sequence`` is auto-assigned to the next available integer per
    task when None — saves the caller from a round-trip.  Pass an
    explicit value to insert at a specific position (e.g., re-shuffling
    via the develop-at-pickup edit-each-item flow).

    ``authorship`` (per ROADMAP §7 + PR #70 fix #2): one of
    ``'user'`` / ``'agent_approved'`` / ``'agent_unapproved'``.
    Defaults to ``'agent_unapproved'`` — agent-proposed items are
    gate-blocked from execution until the user explicitly accepts via
    :func:`approve` or by adopting them into the markdown.
    """
    if state not in VALID_STATES:
        raise ValueError(
            f"Invalid state {state!r}: expected one of {sorted(VALID_STATES)}"
        )

    auth_value = _validate_authorship(authorship)

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
                authorship,
                handoff_package_path,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, sequence, description, state,
                risk_profile_json, agent_required_contexts,
                user_required_contexts, definition_of_done,
                auth_value,
                handoff_package_path,
                now, now,
            ),
        )
        item_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "action_item created: task=%s item=%s seq=%d state=%s",
        task_id, item_id, sequence, state,
    )
    return {"id": item_id, "task_id": task_id, "sequence": sequence,
            "state": state}


def get(item_id: int) -> dict[str, Any] | None:
    conn = store.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM task_action_items WHERE id = ?", (item_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_for_task(
    task_id: str, *, include_done: bool = True,
) -> list[dict[str, Any]]:
    """Return all action items for a task, ordered by sequence ascending."""
    clauses = ["task_id = ?"]
    params: list[Any] = [task_id]
    if not include_done:
        clauses.append("state != 'done'")

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

    ``authorship`` (when set) goes through ``_validate_authorship``
    which raises on typos.  Pass None or omit to leave the field
    unchanged.
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
        sets.append("authorship = ?")
        params.append(_validate_authorship(authorship))

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
    """Delete an action item.  Returns True if the row existed."""
    conn = store.get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM task_action_items WHERE id = ?", (item_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def approve(item_id: int) -> dict[str, Any]:
    """Mark an agent-proposed action item as user-approved.

    Sets ``authorship='agent_approved'`` (PR #70 fix #2) — the user
    explicitly accepted an agent-proposed item, so it admits future
    agent execution via :func:`is_executable` while preserving the
    agent-origin provenance in the canonical column.

    The legacy fields (``user_authored``, ``approved_at``) are kept
    in sync by the update layer so back-compat callers still see the
    expected values.
    """
    return update(item_id, authorship="agent_approved")


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

    Returns True iff the item's ``authorship`` is one of
    {``'user'``, ``'agent_approved'``} AND its state is not terminal
    (``'done'`` / ``'skipped'``).  Items without ``authorship`` set
    are treated as ``'agent_unapproved'`` (the safe default) — gate-
    blocked from agent execution.
    """
    if item.get("state") in {"done", "skipped"}:
        return False
    return item.get("authorship") in {"user", "agent_approved"}


# ---------------------------------------------------------------------------
# Markdown ↔ table sync (Slice 7 + ROADMAP §8 footnote)
# ---------------------------------------------------------------------------


def parse_action_items_from_note(note_body: str | None) -> list[str]:
    """Extract plain bullets from the ``## Action items`` section.

    Per Slice-7 doctrine: action items are PLAIN BULLETS (``- `` only,
    no checkboxes), keyed off the ``## Action items`` heading.  Empty
    section is allowed (returns ``[]``).  An empty list is meaningful
    -- it represents an explicitly-sparse task and the sync layer
    should not auto-populate.

    Returns the description strings in markdown order.  State /
    metadata don't live in the markdown (they're SQLite-side); the
    description text is the only field the user edits in markdown.
    """
    if not note_body:
        return []

    lines = note_body.splitlines()
    in_section = False
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            in_section = (heading == "action items")
            continue
        if not in_section:
            continue
        if not stripped:
            continue
        # Stop at the next heading of any level.
        if stripped.startswith("#"):
            in_section = False
            continue
        # Plain bullet only -- per Slice 7, no checkboxes.
        if stripped.startswith("- "):
            text = stripped[2:].strip()
            if text:
                items.append(text)
    return items


def reconcile_from_markdown(
    task_id: str,
    note_body: str | None,
) -> dict[str, Any]:
    """Sync the ``task_action_items`` table with the note's markdown.

    Obsidian markdown is canonical (per ROADMAP §8 footnote); SQLite
    is a cache.  This function:

    1. Parses ``## Action items`` plain bullets via
       :func:`parse_action_items_from_note`.
    2. Loads the existing table rows for this task (ordered by sequence).
    3. Reconciles by sequence position -- if markdown[i] differs from
       table[i].description, update the description in place
       (preserving state / risk_profile / contexts / approval).
    4. Inserts new bullets that don't have a corresponding row.
    5. Deletes table rows that have no corresponding bullet
       (catches user removals from markdown).

    Returns a summary dict ``{added, updated, deleted, kept}`` for
    audit logging.

    Note: this re-establishes the user_authored=1 invariant on every
    bullet that appears in the markdown -- markdown edits ARE user
    authorship by definition.  Approved-then-removed agent items
    are dropped (the user removed them).
    """
    from_md = parse_action_items_from_note(note_body)
    existing = list_for_task(task_id, include_done=True)
    summary = {"added": 0, "updated": 0, "deleted": 0, "kept": 0}

    # Pair by sequence index (1-based markdown order).
    md_indexed = list(enumerate(from_md, start=1))
    existing_by_seq = {int(r["sequence"]): r for r in existing}

    seen_seqs: set[int] = set()
    for seq, desc in md_indexed:
        seen_seqs.add(seq)
        existing_row = existing_by_seq.get(seq)
        if existing_row is None:
            # New bullet -- the user typed it directly into markdown.
            # Origin = 'user'.
            create(
                task_id=task_id, sequence=seq, description=desc,
                authorship="user",
            )
            summary["added"] += 1
        elif existing_row.get("description") != desc:
            # User edited an existing item's description in markdown.
            # Edits are user authorship by definition; lift to 'user'
            # if it wasn't already (PR #70 fix #2: edit IS rewrite).
            update(
                int(existing_row["id"]),
                description=desc,
                authorship="user",
            )
            summary["updated"] += 1
        else:
            # Description is unchanged.  If the row was 'agent_unapproved'
            # but now appears in markdown, the user adopted it -- promote
            # to 'agent_approved' (preserves agent-origin provenance).
            # If it was already 'agent_approved' or 'user', leave alone.
            if existing_row.get("authorship") == "agent_unapproved":
                update(int(existing_row["id"]), authorship="agent_approved")
            summary["kept"] += 1

    for seq, row in existing_by_seq.items():
        if seq in seen_seqs:
            continue
        # Markdown removed this bullet -> table row goes too.
        delete(int(row["id"]))
        summary["deleted"] += 1

    return summary


def migrate_existing_notes(read_note_body) -> dict[str, Any]:
    """One-shot migration: parse every developed/dense task's note and
    populate ``task_action_items`` from the existing markdown.

    Args:
        read_note_body: callable ``(note_uuid) -> str | None`` --
            typically ``bridge.read_file(f"tasks/notes/{uuid}.md")`` or
            equivalent fallback.  Caller provides this so the function
            stays bridge-agnostic + testable.

    Returns:
        ``{tasks_examined, tasks_with_items, items_inserted}``.
        Idempotent on re-run -- :func:`reconcile_from_markdown` does
        the per-task diff so re-running doesn't duplicate.
    """
    tally = {"tasks_examined": 0, "tasks_with_items": 0, "items_inserted": 0}
    rows = store.query(include_archived=False)
    for row in rows:
        if row.get("density") == "sparse":
            continue  # respect the explicit-sparsity doctrine
        note_uuid = row.get("note_uuid")
        if not note_uuid:
            continue
        try:
            body = read_note_body(note_uuid)
        except Exception as exc:
            logger.debug("migrate_existing_notes: read failed for %s: %s",
                         note_uuid, exc)
            continue
        if body is None:
            # Missing note file -- skip rather than delete existing rows.
            # Defensive: a transient bridge failure shouldn't clobber
            # the table.
            continue
        tally["tasks_examined"] += 1
        summary = reconcile_from_markdown(row["task_id"], body)
        if summary["added"] or summary["kept"]:
            tally["tasks_with_items"] += 1
        tally["items_inserted"] += summary["added"]
    return tally


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
