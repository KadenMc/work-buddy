"""Group-relationship parents — items inside group sub-threads.

Companion to ``decompose.py``. The difference between the two
patterns:

- **decompose**: parent thread → action → N child sub-threads. Each
  child carries one ContextItem (one tab → one sub-thread). Children
  fan out, each runs its own FSM end-to-end, parent advances to DONE
  when every child reaches terminal.
- **group** (this module): parent (the "umbrella") → action ``group``
  → N child sub-threads, one per **cluster**. Each child carries
  ALL the cluster's ContextItems together (cluster of 5 tabs → 5
  ContextItems on one child). Items can be **moved between sibling
  children** via :func:`move_item` — the move rewrites the
  ``context_items`` tuples on both threads, no Thread-level
  re-parenting. Approving the umbrella **cascades full Accept** to
  every child (one click = whole scrape processed); children may
  also be approved individually.

Public API
----------

- :func:`group_thread` — given a parent + source ContextItems +
  clustering output, spawns one child per cluster.
- :func:`move_item` — moves a ContextItem between two sibling
  children (must share the same umbrella parent).
- :func:`cascade_approve_umbrella` — runs Accept on every non-
  terminal child of an umbrella; collects partial failures rather
  than raising.
- :func:`spawn_empty_group` — adds a fresh empty child sub-thread
  under an existing umbrella (drives the "+ New group" drop zone in
  the UI).
- :func:`delete_group_subthread` — DISMISSes a (typically empty)
  child of an umbrella.

Event kinds emitted (see ``events.py``):

- ``KIND_GROUPS_SPAWNED`` on the umbrella (analogous to
  ``KIND_SUBTHREADS_SPAWNED`` from decompose).
- ``KIND_CONTEXT_ITEM_MOVED`` on both source and destination of a
  :func:`move_item` (paired via shared ``migration_id``).
- ``KIND_GROUP_DELETED`` on the umbrella when a child is dismissed
  via :func:`delete_group_subthread`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable, Optional

from work_buddy.threads import autonomy, engine, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    ACTOR_FSM_ENGINE,
    ACTOR_USER,
    KIND_CONTEXT_ITEM_MOVED,
    KIND_GROUP_DELETED,
    KIND_GROUPS_SPAWNED,
    ThreadEvent,
)
from work_buddy.threads.fsm import (
    TRIG_DISMISSED_BY_USER,
)
from work_buddy.threads.models import (
    AutonomyPolicy,
    ContextItem,
    Thread,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GroupRefused(ValueError):
    """A group operation's preconditions failed (parent not found,
    cross-umbrella move, item not present, autonomy override widens,
    etc.)."""


# ---------------------------------------------------------------------------
# Spawn (umbrella → N group children)
# ---------------------------------------------------------------------------


def group_thread(
    parent_id: str,
    source_items: Iterable[ContextItem | dict],
    clusters: list[dict[str, Any]],
    *,
    autonomy_override: Optional[AutonomyPolicy] = None,
    inciting_summary_extra: Optional[dict[str, Any]] = None,
    actor: str = ACTOR_FSM_ENGINE,
    conn=None,
) -> list[str]:
    """Spawn N child sub-threads under ``parent_id``, one per cluster.

    Mirrors :func:`decompose.decompose_thread` but each child carries
    its **whole cluster** of ContextItems (not just one item).

    Args:
        parent_id: the umbrella thread. Will be flagged
            ``parent_relationship='group'`` (set explicitly here so a
            previously-decompose parent can be re-grouped if ever
            needed).
        source_items: iterable of ContextItems (or dicts). Anything
            not referenced by any cluster falls into a synthetic
            "Ungrouped" child so nothing is silently dropped.
        clusters: list of dicts with keys:
            - ``label``: human-readable group title (e.g. "Research")
            - ``item_ids``: list of ContextItem.id values pointing
              into ``source_items``
        autonomy_override: per-child autonomy. Validated as
            override-down only (same rules as decompose).
        inciting_summary_extra: merged into each child's
            ``inciting_event_summary``.

    Returns: list of new child thread_ids in cluster order.

    Raises:
        :class:`GroupRefused` for empty source list, unknown parent,
        or override-up attempt.
    """
    items = [
        ContextItem.from_dict(i) if isinstance(i, dict) else i
        for i in source_items
    ]
    if not items:
        raise GroupRefused(
            "group_thread requires at least one source item",
        )

    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        parent = store.get_thread(parent_id, conn=conn)
        if parent is None:
            raise GroupRefused(f"Parent thread {parent_id!r} not found")

        child_policy = autonomy_override or parent.autonomy_policy
        if autonomy_override is not None:
            try:
                autonomy.validate_override_down(
                    parent.autonomy_policy, autonomy_override,
                )
            except autonomy.OverrideUpRejected as e:
                raise GroupRefused(str(e)) from e

        # Bucket items by cluster. Anything not referenced ends up in
        # a synthetic "Ungrouped" cluster.
        items_by_id: dict[str, ContextItem] = {it.id: it for it in items}
        used_ids: set[str] = set()
        cluster_specs: list[tuple[str, list[ContextItem]]] = []
        for cluster in clusters:
            label = str(cluster.get("label") or "Group")
            cluster_item_ids = cluster.get("item_ids") or cluster.get(
                "tab_ids"
            ) or []
            picked: list[ContextItem] = []
            for item_id in cluster_item_ids:
                key = str(item_id)
                if key in items_by_id and key not in used_ids:
                    picked.append(items_by_id[key])
                    used_ids.add(key)
            if picked:
                cluster_specs.append((label, picked))
        leftover = [it for it in items if it.id not in used_ids]
        if leftover:
            cluster_specs.append(("Ungrouped", leftover))

        if not cluster_specs:
            raise GroupRefused(
                "group_thread: no items ended up in any cluster",
            )

        # Mark the parent as a group umbrella (idempotent if already).
        if parent.parent_relationship != "group":
            store.update_thread_state(
                parent_id,
                parent_relationship="group",
                conn=conn,
            )

        child_ids: list[str] = []
        for label, cluster_items in cluster_specs:
            inciting = {
                "source": "group",
                "parent_id": parent_id,
                "cluster_label": label,
                "title": label,
                "description": label,
                "item_count": len(cluster_items),
            }
            if inciting_summary_extra:
                inciting.update(inciting_summary_extra)

            child = Thread(
                parent_id=parent_id,
                autonomy_policy=child_policy,
                context_items=tuple(cluster_items),
                inciting_event_summary=inciting,
            )
            store.insert_thread(child, conn=conn)
            child_ids.append(child.thread_id)

        # Record group-spawn on the umbrella.
        store.append_event(
            ThreadEvent(
                thread_id=parent_id,
                kind=KIND_GROUPS_SPAWNED,
                actor=actor,
                data={
                    "child_thread_ids": child_ids,
                    "child_labels": [
                        label for label, _ in cluster_specs
                    ],
                    "source_count": len(items),
                    "cluster_count": len(cluster_specs),
                },
                parent_event_id=parent.parent_event_id,
            ),
            conn=conn,
        )

        # Umbrella enters MONITORING (same as decompose-parent —
        # it's a parent-of-children state outside the normal
        # transition table).
        store.update_thread_state(
            parent_id,
            fsm_state=FSMState.MONITORING.value,
            parent_event_id=store.latest_event_id(parent_id, conn=conn),
            conn=conn,
        )

        # Linearize children for stable display order.
        try:
            from work_buddy.threads.linearization import linearize_after_spawn
            linearize_after_spawn(parent_id, conn=conn)
        except Exception as e:
            logger.warning(
                "Linearization after group_thread failed: %s; "
                "siblings keep default order_index=0", e,
            )

        # Kick each child off PROPOSED so it walks through inference
        # like a decompose-spawned child.
        try:
            from work_buddy.threads.kickoff import kickoff_inference
            for cid in child_ids:
                kickoff_inference(cid)
        except Exception as e:
            logger.warning(
                "Group-child kickoff after group_thread failed: %s; "
                "children sit in PROPOSED until manually advanced", e,
            )

        return child_ids
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Move an item between sibling group-children
# ---------------------------------------------------------------------------


def move_item(
    item_id: str,
    src_thread_id: str,
    dest_thread_id: str,
    *,
    actor: str = ACTOR_USER,
    conn=None,
) -> dict[str, Any]:
    """Move a single ContextItem from ``src_thread_id`` to
    ``dest_thread_id``.

    Both threads must:
    - Exist.
    - Share the same umbrella parent (``parent_id`` must be equal AND
      non-NULL).
    - Have the umbrella with ``parent_relationship='group'``.

    Effects:
    - ``src.context_items`` rewritten without the item.
    - ``dest.context_items`` rewritten with the item appended.
    - ``KIND_CONTEXT_ITEM_MOVED`` appended on **both** threads, paired
      via a shared ``migration_id``.
    - ``parent_event_id`` cache bumped on both threads (same fix
      pattern as commit ``8748237b``).

    Returns ``{"migration_id": str, "item": ItemDict}`` on success.

    Raises:
        :class:`GroupRefused` for any precondition failure.
    """
    if src_thread_id == dest_thread_id:
        raise GroupRefused(
            "move_item: source and destination are the same thread",
        )

    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        src = store.get_thread(src_thread_id, conn=conn)
        if src is None:
            raise GroupRefused(f"Source thread {src_thread_id!r} not found")
        dest = store.get_thread(dest_thread_id, conn=conn)
        if dest is None:
            raise GroupRefused(f"Dest thread {dest_thread_id!r} not found")

        if src.parent_id is None or dest.parent_id is None:
            raise GroupRefused(
                "move_item: both threads must have an umbrella parent",
            )
        if src.parent_id != dest.parent_id:
            raise GroupRefused(
                "move_item: cross-umbrella moves not allowed "
                f"({src.parent_id!r} vs {dest.parent_id!r})",
            )

        umbrella = store.get_thread(src.parent_id, conn=conn)
        if umbrella is None:
            raise GroupRefused(
                f"Umbrella thread {src.parent_id!r} not found",
            )
        if umbrella.parent_relationship != "group":
            raise GroupRefused(
                "move_item: umbrella is not a group-relationship parent "
                f"(got {umbrella.parent_relationship!r})",
            )

        # Find the item in src.context_items.
        match: Optional[ContextItem] = None
        new_src_items: list[ContextItem] = []
        for it in src.context_items:
            if it.id == item_id and match is None:
                match = it
            else:
                new_src_items.append(it)
        if match is None:
            raise GroupRefused(
                f"Item {item_id!r} not present on source thread "
                f"{src_thread_id!r}",
            )

        new_dest_items = list(dest.context_items) + [match]
        migration_id = uuid.uuid4().hex[:12]

        # Rewrite both threads' context_items.
        store.update_thread_state(
            src_thread_id,
            context_items=tuple(new_src_items),
            conn=conn,
        )
        store.update_thread_state(
            dest_thread_id,
            context_items=tuple(new_dest_items),
            conn=conn,
        )

        # Audit half 1 — the source.
        store.append_event(
            ThreadEvent(
                thread_id=src_thread_id,
                kind=KIND_CONTEXT_ITEM_MOVED,
                actor=actor,
                migration_id=migration_id,
                data={
                    "direction": "out",
                    "item_id": item_id,
                    "src_thread_id": src_thread_id,
                    "dest_thread_id": dest_thread_id,
                    "umbrella_id": umbrella.thread_id,
                },
            ),
            conn=conn,
        )
        store.update_thread_state(
            src_thread_id,
            parent_event_id=store.latest_event_id(src_thread_id, conn=conn),
            conn=conn,
        )

        # Audit half 2 — the destination.
        store.append_event(
            ThreadEvent(
                thread_id=dest_thread_id,
                kind=KIND_CONTEXT_ITEM_MOVED,
                actor=actor,
                migration_id=migration_id,
                data={
                    "direction": "in",
                    "item_id": item_id,
                    "src_thread_id": src_thread_id,
                    "dest_thread_id": dest_thread_id,
                    "umbrella_id": umbrella.thread_id,
                },
            ),
            conn=conn,
        )
        store.update_thread_state(
            dest_thread_id,
            parent_event_id=store.latest_event_id(
                dest_thread_id, conn=conn,
            ),
            conn=conn,
        )

        return {"migration_id": migration_id, "item": match.to_dict()}
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Cascade: approve umbrella → run Accept on every non-terminal child
# ---------------------------------------------------------------------------


def cascade_approve_umbrella(
    umbrella_id: str,
    *,
    actor: str = ACTOR_USER,
    conn=None,
) -> dict[str, Any]:
    """Run Accept on every non-terminal child of an umbrella.

    Continues on per-child failure (autonomy gate, missing inputs,
    transition not allowed) so one bad child doesn't block the rest.

    Returns::

        {
            "approved": [child_thread_id, ...],
            "failed": [
                {"child_thread_id": str, "error": str}, ...
            ],
            "skipped_terminal": [child_thread_id, ...],
        }

    Raises :class:`GroupRefused` only if the umbrella itself is not
    found / is not a group-relationship parent.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        umbrella = store.get_thread(umbrella_id, conn=conn)
        if umbrella is None:
            raise GroupRefused(f"Umbrella {umbrella_id!r} not found")
        if umbrella.parent_relationship != "group":
            raise GroupRefused(
                f"Umbrella {umbrella_id!r} is not a group-relationship parent",
            )

        approved: list[str] = []
        failed: list[dict[str, Any]] = []
        skipped: list[str] = []

        children = store.list_threads(parent_id=umbrella_id, conn=conn)
        for child in children:
            if child.is_terminal:
                skipped.append(child.thread_id)
                continue
            try:
                _run_child_accept(child.thread_id, actor=actor, conn=conn)
                approved.append(child.thread_id)
            except Exception as e:
                logger.warning(
                    "cascade_approve_umbrella: child %s accept failed: %s",
                    child.thread_id, e,
                )
                failed.append({
                    "child_thread_id": child.thread_id,
                    "error": str(e),
                })

        return {
            "approved": approved,
            "failed": failed,
            "skipped_terminal": skipped,
        }
    finally:
        if own_conn:
            conn.close()


