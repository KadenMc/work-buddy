"""Sub-thread linearization — write-time, persistent.

Per UX.md §8.2: when sub-threads are spawned (via ``decompose``)
or new sub-threads are added later, we compute their semantic
order ONCE and persist it as ``order_index`` on each Thread row.
Render time NEVER recomputes — list query is just
``ORDER BY order_index ASC``.

The function lives here so both the
decompose action AND ad-hoc spawn paths can call it.

Embedding source per sub-thread (per UX.md §8.2):
    context_item.label + " " + json.dumps(context_item.payload)

Falls back to creation-order if the embedding service is
unavailable — the order is "good enough" stability, not
perfect-on-every-edit.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from work_buddy.threads import store
from work_buddy.threads.models import ContextItem, Thread

logger = logging.getLogger(__name__)


def _embed_text_for_thread(thread: Thread) -> str:
    """The text that gets embedded for ``thread`` ordering.

    Per UX.md §8.2: inciting ContextItem's label + payload JSON.
    Falls back to inciting_event_summary if no context items.
    """
    if thread.context_items:
        ci = thread.context_items[0]
        try:
            payload_str = json.dumps(ci.payload or {})
        except (TypeError, ValueError):
            payload_str = ""
        return f"{ci.label or ci.id} {payload_str}".strip()
    summary = thread.inciting_event_summary or {}
    parts = []
    for key in ("description", "summary", "title"):
        val = summary.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    return " ".join(parts) or thread.thread_id


def linearize_siblings(
    parent_id: str,
    *,
    conn=None,
) -> list[str]:
    """Re-compute order_index across every sub-thread of ``parent_id``.

    Returns the list of thread IDs in the new order (also persisted
    in the threads.order_index column).

    No-op if there are 0 or 1 siblings.
    """
    siblings = store.list_threads(parent_id=parent_id, conn=conn)
    if len(siblings) <= 1:
        # Nothing to order
        if siblings:
            store.update_thread_state(
                siblings[0].thread_id, order_index=0, conn=conn,
            )
        return [t.thread_id for t in siblings]

    ordered_ids = _seriate(siblings)
    # Persist order_index in seriation order (0..N-1)
    for idx, tid in enumerate(ordered_ids):
        store.update_thread_state(tid, order_index=idx, conn=conn)
    return ordered_ids


def _seriate(threads: list[Thread]) -> list[str]:
    """Run the seriation pipeline and return ordered thread IDs.

    Falls back to creation-order on embedding failure.
    """
    items = []
    texts = []
    for t in threads:
        items.append({"id": t.thread_id, "tags": []})
        texts.append(_embed_text_for_thread(t))

    embeddings = _embed_texts(texts)
    if embeddings is None or len(embeddings) != len(items):
        logger.info(
            "Linearization: embedding unavailable for parent's %d siblings; "
            "falling back to creation-order.", len(threads),
        )
        # Fallback: creation-order (already what list_threads returns
        # internally for ties — but we explicitly sort by created_at
        # to be deterministic).
        return [
            t.thread_id
            for t in sorted(threads, key=lambda t: t.created_at)
        ]

    try:
        from work_buddy.ml.seriation import seriate_by_embeddings
        result = seriate_by_embeddings(items, embeddings)
        ordered = result.get("order") or []
        if not ordered:
            return [t.thread_id for t in threads]
        return list(ordered)
    except Exception as e:
        logger.warning(
            "Linearization seriate_by_embeddings failed (%s); "
            "falling back to creation-order.", e,
        )
        return [
            t.thread_id
            for t in sorted(threads, key=lambda t: t.created_at)
        ]


def _embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """Try to embed via the shared service; return None on failure."""
    try:
        from work_buddy.embedding.client import embed
        vectors = embed(texts)
        return vectors
    except Exception as e:
        logger.warning("Embedding service call failed: %s", e)
        return None


def linearize_after_spawn(parent_id: str, *, conn=None) -> None:
    """Hook called by decompose / sub-thread spawn paths.

    Idempotent — re-running over the same set is a no-op (same
    embeddings → same order).
    """
    try:
        linearize_siblings(parent_id, conn=conn)
    except Exception as e:
        # Never let a linearization failure block a Thread from
        # being spawned. Log + move on; siblings stay at order_index=0
        # which is fine but degraded.
        logger.warning(
            "Linearization for parent %s failed: %s", parent_id, e,
        )
