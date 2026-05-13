"""Journal-backlog source pipeline.

Wires the existing journal segmentation + tagging + similarity-merge
machinery into the unified :class:`SourcePipeline` shape. End-to-end
flow:

1. **collect** — segment today's Running Notes via the existing
   line-range LLM (``clarify/adapters/journal._segment_with_escalation``).
   Each segment becomes a :class:`CapturedItem`.
2. **annotate_items** — run the existing per-thread tag/summary LLM
   (``journal_backlog/manifest.build_thread_manifest``) so every item
   carries semantic tags + a Haiku-summarised one-liner.
3. **precluster** — embedding-fused similarity clustering. Reuses
   the same Louvain machinery Chrome triage uses
   (``clarify/cluster.py``); journal-specific weights drop the
   proximity signal (``{emb: 0.85, tag: 0.15, prox: 0.0}``).
4. **umbrella_summary** — builds the umbrella thread's
   ``inciting_event_summary``.

Stage 4 (LLM cluster refinement) is shared and runs in
:func:`work_buddy.pipelines.refine_clusters`. The journal pipeline
declares its action library here; the runner merges it with the
universal action library before passing to the LLM.

Action library
--------------

Per-source actions: route to tasks, route to considerations, append
to a vault note, rewrite running notes (umbrella-level cleanup).
The universal library (dismiss / defer / rename) layers on top.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from work_buddy.pipelines.actions import (
    CARDINALITY_PER_GROUP,
    CARDINALITY_UMBRELLA,
    ActionDescriptor,
    ActionLibrary,
)
from work_buddy.pipelines.types import CapturedItem, ClusterSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action library — journal-specific descriptors
# ---------------------------------------------------------------------------


JOURNAL_ACTIONS: list[ActionDescriptor] = [
    ActionDescriptor(
        capability_name="journal_route_to_tasks",
        label="Route to tasks",
        description=(
            "Create one task in the master task list per item in this "
            "group. Each item's first line becomes the task text."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="check-square",
    ),
    ActionDescriptor(
        capability_name="journal_route_to_considerations",
        label="Route to considerations",
        description=(
            "Create one consideration note per item in this group. "
            "Each item's label becomes the title; raw text becomes "
            "the body."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        default_params={"project": "inbox"},
        icon="book-open",
    ),
    ActionDescriptor(
        capability_name="journal_append_to_note",
        label="Append to a note",
        description=(
            "Append all items from this group as bullets to a single "
            "existing vault note (e.g. a project's main note). The "
            "user supplies the note path at approval time."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        default_params={"bullet_prefix": "- "},
        icon="file-plus",
    ),
    ActionDescriptor(
        capability_name="journal_rewrite_running_notes",
        label="Rewrite running notes",
        description=(
            "Remove processed lines from today's daily note "
            "(consent-gated). Umbrella-level: runs once for the "
            "whole scrape after all groups are routed."
        ),
        cardinality=CARDINALITY_UMBRELLA,
        icon="scissors",
    ),
]


JOURNAL_ACTION_LIBRARY = ActionLibrary(JOURNAL_ACTIONS)
"""The journal pipeline's action library. Merged with universal
actions by the runner."""


# ---------------------------------------------------------------------------
# Item conversion
# ---------------------------------------------------------------------------


# Whitelist of metadata fields that are user-meaningful and worth
# carrying through onto the ``CapturedItem.payload``. Anything else
# in the journal segmenter's metadata (``thread_id`` — its INTERNAL
# partition id, ``has_multi_flag`` — its segmenter signal) is
# implementation plumbing and stays internal.
_JOURNAL_PAYLOAD_FIELDS: frozenset[str] = frozenset({
    "journal_date",
    "note_path",
    "line_count",
    "source_dates",
})


def _journal_payload(triage_item: Any) -> dict[str, Any]:
    """Build the payload dict for a journal CapturedItem.

    Carries through the user-meaningful subset of the segmenter's
    metadata + the segment's raw text (truncated to 500 chars; the
    full text lives on the segmenter side). Drops segmenter-internal
    fields that would surface as confusing payload entries (e.g.,
    ``thread_id`` — the segmenter's internal partition id, NOT a
    Thread id).
    """
    md = triage_item.metadata or {}
    payload: dict[str, Any] = {
        k: md[k] for k in _JOURNAL_PAYLOAD_FIELDS if k in md
    }
    raw_text = triage_item.text or ""
    if raw_text:
        payload["raw_text"] = raw_text[:500]
    return payload


# ---------------------------------------------------------------------------
# Pipeline implementation
# ---------------------------------------------------------------------------


class JournalBacklogPipeline:
    """The journal-backlog data source.

    Implements the :class:`work_buddy.pipelines.SourcePipeline`
    protocol. Construct without arguments; per-run configuration
    (``journal_date``, ``profile``) flows in via ``run_pipeline``
    kwargs and lands as ``collect_kwargs`` in ``collect``.
    """

    name = "journal_backlog"

    @property
    def action_library(self) -> ActionLibrary:
        return JOURNAL_ACTION_LIBRARY

    # ------------------------------------------------------------------
    # Stage 1 — collect
    # ------------------------------------------------------------------

    def collect(
        self,
        *,
        journal_date: Optional[str] = None,
        profile: Optional[str] = None,
        **_unused: Any,
    ) -> list[CapturedItem]:
        """Segment today's Running Notes into one CapturedItem per
        line-range cluster.

        Reuses ``clarify/adapters/journal.collect_same_day_candidates``
        (which calls the segmentation LLM with the configured tier
        chain). On segmentation failure across all tiers, returns an
        empty list — the runner then spawns an empty umbrella so the
        operator sees the run executed.
        """
        from work_buddy.clarify.adapters.journal import (
            collect_same_day_candidates,
        )
        from work_buddy.clarify.config import (
            load_triage_config, resolve_profile,
        )

        cfg = load_triage_config()
        seg_profile = resolve_profile(cfg, "segment", override=profile)

        triage_items, _content_hash = collect_same_day_candidates(
            journal_date=journal_date, profile=seg_profile,
        )

        captured: list[CapturedItem] = []
        for ti in triage_items:
            captured.append(CapturedItem(
                id=ti.id,
                source="journal_segment",
                type="todo_line",
                label=ti.label or ti.id,
                payload=_journal_payload(ti),
            ))
        return captured

    # ------------------------------------------------------------------
    # Stage 2 — annotate (LLM tags + summary per item)
    # ------------------------------------------------------------------

    def annotate_items(
        self, items: list[CapturedItem],
    ) -> list[CapturedItem]:
        """Run the per-thread tag/summary LLM (Haiku) over each item.

        The existing ``build_thread_manifest`` helper expects a list
        of thread dicts (id, raw_text, line_count, source_dates,
        has_multi_flag); we adapt CapturedItems to that shape, run
        the manifest, then re-augment the CapturedItems with
        ``tags`` + ``summary`` from the result.

        Per-item failures are non-fatal — items with missing
        manifest entries pass through with empty tags + None
        summary; the precluster step already handles tag-less items
        gracefully via its embedding signal.
        """
        if not items:
            return items
        try:
            from work_buddy.journal_backlog.manifest import (
                build_thread_manifest,
            )
        except Exception as e:
            logger.warning(
                "journal pipeline.annotate_items: manifest import "
                "failed: %s; items pass through unannotated",
                e,
            )
            return items

        thread_dicts = [
            {
                "id": ci.id,
                "raw_text": (ci.payload or {}).get("raw_text") or ci.label,
                "line_count": (ci.payload or {}).get("line_count") or 1,
                "source_dates": (ci.payload or {}).get("source_dates") or [],
                "has_multi_flag": False,
            }
            for ci in items
        ]
        try:
            manifest = build_thread_manifest(thread_dicts)
        except Exception as e:
            logger.warning(
                "journal pipeline.annotate_items: build_thread_manifest "
                "failed: %s; items pass through unannotated",
                e,
            )
            return items

        # manifest is a list of dicts keyed by id; index for fast lookup
        by_id = {entry.get("id"): entry for entry in manifest if entry.get("id")}
        out: list[CapturedItem] = []
        for ci in items:
            entry = by_id.get(ci.id) or {}
            tags = tuple(entry.get("tags") or ())
            summary = entry.get("summary")
            out.append(ci.augment(tags=tags, summary=summary))
        return out

    # ------------------------------------------------------------------
    # Stage 3 — precluster (embedding-fused, Louvain)
    # ------------------------------------------------------------------

    def precluster(
        self, items: list[CapturedItem],
    ) -> list[ClusterSpec]:
        """Cluster items by embedding+tag similarity (Louvain over
        fused signals). Journal weights drop proximity since journal
        items have no spatial relationship.

        On embedding-service unavailability the embedding signal goes
        to zero (vectors stay None → cosine similarity falls to 0.0)
        but the tag signal still drives clustering. As a last-ditch
        fallback if Louvain itself fails, all items collapse into one
        ``Ungrouped`` cluster so the user can re-organise via drag-drop.
        """
        if not items:
            return []
        try:
            return self._run_louvain(items)
        except Exception as e:
            logger.warning(
                "journal pipeline.precluster: Louvain failed: %s; "
                "falling back to single Ungrouped cluster", e,
            )
            return self._fallback_single_cluster(items)

    def _run_louvain(
        self, items: list[CapturedItem],
    ) -> list[ClusterSpec]:
        """Wire the lower-level :mod:`work_buddy.ml.clustering` directly.

        Embedding signal (when service is up) dominates; tag-Jaccard
        provides a fallback when items share manifest-derived tags.
        Proximity is zero — journal items don't have a spatial
        relationship that maps cleanly onto a 1-D index.
        """
        from work_buddy.embedding.client import embed_for_ir
        from work_buddy.ml.clustering import (
            cluster_items as graph_cluster_items,
            compute_pairwise_similarity,
        )

        # Build the dict shape ml.clustering expects.
        item_dicts = [
            {
                "id": ci.id,
                "tags": list(ci.tags or ()),
                "summary": ci.summary or ci.label,
            }
            for ci in items
        ]

        # Embed each item's raw text for the dominant similarity signal.
        # On service-down, embed_for_ir returns None per item; the
        # cosine_similarity helper degrades to 0.0 for None inputs and
        # the tag signal then carries the cluster boundaries.
        texts = [
            (ci.payload or {}).get("raw_text") or ci.label
            for ci in items
        ]
        try:
            embeddings_raw = embed_for_ir(texts)
        except Exception as e:
            logger.warning(
                "journal pipeline.precluster: embed_for_ir failed: %s; "
                "proceeding with tag-only similarity", e,
            )
            embeddings_raw = [None] * len(items)

        # Sanitize — ml.clustering wants list[list[float]]; convert
        # Nones to empty lists and tuples to lists.
        embeddings: list[list[float]] = []
        for e in embeddings_raw or []:
            if e is None:
                embeddings.append([])
            else:
                embeddings.append(list(e))
        # If we got fewer back than expected, pad with empties.
        while len(embeddings) < len(items):
            embeddings.append([])

        pairs = compute_pairwise_similarity(
            item_dicts, embeddings,
            weights={"embedding": 0.85, "tag": 0.15, "proximity": 0.0},
        )
        raw_clusters = graph_cluster_items(
            item_dicts, pairs,
            edge_threshold=0.45,
            resolution=1.2,
        )

        out: list[ClusterSpec] = []
        for rc in raw_clusters or []:
            ids = tuple(rc.get("thread_ids") or ())
            if not ids:
                continue
            label = rc.get("label") or "Group"
            # Tidy the auto-label when ml.clustering falls through
            # to its summary-prefix fallback.
            if not label.strip():
                label = "Group"
            out.append(ClusterSpec(label=label, item_ids=ids))
        return out or self._fallback_single_cluster(items)

    def _fallback_single_cluster(
        self, items: list[CapturedItem],
    ) -> list[ClusterSpec]:
        """Last-ditch fallback when clustering fails entirely."""
        return [ClusterSpec(
            label="Ungrouped",
            item_ids=tuple(ci.id for ci in items),
        )]

    # ------------------------------------------------------------------
    # Stage 5 helper — umbrella inciting summary
    # ------------------------------------------------------------------

    def _resolve_journal_date(self, run_metadata: dict[str, Any]) -> str:
        """Return ``run_metadata['journal_date']`` if present, else today's
        ISO date. Single resolution path used by ``dedup_key`` and
        ``umbrella_summary`` so both agree on the date value that ends up
        in the umbrella's inciting summary.
        """
        journal_date = run_metadata.get("journal_date")
        if not journal_date:
            from datetime import date as _date_cls
            journal_date = _date_cls.today().isoformat()
        return journal_date

    def dedup_key(
        self,
        items: list[CapturedItem],
        run_metadata: dict[str, Any],
    ) -> str | None:
        """One umbrella per ``journal_date``.

        The hourly ``journal-triage-scan`` cron would otherwise spawn a
        fresh umbrella on every fire even when an open one for today
        already exists. Date-only (not content-hash) is deliberate:
        running notes that grow during the day shouldn't fork the
        thread; the user routes items off the single umbrella until it
        reaches a terminal state, at which point a fresh spawn becomes
        valid the next day.
        """
        return f"{self.name}:{self._resolve_journal_date(run_metadata)}"

    def umbrella_summary(
        self, run_metadata: dict[str, Any],
        items: list[CapturedItem] | None = None,
    ) -> dict[str, Any]:
        journal_date = self._resolve_journal_date(run_metadata)
        scan_id = run_metadata.get("scan_id")
        item_count = run_metadata.get("item_count", 0)
        title = f"Daily note: {journal_date}"
        return {
            "source": self.name,
            "title": title,
            "description": title,
            "journal_date": journal_date,
            "scan_id": scan_id,
            "item_count": item_count,
            "source_pipeline": "journal_backlog",
            "dedup_key": f"{self.name}:{journal_date}",
        }