def _run_child_accept(
    child_id: str,
    *,
    actor: str,
    conn,
) -> None:
    """Run the standard Accept transition on a child group sub-thread.

    The right trigger depends on the child's current FSM state:
    children sitting in ``awaiting_confirmation`` get
    ``confirmed_by_user``; children in ``awaiting_consent`` get
    ``approved_by_user``; etc. We delegate the choice to a small
    state→trigger map. If no trigger matches, raise so the cascade
    records this child as failed (the user will need to handle it
    individually).
    """
    from work_buddy.threads.fsm import (
        TRIG_APPROVED_BY_USER,
        TRIG_CONFIRMED_BY_USER,
    )
    state_to_trigger = {
        FSMState.AWAITING_CONFIRMATION: TRIG_CONFIRMED_BY_USER,
        FSMState.AWAITING_CONSENT: TRIG_APPROVED_BY_USER,
    }
    child = store.get_thread(child_id, conn=conn)
    if child is None:
        raise GroupRefused(f"Child {child_id!r} not found")
    trigger = state_to_trigger.get(child.fsm_state)
    if trigger is None:
        raise GroupRefused(
            f"Child {child_id!r} in state {child.fsm_state.value!r} "
            f"has no Accept-equivalent trigger; user must handle individually",
        )
    engine.transition(
        child_id,
        trigger,
        data={"cascade_from_umbrella": True},
        actor=actor,
        conn=conn,
        fire_side_effects=True,
    )


