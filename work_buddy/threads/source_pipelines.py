"""Source-pipeline → v5 Thread spawn helpers.

Stage 4.12 + 4.13 deliverable. The journal scanner and Chrome
triage scanner are existing v4 producers. This module provides
the bridge: take their output (TriageItem-shaped dicts) and
spawn v5 Threads with proper inciting_event_summary so cleanup
adapters can later mutate the source.

UX.md §15 Stage 4.12 (journal) + 4.13 (Chrome).

The helpers are intentionally thin — they don't replace the
existing v4 pipelines; they layer on top. v4 paths still produce
PoolEntries during transition; v5 paths produce Threads.
Stage 4.14 deletes the v4 paths.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from work_buddy.threads import store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_INCITING_EVENT,
    KIND_INTENT_INFERRED,
    KIND_SUBTHREADS_SPAWNED,
    KIND_THREAD_CREATED,
    ThreadEvent,
)
from work_buddy.threads.models import ContextItem, Thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Journal-source spawner
# ---------------------------------------------------------------------------


def spawn_thread_from_journal_item(
    triage_item: dict[str, Any],
    *,
    note_path: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Optional[str]:
    """Create a v5 Thread from a journal-source TriageItem.

    Args:
        triage_item: dict shape — id, text, label, source='journal_thread',
            metadata{thread_id, line_count, journal_date, ...}
        note_path: vault-relative path to the inciting note. If None,
            derive from metadata.journal_date as 'journal/<date>.md'.
            (Matches work_buddy/journal.py's vault-rel path
            convention.)

    Returns:
        New v5 thread_id on success; None on failure (logged).
    """
    if not isinstance(triage_item, dict):
        return None
    md = triage_item.get("metadata") or {}
    if note_path is None:
        journal_date = md.get("journal_date")
        if not journal_date:
            logger.warning(
                "spawn_thread_from_journal_item: no journal_date in metadata "
                "and no note_path passed; can't determine source",
            )
            return None
        note_path = f"journal/{journal_date}.md"

    raw_text = triage_item.get("text") or ""
    label = triage_item.get("label") or triage_item.get("id") or "(journal item)"

    # The cleanup adapter matches by exact line_text. For multi-line
    # threads this is approximate — the adapter walks per-line, so
    # passing the first non-empty line gives a usable handle. The
    # adapter also returns source_already_gone if exact match fails,
    # so a stale or edited journal won't error.
    first_line = next(
        (ln.strip() for ln in raw_text.split("\n") if ln.strip()),
        raw_text.strip(),
    )

    inciting = {
        "source": "journal_note",
        "note_path": note_path,
        "line_text": first_line,
        "journal_date": md.get("journal_date"),
        "thread_id_hint": md.get("thread_id"),
        "line_count": md.get("line_count"),
        "label": label,
        "description": label,
    }

    ctx_item = ContextItem(
        id=triage_item.get("id") or "journal_item",
        source="journal_note",
        type="todo_line",
        label=label,
        payload={"raw_text": raw_text[:500]},
    )

    # Apply default autonomy (PLAN_THEN_REVIEW unless overridden in
    # config). The bare AutonomyPolicy() default would block every
    # wait state and force the user to confirm every inference step.
    from work_buddy.threads.autonomy import default_spawn_policy
    thread = Thread(
        parent_id=parent_id,
        context_items=(ctx_item,),
        inciting_event_summary=inciting,
        autonomy_policy=default_spawn_policy(),
    )
    try:
        store.insert_thread(thread)
        # Inciting event + thread_created
        e1 = store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
            data=inciting,
        ))
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_THREAD_CREATED,
            actor="inciting",
            data={"source_pipeline": "journal_triage_scan"},
            parent_event_id=e1.id,
        ))
        # Bump the cache's parent_event_id so the next transition has
        # the right optimistic-lock target.
        store.update_thread_state(
            thread.thread_id,
            parent_event_id=store.latest_event_id(thread.thread_id),
        )
        # Kickoff transition PROPOSED -> AWAITING_INFERENCE. Fires
        # the bootstrap-registered handler that enqueues into the
        # LLM-call queue. Without this, the thread dead-ends.
        _kickoff_inference(thread.thread_id)
        return thread.thread_id
    except Exception as e:
        logger.warning(
            "spawn_thread_from_journal_item: insert failed: %s", e,
        )
        return None


def _kickoff_inference(thread_id: str) -> None:
    """Fire PROPOSED -> AWAITING_INFERENCE for a freshly-spawned
    Thread. Non-fatal — logs on failure (the thread is already
    persisted; user can manually trigger via dashboard later)."""
    try:
        from work_buddy.threads import engine
        from work_buddy.threads.fsm import TRIG_BEGIN_INFERENCE
        engine.transition(
            thread_id, TRIG_BEGIN_INFERENCE,
            actor="inciting",
            fire_side_effects=True,
        )
    except Exception as e:
        logger.warning(
            "_kickoff_inference for %s failed: %s — thread will sit "
            "in PROPOSED until manually advanced",
            thread_id, e,
        )


def spawn_parent_thread_from_journal_scan(
    *,
    journal_date: str,
    item_count: int,
    scan_id: Optional[str] = None,
) -> Optional[str]:
    """Create the umbrella "scan" Thread for a journal scan (v2).

    Stage 5 v2: the journal scan is a **group umbrella** — it
    spawns N group sub-threads (one per cluster) via
    :func:`work_buddy.threads.group.group_thread`, each holding
    its segments as ``context_items``. The umbrella's pre-recorded
    intent + action reflect the **group** semantics, not decompose.

    - Intent: "Organize daily notes into groups" (confidence 1.0).
    - Action: standard ``"group"`` (confidence 1.0). Distinct from
      ``"decompose"`` — the standard card UI uses
      ``parent_relationship`` to render the column grid, but the
      action label is what surfaces to the user as the umbrella's
      next step (e.g., "Approve all" cascades the group action's
      effect across children).

    The umbrella sits in MONITORING from the start; it never goes
    through inference. As children reach terminal states the
    cascade-on-terminal handler (decompose.cascade_terminal_to_parent)
    advances the umbrella to DONE when all are terminal.

    Returns the new umbrella thread_id, or None on failure.
    """
    if scan_id is None:
        scan_id = uuid.uuid4().hex[:8]
    title = f"Journal scan: {journal_date}"
    description = f"{title} ({item_count} item{'s' if item_count != 1 else ''})"
    inciting = {
        "source": "journal_scan",
        "title": title,
        "description": description,
        "journal_date": journal_date,
        "scan_id": scan_id,
        "item_count": item_count,
    }
    from work_buddy.threads.autonomy import default_spawn_policy
    # Insert directly in MONITORING — the parent is a structural
    # container, not a thing that goes through inference. This
    # mirrors what decompose_thread does for parents that fan out
    # via the decompose action.
    parent = Thread(
        fsm_state=FSMState.MONITORING,
        inciting_event_summary=inciting,
        autonomy_policy=default_spawn_policy(),
    )
    try:
        store.insert_thread(parent)
        # Inciting + thread_created
        e1 = store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
            data=inciting,
        ))
        store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_THREAD_CREATED,
            actor="inciting",
            data={"source_pipeline": "journal_v5_scan"},
            parent_event_id=e1.id,
        ))
        # Pre-record the known intent + action. We use confidence
        # 1.0 because these aren't inferred — they're definitionally
        # true for any journal scan. actor=inciting (not agent)
        # since no LLM was consulted.
        intent_event = store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_INTENT_INFERRED,
            actor="inciting",
            data={
                "target": "intent",
                "payload": {
                    "intent": "Organize daily notes into groups",
                },
                "confidence": 1.0,
                "tier_used": None,
                "model_used": None,
                "synthetic": True,
            },
        ))
        store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="inciting",
            data={
                "target": "action",
                "payload": {
                    "kind": "standard",
                    "name": "group",
                    "plan_summary": (
                        "Cluster items into group sub-threads; "
                        "user re-organizes by drag-drop and "
                        "approves all groups together."
                    ),
                    "irreversibility": "low",
                    "regret_potential": "low",
                    "risk_amplifier": False,
                },
                "confidence": 1.0,
                "tier_used": None,
                "model_used": None,
                "synthetic": True,
            },
            parent_event_id=intent_event.id,
        ))
        # Bump cache parent_event_id so any later writes have the
        # right optimistic-lock target.
        store.update_thread_state(
            parent.thread_id,
            parent_event_id=store.latest_event_id(parent.thread_id),
        )
        return parent.thread_id
    except Exception as e:
        logger.warning(
            "spawn_parent_thread_from_journal_scan: failed: %s", e,
        )
        return None


def _similarity_merge_journal_items(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the similarity-merge pass on TriageItem-shaped journal items.

    Adapts TriageItem dicts (``id`` / ``text`` / ``label`` / ``metadata``)
    to the segment shape ``work_buddy.journal_backlog.similarity`` expects
    (``id`` / ``raw_text`` / ``line_count`` / ``source_dates`` /
    ``has_multi_flag``), runs the merge plan, and converts back. Items that
    aren't part of any merge pass through untouched.

    The merge plan is best-effort. Any exception (embedding service
    pathology, missing optional dependency, etc.) is logged and the
    original items are returned with ``embed_status='error'`` so the spawn
    isn't blocked on a similarity-layer failure.

    Returns ``(merged_items, plan_meta)``. ``plan_meta`` mirrors
    ``similarity.merge_segments``'s second return value plus an extra
    ``error`` key if the merge raised.
    """
    if len(items) < 2:
        return list(items), {
            "before_count": len(items),
            "after_count": len(items),
            "applied_merges": 0,
            "embed_status": "skipped",
            "embedded": 0,
            "skipped": 0,
        }

    try:
        from work_buddy.journal_backlog.similarity import merge_segments
    except Exception as e:
        logger.warning(
            "Could not import journal-backlog similarity merge (%s); "
            "skipping merge pass.", e,
        )
        return list(items), {
            "before_count": len(items),
            "after_count": len(items),
            "applied_merges": 0,
            "embed_status": "import_error",
            "embedded": 0,
            "skipped": 0,
            "error": str(e),
        }

    # TriageItem -> segment shape
    segments: list[dict[str, Any]] = []
    for it in items:
        md = it.get("metadata") or {}
        segments.append({
            "id": it.get("id"),
            "raw_text": it.get("text") or "",
            "line_count": md.get("line_count") or 0,
            "source_dates": md.get("source_dates") or [],
            "has_multi_flag": bool(md.get("has_multi_flag")),
        })

    try:
        merged_segments, meta = merge_segments(segments)
    except Exception as e:
        logger.warning(
            "Similarity merge raised (%s); spawning unmerged items.", e,
        )
        return list(items), {
            "before_count": len(items),
            "after_count": len(items),
            "applied_merges": 0,
            "embed_status": "error",
            "embedded": 0,
            "skipped": 0,
            "error": str(e),
        }

    # Map back to TriageItem shape. For unmerged segments we return the
    # original item unchanged; for merged segments we synthesise a new
    # TriageItem whose text is the concatenated raw_text and whose
    # metadata reflects the merger.
    by_id = {it.get("id"): it for it in items}
    merged_items: list[dict[str, Any]] = []
    for seg in merged_segments:
        sid = seg["id"]
        if "merged_from" not in seg:
            # Unchanged — pass through original.
            merged_items.append(by_id.get(sid) or {
                "id": sid,
                "text": seg.get("raw_text", ""),
                "source": "journal_thread",
                "metadata": {},
            })
            continue
        # Merged — first member's TriageItem provides the metadata
        # template; we overwrite text with the merged raw_text and
        # annotate metadata with the merge audit fields.
        primary = by_id.get(sid) or {}
        new_metadata = dict(primary.get("metadata") or {})
        new_metadata["line_count"] = seg.get("line_count", 0)
        new_metadata["source_dates"] = seg.get("source_dates", [])
        new_metadata["has_multi_flag"] = seg.get("has_multi_flag", False)
        new_metadata["merged_from"] = list(seg.get("merged_from", []))
        new_metadata["merge_score"] = float(seg.get("merge_score", 0.0))
        merged = dict(primary)
        merged["text"] = seg.get("raw_text", "")
        merged["metadata"] = new_metadata
        # The label was a one-line summary of the original first segment;
        # after merging, prefix it with " (+N merged)" so the spawned
        # sub-thread surfaces the merger to the user.
        n_extra = max(0, len(seg.get("merged_from", [])) - 1)
        if n_extra:
            label = primary.get("label") or "(journal item)"
            merged["label"] = f"{label} (+{n_extra} merged)"
        merged_items.append(merged)

    return merged_items, meta


