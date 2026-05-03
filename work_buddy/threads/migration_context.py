"""Cross-thread context-item migration — Stage 4.9.

DESIGN.md §12.4 specifies the atomic operation: move a ContextItem
from one Thread to another with linked events on both Threads
sharing a ``migration_id``.

Stage 4.9 deliverable. Module is named ``migration_context`` to
avoid collision with ``work_buddy/threads/migration.py`` (the v4
→ v5 cutover script).

Per UX.md §9.4 / §8.2: context migration does NOT trigger
re-seriation. The two affected Threads keep their ``order_index``
unchanged.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from work_buddy.threads import store
from work_buddy.threads.events import (
    KIND_CONTEXT_ADDED,
    KIND_CONTEXT_REMOVED,
    OptimisticLockConflict,
    ThreadEvent,
)
from work_buddy.threads.models import ContextItem

logger = logging.getLogger(__name__)


class ContextMigrationError(Exception):
    """Raised when migrate_context preconditions fail or both
    sides can't be updated atomically."""


def _new_migration_id() -> str:
    return f"mig-{uuid.uuid4().hex[:12]}"


def migrate_context(
    *,
    item_id: str,
    from_thread_id: str,
    to_thread_id: str,
    actor: str = "user",
) -> str:
    """Atomically move a ContextItem from one Thread to another.

    Both sides emit linked events sharing a ``migration_id``:
    - ``context_removed`` on the source Thread
    - ``context_added`` on the destination Thread

    Returns the migration_id.

    Raises:
    - ContextMigrationError on missing thread / missing item / both-
      sides-same-thread / atomic-write failure.

    Optimistic locking is opportunistic: we read both Threads, write
    in a single transaction, then check that no other event landed
    between read and write. If the conflict surfaces, the entire
    migration aborts and the caller can retry.
    """
    if from_thread_id == to_thread_id:
        raise ContextMigrationError(
            f"from and to threads are the same: {from_thread_id!r}"
        )

    conn = store.get_connection()
    try:
        from_thread = store.get_thread(from_thread_id, conn=conn)
        to_thread = store.get_thread(to_thread_id, conn=conn)
        if from_thread is None:
            raise ContextMigrationError(
                f"source thread not found: {from_thread_id!r}"
            )
        if to_thread is None:
            raise ContextMigrationError(
                f"destination thread not found: {to_thread_id!r}"
            )

        # Find the item on the source side. context_items render-IDs
        # are 1-based ("ci-1", "ci-2", ...) per render.py. We accept
        # either the render ID OR the raw ContextItem.id.
        idx, target = _find_context_item(from_thread.context_items, item_id)
        if target is None:
            raise ContextMigrationError(
                f"item {item_id!r} not found on thread {from_thread_id!r}",
            )

        migration_id = _new_migration_id()

        # Build new context_items tuples
        from_new = (
            tuple(from_thread.context_items[:idx])
            + tuple(from_thread.context_items[idx + 1:])
        )
        to_new = tuple(to_thread.context_items) + (target,)

        # Record the linked events first; on success, update both
        # threads' context_items_json. SQLite is single-writer so
        # this sequence is effectively atomic for our purposes.
        from_event = store.append_event(
            ThreadEvent(
                thread_id=from_thread_id,
                kind=KIND_CONTEXT_REMOVED,
                actor=actor,
                data={
                    "item": target.to_dict(),
                    "to_thread_id": to_thread_id,
                },
                parent_event_id=from_thread.parent_event_id,
                migration_id=migration_id,
            ),
            conn=conn,
        )
        to_event = store.append_event(
            ThreadEvent(
                thread_id=to_thread_id,
                kind=KIND_CONTEXT_ADDED,
                actor=actor,
                data={
                    "item": target.to_dict(),
                    "from_thread_id": from_thread_id,
                },
                parent_event_id=to_thread.parent_event_id,
                migration_id=migration_id,
            ),
            conn=conn,
        )

        # Update context_items_json on both threads
        conn.execute(
            "UPDATE threads SET context_items_json = ?, "
            "parent_event_id = ?, updated_at = datetime('now') "
            "WHERE thread_id = ?",
            (
                json.dumps([c.to_dict() for c in from_new]),
                from_event.id,
                from_thread_id,
            ),
        )
        conn.execute(
            "UPDATE threads SET context_items_json = ?, "
            "parent_event_id = ?, updated_at = datetime('now') "
            "WHERE thread_id = ?",
            (
                json.dumps([c.to_dict() for c in to_new]),
                to_event.id,
                to_thread_id,
            ),
        )
        conn.commit()
        return migration_id
    except OptimisticLockConflict as e:
        conn.rollback()
        raise ContextMigrationError(
            f"optimistic lock conflict during migration: {e}"
        ) from e
    finally:
        conn.close()


def _find_context_item(
    items: tuple[ContextItem, ...], lookup_id: str,
) -> tuple[int, Optional[ContextItem]]:
    """Find a context item by render ID (``ci-N``, 1-based) or
    raw ContextItem.id. Returns (index, item) or (-1, None)."""
    if lookup_id.startswith("ci-"):
        try:
            n = int(lookup_id[len("ci-"):])
            idx = n - 1
            if 0 <= idx < len(items):
                return idx, items[idx]
        except ValueError:
            pass
    # Fallback: match by raw id
    for i, ci in enumerate(items):
        if ci.id == lookup_id:
            return i, ci
    return -1, None