# ---------------------------------------------------------------------------
# Delete an empty (or otherwise unwanted) group sub-thread
# ---------------------------------------------------------------------------


def delete_group_subthread(
    thread_id: str,
    *,
    actor: str = ACTOR_USER,
    conn=None,
) -> dict[str, Any]:
    """DISMISS a child group sub-thread; record audit on the umbrella.

    Typically used when an empty column is no longer wanted (the user
    rearranged items out and now wants to clean up). Empty children
    do NOT auto-DISMISS — the user explicitly invokes this via the
    "X / Delete group sub-thread" header button.

    Returns ``{"dismissed": child_id, "umbrella_id": umbrella_id}``.

    Raises :class:`GroupRefused` if the thread is not a child of a
    group umbrella.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        child = store.get_thread(thread_id, conn=conn)
        if child is None:
            raise GroupRefused(f"Thread {thread_id!r} not found")
        if child.parent_id is None:
            raise GroupRefused(
                f"Thread {thread_id!r} has no parent; not a group child",
            )
        umbrella = store.get_thread(child.parent_id, conn=conn)
        if umbrella is None or umbrella.parent_relationship != "group":
            raise GroupRefused(
                f"Thread {thread_id!r} parent is not a group umbrella",
            )

        # Dismiss the child via the FSM (uses standard transition table).
        if not child.is_terminal:
            try:
                engine.transition(
                    thread_id,
                    TRIG_DISMISSED_BY_USER,
                    data={"reason": "user_deleted_group"},
                    actor=actor,
                    conn=conn,
                    fire_side_effects=True,
                )
            except engine.InvalidTransition as e:
                raise GroupRefused(
                    f"Could not dismiss {thread_id!r}: {e}",
                ) from e

        # Record on the umbrella for audit.
        store.append_event(
            ThreadEvent(
                thread_id=umbrella.thread_id,
                kind=KIND_GROUP_DELETED,
                actor=actor,
                data={
                    "deleted_child_id": thread_id,
                    "had_items": len(child.context_items),
                },
                parent_event_id=store.latest_event_id(
                    umbrella.thread_id, conn=conn,
                ),
            ),
            conn=conn,
        )
        store.update_thread_state(
            umbrella.thread_id,
            parent_event_id=store.latest_event_id(
                umbrella.thread_id, conn=conn,
            ),
            conn=conn,
        )

        return {
            "dismissed": thread_id,
            "umbrella_id": umbrella.thread_id,
        }
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Spawn an empty group child under an existing umbrella
# ---------------------------------------------------------------------------


def spawn_empty_group(
    umbrella_id: str,
    label: str,
    *,
    actor: str = ACTOR_USER,
    conn=None,
) -> str:
    """Add a fresh empty child under ``umbrella_id``.

    Drives the "+ New group" drop zone in the UI: drag selected items
    onto the zone → spawn an empty child here, then immediately call
    :func:`move_item` for each selected item to populate it.

    Returns the new child's thread_id.

    Raises :class:`GroupRefused` if the umbrella is missing or not a
    group-relationship parent.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        umbrella = store.get_thread(umbrella_id, conn=conn)
        if umbrella is None:
            raise GroupRefused(f"Umbrella {umbrella_id!r} not found")
        if umbrella.parent_relationship != "group":
            raise GroupRefused(
                f"Umbrella {umbrella_id!r} is not a group-relationship parent",
            )

        cleaned_label = (label or "").strip() or "New group"
        inciting = {
            "source": "group",
            "parent_id": umbrella_id,
            "cluster_label": cleaned_label,
            "title": cleaned_label,
            "description": cleaned_label,
            "item_count": 0,
            "user_created": True,
        }

        child = Thread(
            parent_id=umbrella_id,
            autonomy_policy=umbrella.autonomy_policy,
            context_items=(),
            inciting_event_summary=inciting,
        )
        store.insert_thread(child, conn=conn)

        # Record on the umbrella so the timeline shows the user-add.
        store.append_event(
            ThreadEvent(
                thread_id=umbrella_id,
                kind=KIND_GROUPS_SPAWNED,
                actor=actor,
                data={
                    "child_thread_ids": [child.thread_id],
                    "child_labels": [cleaned_label],
                    "source_count": 0,
                    "cluster_count": 1,
                    "user_created": True,
                },
                parent_event_id=umbrella.parent_event_id,
            ),
            conn=conn,
        )
        store.update_thread_state(
            umbrella_id,
            parent_event_id=store.latest_event_id(umbrella_id, conn=conn),
            conn=conn,
        )

        # Kick the child off PROPOSED so it walks inference if the
        # user later moves items in. (An empty child has nothing to
        # infer immediately; inference will rerun once items land.)
        try:
            from work_buddy.threads.kickoff import kickoff_inference
            kickoff_inference(child.thread_id)
        except Exception as e:
            logger.warning(
                "spawn_empty_group: kickoff failed for %s: %s",
                child.thread_id, e,
            )

        return child.thread_id
    finally:
        if own_conn:
            conn.close()