def spawn_threads_from_journal_scan(
    items: list[dict[str, Any]],
    *,
    journal_date: Optional[str] = None,
) -> dict[str, Any]:
    """Spawn the journal-scan thread tree (v2): one umbrella +
    N group children, with the segmented lines as ContextItems on
    each child.

    Each scan produces:
    - A umbrella Thread (``parent_relationship='group'``).
    - N child sub-threads, one per cluster from
      ``journal_backlog.clustering.linearize_threads`` (Jaccard
      tag-similarity seriation). Each child's
      ``inciting_event_summary['cluster_label']`` is generated
      from the cluster's shared tags via
      ``journal_backlog.clustering.cluster_label``.
    - ContextItems on each child = the merged journal segments
      that fall in that cluster.

    Items are pre-merged via the existing similarity fusion layer
    (``_similarity_merge_journal_items``) so over-split LLM
    partitions don't end up as sibling items in the same column.

    Args:
        items: list of TriageItem-shaped dicts from the journal
            adapter.
        journal_date: ``YYYY-MM-DD``; required for the umbrella's
            inciting summary.

    Returns:
        v2 grouping shape::

            {
              "umbrella_id": str,
              "child_thread_ids": [str, ...],
              "total_count": int,    # total items distributed
              "child_count": int,    # number of group children
            }

        Returns a 0-everything dict if the umbrella itself fails
        to spawn or no items survive the merge.
    """
    if journal_date is None:
        if items:
            md = (items[0] or {}).get("metadata") or {}
            journal_date = md.get("journal_date")
    if journal_date is None:
        journal_date = "unknown"

    # Similarity-merge first — collapse over-split LLM segments
    # before clustering. (Same behaviour as the pre-v2 path.)
    merged_items, merge_meta = _similarity_merge_journal_items(items)

    if not merged_items:
        # Empty scan: still spawn an umbrella so the user sees "we
        # ran the scan and there was nothing actionable today".
        # group_thread refuses empty source lists, so go through
        # the umbrella-only path here.
        umbrella_id = spawn_parent_thread_from_journal_scan(
            journal_date=journal_date,
            item_count=0,
        )
        return {
            "umbrella_id": umbrella_id,
            "child_thread_ids": [],
            "total_count": 0,
            "child_count": 0,
        }

    # Spawn the umbrella as a normal decompose-style parent (it'll
    # be flipped to parent_relationship='group' by group_thread).
    umbrella_id = spawn_parent_thread_from_journal_scan(
        journal_date=journal_date,
        item_count=len(merged_items),
    )
    if umbrella_id is None:
        return {
            "umbrella_id": None,
            "child_thread_ids": [],
            "total_count": 0,
            "child_count": 0,
        }

    # Record the merge plan for audit. Best-effort.
    if merge_meta.get("applied_merges"):
        try:
            store.append_event(ThreadEvent(
                thread_id=umbrella_id,
                kind="similarity_merge_applied",
                actor="inciting",
                data={
                    "before_count": merge_meta["before_count"],
                    "after_count": merge_meta["after_count"],
                    "applied_merges": merge_meta["applied_merges"],
                    "embed_status": merge_meta["embed_status"],
                    "embedded": merge_meta.get("embedded"),
                    "skipped": merge_meta.get("skipped"),
                },
            ))
            store.update_thread_state(
                umbrella_id,
                parent_event_id=store.latest_event_id(umbrella_id),
            )
        except Exception as e:
            logger.warning(
                "similarity_merge_applied event for journal umbrella "
                "%s failed: %s", umbrella_id, e,
            )

    # Cluster the merged items into groups using the existing
    # journal_backlog.clustering pipeline. Each cluster becomes one
    # group child; cluster_label generates a human-readable name from
    # shared tags. If clustering fails (missing optional dep, etc.)
    # we fall back to a single "Ungrouped" cluster so spawn still
    # works.
    clusters_input = _build_journal_clusters(merged_items)

    note_path = f"journal/{journal_date}.md"

    # Convert merged_items to ContextItems (mirrors what
    # spawn_thread_from_journal_item used to do for individual
    # threads, but at item-level for group_thread). The cleanup
    # adapter still matches by ``inciting.line_text`` on each
    # *thread* (not item), so we inject the cleanup signal into
    # the per-cluster inciting_summary_extra below.
    ctx_items: list[ContextItem] = []
    items_by_id: dict[str, dict[str, Any]] = {}
    for item in merged_items:
        item_id = item.get("id") or "journal_item"
        items_by_id[item_id] = item
        raw_text = item.get("text") or ""
        label = (item.get("label")
                 or item.get("id")
                 or "(journal item)")
        md = item.get("metadata") or {}
        ctx_items.append(ContextItem(
            id=item_id,
            source="journal_note",
            type="todo_line",
            label=label,
            payload={
                "raw_text": raw_text[:500],
                "journal_date": md.get("journal_date"),
                "line_count": md.get("line_count"),
                "thread_id_hint": md.get("thread_id"),
                "note_path": note_path,
            },
        ))

    try:
        from work_buddy.threads.group import group_thread, GroupRefused
        child_ids = group_thread(
            umbrella_id,
            ctx_items,
            clusters_input,
            inciting_summary_extra={
                "journal_date": journal_date,
                "source_pipeline": "journal_v5_scan",
                "note_path": note_path,
            },
        )
    except GroupRefused as e:
        logger.warning(
            "spawn_threads_from_journal_scan: group_thread refused: %s",
            e,
        )
        return {
            "umbrella_id": umbrella_id,
            "child_thread_ids": [],
            "total_count": 0,
            "child_count": 0,
            "error": str(e),
        }

    return {
        "umbrella_id": umbrella_id,
        "child_thread_ids": child_ids,
        "total_count": len(merged_items),
        "child_count": len(child_ids),
    }


