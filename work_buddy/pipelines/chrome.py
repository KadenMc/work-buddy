"""Chrome-triage source pipeline.

Wires the existing Chrome adapter + clustering into the unified
:class:`SourcePipeline` shape. End-to-end flow:

1. **collect** — read currently-open tabs from the ledger via
   ``clarify/adapters/chrome.chrome_tabs_to_items``. Cached Haiku
   summaries (from earlier runs) are attached automatically; new
   tabs land with no summary.
2. **annotate_items** — transfer the cached summary into
   ``CapturedItem.summary`` and synthesise tags from ``domain`` +
   Chrome ``group_title`` so the algorithmic clusterer has signal
   even when an embedding is unavailable. No new LLM call.
3. **precluster** — embedding-fused Louvain via the existing
   ``clarify/cluster.cluster_items`` (weights ``{emb: 0.80, tag:
   0.10, prox: 0.10}`` with window-gated proximity decay). Reuses
   the same Chrome-tuned implementation today's triage modal flow
   uses.
4. **umbrella_summary** — title ``"Chrome triage: <summary>"``.

Stage 4 (LLM cluster refinement) runs through the shared
``refine_clusters``; the Chrome action library declares which
capabilities the LLM may pick.

Action library
--------------

Per-source actions: close all tabs, group in Chrome, move to focus
window, create one task per tab, create umbrella task for the whole
group. The universal library (dismiss / defer / rename) layers on
top.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

from work_buddy.pipelines.actions import (
    CARDINALITY_PER_GROUP,
    ActionDescriptor,
    ActionLibrary,
)
from work_buddy.pipelines.types import CapturedItem, ClusterSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action library — chrome-specific descriptors
# ---------------------------------------------------------------------------


CHROME_ACTIONS: list[ActionDescriptor] = [
    ActionDescriptor(
        capability_name="chrome_tab_close",
        label="Close all tabs",
        description=(
            "Close every tab in this group. Use when the user is done "
            "with the cluster — the session is logged but the tabs no "
            "longer clutter the window."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="x-square",
    ),
    ActionDescriptor(
        capability_name="chrome_tab_group",
        label="Group in Chrome",
        description=(
            "Create or update a Chrome tab group with the cluster's "
            "label as the title. Useful for keeping a focused subset "
            "of tabs together visually."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        default_params={"color": "blue"},
        icon="folder",
    ),
    ActionDescriptor(
        capability_name="chrome_tab_move",
        label="Move to focus window",
        description=(
            "Move every tab in this group to a separate Chrome window "
            "for a focused work session."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="external-link",
    ),
    ActionDescriptor(
        capability_name="chrome_route_to_tasks",
        label="Create one task per tab",
        description=(
            "Walk each tab in this group and create a task in the "
            "master task list. Each tab's title becomes the task "
            "text; the URL is included in the description."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="check-square",
    ),
    ActionDescriptor(
        capability_name="chrome_route_to_umbrella_task",
        label="Create umbrella task",
        description=(
            "Create a single task representing the whole group. The "
            "task text uses the cluster's label; the description "
            "lists every tab's title + URL."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="package",
    ),
]


CHROME_ACTION_LIBRARY = ActionLibrary(CHROME_ACTIONS)
"""The Chrome pipeline's action library. Merged with universal
actions by the runner."""


# ---------------------------------------------------------------------------
# Item conversion helpers
# ---------------------------------------------------------------------------


def _captured_from_triage_dict(td: dict[str, Any]) -> CapturedItem:
    """Convert a TriageItem dict (from ``chrome_tabs_to_items``) into
    a CapturedItem. Carries url / domain / window_id / index in
    payload so the precluster + per-group actions can reach them."""
    metadata = td.get("metadata") or {}
    payload = {
        "url": td.get("url") or "",
        "title": metadata.get("title") or td.get("label") or "",
        "domain": metadata.get("domain") or _domain_of(td.get("url") or ""),
        "tab_id": metadata.get("tab_id"),
        "window_id": metadata.get("window_id"),
        "group_id": metadata.get("group_id"),
        "group_title": metadata.get("group_title") or "",
        "index": metadata.get("index"),
        "pinned": metadata.get("pinned", False),
        "engaged_count": metadata.get("engaged_count", 0),
        "score": metadata.get("score", 0),
    }
    return CapturedItem(
        id=td.get("id") or "tab",
        source="chrome_tab",
        type="tab",
        label=td.get("label") or payload["title"] or td.get("id") or "tab",
        payload=payload,
        # The text returned by chrome_tabs_to_items already includes
        # any cached Haiku summary. Carry it as the CapturedItem's
        # summary so refine_clusters has it to work with.
        summary=td.get("text") or None,
    )


def _domain_of(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc or ""
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _synthesised_tags(payload: dict[str, Any]) -> tuple[str, ...]:
    """Build a tag tuple from Chrome metadata. Used as the tag
    similarity signal in precluster; covers cases where the
    embedding service is unavailable."""
    tags: list[str] = []
    domain = payload.get("domain") or ""
    if domain:
        tags.append(f"domain:{domain}")
    group_title = (payload.get("group_title") or "").strip()
    if group_title:
        tags.append(f"chrome_group:{group_title}")
    return tuple(tags)


# ---------------------------------------------------------------------------
# Pipeline implementation
# ---------------------------------------------------------------------------


class ChromeTriagePipeline:
    """The Chrome-triage data source.

    Implements the :class:`work_buddy.pipelines.SourcePipeline`
    protocol. Construct without arguments; per-run configuration
    (``engagement_window``, ``include_summaries``, ``summary``) flows
    in via ``run_pipeline`` kwargs.
    """

    name = "chrome_triage"

    @property
    def action_library(self) -> ActionLibrary:
        return CHROME_ACTION_LIBRARY

    # ------------------------------------------------------------------
    # Stage 1 — collect
    # ------------------------------------------------------------------

    def collect(
        self,
        *,
        engagement_window: str = "12h",
        include_summaries: bool = True,
        **_unused: Any,
    ) -> list[CapturedItem]:
        """Pull currently-open Chrome tabs from the ledger and convert
        each to a CapturedItem. Cached Haiku summaries (when
        ``include_summaries=True``) are carried through into the
        item's ``summary`` field."""
        from work_buddy.clarify.adapters.chrome import chrome_tabs_to_items

        result = chrome_tabs_to_items(
            engagement_window=engagement_window,
            include_summaries=include_summaries,
        )
        if not result.get("success"):
            logger.warning(
                "chrome pipeline.collect: chrome_tabs_to_items returned "
                "success=False (%s)", result,
            )
            return []
        triage_dicts = result.get("items") or []
        return [_captured_from_triage_dict(td) for td in triage_dicts]

    # ------------------------------------------------------------------
    # Stage 2 — annotate (synthesise tags from chrome metadata)
    # ------------------------------------------------------------------

    def annotate_items(
        self, items: list[CapturedItem],
    ) -> list[CapturedItem]:
        """Synthesise tags from each tab's ``domain`` + ``group_title``.

        No new LLM call — the cached summary loaded by
        :meth:`collect` is already in ``CapturedItem.summary``.
        Future iterations could add a Haiku tag/summary refresh for
        tabs without cached summaries.
        """
        if not items:
            return items
        return [
            ci.augment(tags=_synthesised_tags(ci.payload))
            for ci in items
        ]

    # ------------------------------------------------------------------
    # Stage 3 — precluster (embedding+tag+proximity Louvain)
    # ------------------------------------------------------------------

    def precluster(
        self, items: list[CapturedItem],
    ) -> list[ClusterSpec]:
        """Cluster Chrome tabs using the existing
        ``clarify/cluster.cluster_items`` — same algorithm + weights
        the v4 triage modal uses today, just with the input adapted
        from CapturedItems back to the TriageItem shape that helper
        expects."""
        if not items:
            return []
        try:
            return self._run_chrome_clusterer(items)
        except Exception as e:
            logger.warning(
                "chrome pipeline.precluster: clusterer failed: %s; "
                "falling back to a single Ungrouped cluster", e,
            )
            return [ClusterSpec(
                label="Ungrouped",
                item_ids=tuple(ci.id for ci in items),
            )]

    def _run_chrome_clusterer(
        self, items: list[CapturedItem],
    ) -> list[ClusterSpec]:
        from work_buddy.clarify.cluster import cluster_items as chrome_cluster
        from work_buddy.clarify.items import TriageItem

        # Adapt CapturedItem → TriageItem (the shape Chrome's
        # cluster_items expects). Carry url + metadata so its
        # spatial signals (window_id, index) work.
        triage_items: list[TriageItem] = []
        for ci in items:
            payload = ci.payload or {}
            triage_items.append(TriageItem(
                id=ci.id,
                text=ci.summary or ci.label,
                label=ci.label,
                source="chrome_tab",
                url=payload.get("url") or "",
                metadata={
                    "domain": payload.get("domain"),
                    "title": payload.get("title"),
                    "tab_id": payload.get("tab_id"),
                    "window_id": payload.get("window_id"),
                    "group_id": payload.get("group_id"),
                    "group_title": payload.get("group_title"),
                    "index": payload.get("index"),
                    "engaged_count": payload.get("engaged_count", 0),
                    "score": payload.get("score", 0),
                },
            ))

        chrome_clusters = chrome_cluster(triage_items)
        out: list[ClusterSpec] = []
        for tc in chrome_clusters or []:
            ids = tuple(it.id for it in tc.items)
            if not ids:
                continue
            label = tc.label or "Group"
            out.append(ClusterSpec(label=label, item_ids=ids))
        if not out:
            out = [ClusterSpec(
                label="Ungrouped",
                item_ids=tuple(ci.id for ci in items),
            )]
        return out

    # ------------------------------------------------------------------
    # Stage 5 helper — umbrella inciting summary
    # ------------------------------------------------------------------

    def umbrella_summary(
        self, run_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        scrape_id = run_metadata.get("scrape_id") or run_metadata.get("scan_id")
        scrape_summary = run_metadata.get("summary")
        if scrape_summary:
            title = f"Chrome triage: {scrape_summary}"
        elif scrape_id:
            title = f"Chrome triage: {scrape_id}"
        else:
            title = "Chrome triage"
        return {
            "source": self.name,
            "title": title,
            "description": title,
            "scrape_id": scrape_id,
            "engagement_window": run_metadata.get("engagement_window"),
            "source_pipeline": "chrome_triage",
        }