def _build_journal_clusters(
    merged_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cluster merged journal items via Jaccard tag similarity.

    Reuses ``work_buddy.journal_backlog.clustering.linearize_threads``
    (which produces ``[[entry, ...], ...]``) and ``cluster_label``
    (which generates a human-readable name from shared tags).

    Returns the ``[{"label", "item_ids"}, ...]`` shape that
    :func:`work_buddy.threads.group.group_thread` expects.

    Falls back to a single "Ungrouped" cluster on any failure
    (missing optional dep, malformed entries, etc.) — clustering
    is a polish concern, not a correctness one.
    """
    if not merged_items:
        return []
    try:
        from work_buddy.journal_backlog.clustering import (
            cluster_label, linearize_threads,
        )
        from work_buddy.journal_backlog.similarity import (
            extract_inline_tags,
        )
    except Exception as e:
        logger.warning(
            "_build_journal_clusters: clustering deps unavailable: %s; "
            "falling back to single cluster", e,
        )
        return [{
            "label": "Ungrouped",
            "item_ids": [it.get("id") for it in merged_items],
        }]

    # Build entries with id + tags for linearize_threads.
    entries: list[dict[str, Any]] = []
    for it in merged_items:
        item_id = it.get("id") or "journal_item"
        text = it.get("text") or ""
        try:
            tags = list(extract_inline_tags(text))
        except Exception:
            tags = []
        entries.append({"id": item_id, "tags": tags})

    try:
        clusters = linearize_threads(entries, break_threshold=0.15)
    except Exception as e:
        logger.warning(
            "_build_journal_clusters: linearize_threads failed: %s; "
            "falling back to single cluster", e,
        )
        return [{
            "label": "Ungrouped",
            "item_ids": [e["id"] for e in entries],
        }]

    out: list[dict[str, Any]] = []
    for cluster in clusters:
        if not cluster:
            continue
        try:
            label = cluster_label(cluster)
        except Exception:
            label = "Group"
        # ``cluster_label`` returns "Untagged" / "Empty" when there
        # are no tags to summarise. Use a friendlier "Ungrouped"
        # consistent with the rest of the v2 group UI naming.
        if label in ("Untagged", "Empty", "", None):
            label = "Ungrouped"
        out.append({
            "label": label,
            "item_ids": [e["id"] for e in cluster],
        })
    return out or [{
        "label": "Ungrouped",
        "item_ids": [e["id"] for e in entries],
    }]


# ---------------------------------------------------------------------------
# Chrome-source spawner (Stage 4.13 — also lives here for symmetry)
# ---------------------------------------------------------------------------


def spawn_parent_thread_from_chrome_scrape(
    *,
    scrape_id: Optional[str] = None,
    summary: Optional[str] = None,
    parent_relationship: str = "decompose",
    originating_scrape_id: Optional[str] = None,
    cluster_label: Optional[str] = None,
    cluster_index: Optional[int] = None,
    cluster_size: Optional[int] = None,
) -> Optional[str]:
    """Create a parent Thread for a Chrome triage scrape.

    Stage 5: when called with ``parent_relationship='group'`` this
    becomes a group-relationship parent — one of N sibling group-
    parents that together represent one Chrome scrape's cluster set.
    Each cluster gets its own parent; items can move between
    siblings via the move endpoint.

    Args:
        scrape_id: per-scrape id (legacy; carried in inciting summary).
        summary: short title shown in the dashboard list.
        parent_relationship: 'decompose' (legacy single-parent
            behaviour) or 'group' (Stage 5 multi-parent).
        originating_scrape_id: sibling-scope id for group-parents.
            REQUIRED when ``parent_relationship='group'`` — siblings
            recognise each other by this id.
        cluster_label / cluster_index / cluster_size: optional
            cluster metadata for group-parents (carried in the
            inciting summary so the dashboard can show "Cluster 2 of
            5: 'Research'").

    Returns the new thread_id. Sub-Threads are spawned by the caller
    (``spawn_threads_from_chrome_scrape``).
    """
    inciting: dict[str, Any] = {
        "source": "chrome_scrape",
        "scrape_id": scrape_id,
    }
    # Title: prefer cluster-aware label when supplied; fall back to
    # the scrape-wide summary.
    if cluster_label:
        title = cluster_label
        if cluster_index is not None and cluster_size is not None:
            inciting["description"] = (
                f"{cluster_label} (cluster {cluster_index + 1} of "
                f"{cluster_size})"
            )
        else:
            inciting["description"] = cluster_label
    else:
        title = summary or "Chrome triage"
        inciting["description"] = summary or "Chrome triage"
    inciting["title"] = title
    if cluster_index is not None:
        inciting["cluster_index"] = cluster_index
    if cluster_size is not None:
        inciting["cluster_size"] = cluster_size

    from work_buddy.threads.autonomy import default_spawn_policy
    # Group-parents start in MONITORING immediately — they have no
    # action of their own to infer and exist purely as containers.
    # Decompose-parents start in PROPOSED and walk through inference
    # the legacy way.
    if parent_relationship == "group":
        parent = Thread(
            inciting_event_summary=inciting,
            autonomy_policy=default_spawn_policy(),
            parent_relationship="group",
            originating_scrape_id=originating_scrape_id,
            fsm_state=FSMState.MONITORING,
        )
    else:
        parent = Thread(
            inciting_event_summary=inciting,
            autonomy_policy=default_spawn_policy(),
        )
    try:
        store.insert_thread(parent)
        e1 = store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
            data=inciting,
        ))
        store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_THREAD_CREATED,
            actor="inciting",
            data={
                "source_pipeline": "chrome_triage",
                "parent_relationship": parent_relationship,
                "originating_scrape_id": originating_scrape_id,
            },
            parent_event_id=e1.id,
        ))
        store.update_thread_state(
            parent.thread_id,
            parent_event_id=store.latest_event_id(parent.thread_id),
        )
        return parent.thread_id
    except Exception as e:
        logger.warning(
            "spawn_parent_thread_from_chrome_scrape: failed: %s", e,
        )
        return None


def chrome_tab_to_context_item(tab: dict[str, Any]) -> ContextItem:
    """Convert a chrome-tab dict into a ContextItem suitable for
    decompose. Used by the Chrome pipeline (Stage 4.13)."""
    tab_id = tab.get("id") or tab.get("tab_id") or "tab"
    title = tab.get("title") or tab.get("url") or tab_id
    return ContextItem(
        id=str(tab_id),
        source="chrome_tab",
        type="tab",
        label=str(title),
        payload={
            "url": tab.get("url"),
            "title": title,
            "window_id": tab.get("window_id"),
            "group_id": tab.get("group_id"),
            "tab_index": tab.get("tab_index"),
        },
    )


def spawn_threads_from_chrome_scrape(
    *,
    tabs: list[dict[str, Any]],
    scrape_id: Optional[str] = None,
    summary: Optional[str] = None,
    clusters: Optional[list[dict[str, Any]]] = None,
    use_grouping: bool = True,
) -> Optional[dict[str, Any]]:
    """End-to-end Chrome scrape → v5 Thread tree.

    Stage 5 v2 (current): spawns one **umbrella** thread per scrape
    (parent_relationship='group') plus N child sub-threads (one per
    cluster). Each child holds its cluster's tabs as ``context_items``
    on a normal Thread row — no per-tab sub-thread. Items move
    between sibling children via
    :func:`work_buddy.threads.group.move_item`.

    Falls back to the legacy single-decompose-parent shape (each tab
    is its own sub-thread, no umbrella) when:
    - ``use_grouping`` is explicitly False.
    - ``clusters`` is empty / None.

    Args:
        tabs: list of tab dicts (id, url, title, window_id, ...).
        scrape_id: per-scrape id (carried in inciting summary).
        summary: scrape-wide short title.
        clusters: optional clustering output. Each entry:
            ``{"label": str, "item_ids": [str, ...]}`` (legacy
            ``"tab_ids"`` accepted too). Tabs not referenced by any
            cluster fall into a synthetic "Ungrouped" child so
            nothing is silently dropped.
        use_grouping: when True (default) AND clusters are supplied,
            use the v2 umbrella+groups pattern. When False, spawn
            the legacy single-decompose-parent shape.

    Returns:
        Stage 5 v2 grouping shape::

            {
              "umbrella_id": str,
              "child_thread_ids": [str, ...],
              "total_count": int,    # total items distributed
              "child_count": int,    # number of group children spawned
            }

        Legacy shape (when ``use_grouping=False`` or no clusters)::

            {"parent_id": str, "sub_thread_ids": [str, ...], "count": int}

        None on failure (logged).
    """
    if not tabs:
        return None

    legacy_mode = (not use_grouping) or not clusters
    if legacy_mode:
        return _spawn_chrome_scrape_legacy(
            tabs=tabs, scrape_id=scrape_id, summary=summary,
        )

    # Stage 5 grouping path.
    return _spawn_chrome_scrape_grouped(
        tabs=tabs,
        scrape_id=scrape_id,
        summary=summary,
        clusters=clusters,
    )


def _spawn_chrome_scrape_legacy(
    *,
    tabs: list[dict[str, Any]],
    scrape_id: Optional[str],
    summary: Optional[str],
) -> Optional[dict[str, Any]]:
    """Pre-Stage-5 single-decompose-parent path (kept for callers that
    explicitly opt out of grouping, and for the empty-clusters
    fallback). All tabs become sub-threads of one big parent."""
    parent_id = spawn_parent_thread_from_chrome_scrape(
        scrape_id=scrape_id, summary=summary,
    )
    if parent_id is None:
        return None
    try:
        from work_buddy.threads.decompose import decompose_thread
        ctx_items = [chrome_tab_to_context_item(t) for t in tabs]
        sub_ids = decompose_thread(
            parent_id, ctx_items,
            inciting_summary_extra={
                "scrape_id": scrape_id,
                "source_pipeline": "chrome_triage",
            },
        )
        return {
            "parent_id": parent_id,
            "sub_thread_ids": sub_ids,
            "count": len(sub_ids),
        }
    except Exception as e:
        logger.warning(
            "spawn_threads_from_chrome_scrape (legacy): decompose failed: %s", e,
        )
        return {
            "parent_id": parent_id,
            "sub_thread_ids": [],
            "count": 0,
            "error": str(e),
        }


def _spawn_chrome_scrape_grouped(
    *,
    tabs: list[dict[str, Any]],
    scrape_id: Optional[str],
    summary: Optional[str],
    clusters: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Stage 5 v2 path: one **umbrella** + N group children, items
    held inside each child as ``context_items``.

    The umbrella is a normal Thread with ``parent_relationship='group'``;
    its children are normal Threads (one per cluster), each with the
    cluster's tabs as ContextItems on its ``context_items`` tuple. The
    UI's drag/drop column grid lives on the umbrella's "Sub-threads"
    section; items can move between sibling children via
    :func:`work_buddy.threads.group.move_item`.

    Tabs not referenced by any cluster fall into a synthetic
    "Ungrouped" child (mirrors the old per-cluster sibling) so nothing
    is silently dropped on the floor.
    """
    if not tabs:
        return None

    # Build a tab_id -> tab map for fast cluster expansion.
    tabs_by_id: dict[str, dict[str, Any]] = {}
    for t in tabs:
        tid = str(t.get("id") or t.get("tab_id") or "")
        if tid:
            tabs_by_id[tid] = t

    # Convert to ContextItems indexed by id (canonical "items").
    items: list[ContextItem] = []
    for t in tabs:
        ci = chrome_tab_to_context_item(t)
        items.append(ci)
    if not items:
        return None

    # Spawn the umbrella as a normal decompose-style parent (it'll be
    # flipped to parent_relationship='group' by group_thread).
    umbrella_id = spawn_parent_thread_from_chrome_scrape(
        scrape_id=scrape_id,
        summary=summary,
        # Carry sibling-scope-id metadata even though the new model
        # doesn't gate on it (siblings now share an umbrella parent
        # instead). Useful for debugging older databases.
        originating_scrape_id=scrape_id,
    )
    if umbrella_id is None:
        return None

    # Hand off to group_thread, which buckets items by cluster,
    # creates the children, marks the umbrella, and kicks each child
    # off PROPOSED. Cluster spec is the same shape Chrome already
    # emits — group_thread accepts both ``item_ids`` and the legacy
    # ``tab_ids`` key.
    from work_buddy.threads.group import group_thread, GroupRefused
    try:
        child_ids = group_thread(
            umbrella_id,
            items,
            clusters,
            inciting_summary_extra={
                "scrape_id": scrape_id,
                "source_pipeline": "chrome_triage",
            },
        )
    except GroupRefused as e:
        logger.warning(
            "spawn_threads_from_chrome_scrape: group_thread refused: %s", e,
        )
        return {
            "umbrella_id": umbrella_id,
            "child_thread_ids": [],
            "total_count": 0,
            "child_count": 0,
            "error": str(e),
        }

    return {
        "umbrella_id": umbrella_id,
        "child_thread_ids": child_ids,
        "total_count": len(items),
        "child_count": len(child_ids),
    }


# ---------------------------------------------------------------------------
# Chrome-tab cleanup adapter (stub — closing tabs via the existing
# Chrome native-messaging host is not currently supported; the host
# only exports tab state. This adapter ships as a placeholder that
# returns a clean "not yet implemented" failure so the UI's Clean
# Up button on Chrome tabs surfaces honestly.)
# ---------------------------------------------------------------------------


def _chrome_tab_can_clean_up(thread) -> bool:  # type: ignore[no-untyped-def]
    summary = getattr(thread, "inciting_event_summary", None) or {}
    # We say "yes we can" so the UI shows the button; the cleanup
    # call returns a friendly failure detail. This is intentionally
    # discoverable: users learn the gap and can ask for it to be
    # implemented.
    return summary.get("source") == "chrome_tab"


def _chrome_tab_cleanup(thread):  # type: ignore[no-untyped-def]
    from work_buddy.threads.cleanup import CleanupResult
    return CleanupResult(
        success=False,
        detail=(
            "Chrome tab cleanup is not yet wired (the extension's "
            "native-messaging host is export-only today). Tab will "
            "remain open; please close it manually."
        ),
    )


def register_chrome_tab_cleanup_adapter() -> None:
    """Register the Chrome-tab cleanup adapter. Bootstrap calls this
    alongside the journal adapter (Stage 4.4 + 4.13)."""
    from work_buddy.threads.cleanup import (
        CleanupAdapter, register_cleanup_adapter,
    )
    register_cleanup_adapter(CleanupAdapter(
        source="chrome_tab",
        can_clean_up=_chrome_tab_can_clean_up,
        cleanup=_chrome_tab_cleanup,
        description="(stub) close the Chrome tab — not yet wired.",
    ))
