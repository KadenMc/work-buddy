"""Coarse-to-fine retrieval funnel over summarization-framework summaries.

Two stages:

1. **Coarse** — search the `summary` IR source for query-matching `SummaryNode`s.
   Cheap because the corpus is the compressed layer (TLDRs + topic
   titles/summaries/keywords), not raw spans. Each hit carries the
   `namespace` + `item_id` of its parent summarized item, so candidates can
   be ranked by best-summary-score per item.
2. **Fine** (optional) — for each top candidate, drill into its raw source.
   Today this dispatches to `session_search(session_id=item_id, query=...)`
   for the `conversation_session` namespace. Other namespaces (Chrome page,
   future doc/event-stream summarizers) can plug a custom drill handler in
   later — the funnel itself stays the same shape.

The shape of the return preserves both views: per-node summary hits (useful
for "find the topic about X") and per-item aggregates (useful for "which
sessions matter for X") plus, when drilled, the actual raw-span hits.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


# A drill handler signature: (namespace, item_id, query, method, top_k)
#   -> list[dict] (the raw-span hits in whatever shape the domain uses)
DrillHandler = Callable[[str, str, str, str, int], Any]


def _default_drill_handler(
    namespace: str,
    item_id: str,
    query: str,
    method: str,
    top_k: int,
) -> Any:
    """Dispatch the drill stage based on namespace.

    `conversation_session` -> `session_search`. Other namespaces have no
    registered drill today (returns `None` to indicate "no drill available
    for this domain"); the funnel still surfaces the coarse hits.
    """
    if namespace == "conversation_session":
        from work_buddy.sessions.inspector import session_search

        try:
            result = session_search(
                session_id=item_id,
                query=query,
                method=method,
                top_k=top_k,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "summary_search drill failed for %s:%s — %s",
                namespace, item_id, exc,
            )
            return None
        return result
    return None


def summary_search(
    query: str,
    *,
    namespace: str | None = None,
    top_k: int = 8,
    drill: bool = True,
    drill_top_k: int = 5,
    drill_per_item_top_k: int = 5,
    method: str = "keyword,semantic",
    ir_search_fn: Callable[..., Any] | None = None,
    drill_handler: DrillHandler | None = None,
) -> dict[str, Any]:
    """Two-stage retrieval over framework summaries.

    Args:
        query: Natural-language query.
        namespace: Restrict stage-1 to one summary namespace (e.g.
            `"conversation_session"`); `None` searches across all
            summary namespaces.
        top_k: Stage-1 (coarse) cap — how many summary nodes to consider.
        drill: When True, run stage 2 over candidates.
        drill_top_k: How many distinct items to drill into (deduplicated
            from `top_k` stage-1 hits by `item_id`, ranked by best score).
        drill_per_item_top_k: How many raw-span hits to return per drilled item.
        method: Search method passed through to both stages
            (`"keyword"`, `"semantic"`, or `"keyword,semantic"`).
        ir_search_fn: Override for tests — defaults to `ir.search.search`.
        drill_handler: Override the per-namespace drill dispatcher; defaults
            to one that routes `conversation_session` to `session_search`.

    Returns a dict with three keys (always present, may be empty):

    - ``stage1_hits`` — list of per-node hits, each with ``namespace``,
      ``item_id``, ``level``, ``title``, ``summary``, ``score``,
      ``source_ref``, ``generated_at``, ``model``.
    - ``candidate_items`` — per-item aggregate, ranked by best hit. Each
      entry has ``namespace``, ``item_id``, ``best_score``, ``n_hits``,
      ``top_titles`` (up to 3 distinct titles seen for that item).
    - ``drilled`` — dict mapping ``item_id`` to the drill handler's result
      (typically a `session_search`-shaped object), or ``None`` per item
      when the namespace has no drill handler. Empty when ``drill=False``.

    On stage-1 failure (e.g. embedding service down), ``stage1_hits`` is
    empty and ``error`` is set in the return dict.
    """
    if ir_search_fn is None:
        from work_buddy.ir.search import search as _ir_search

        ir_search_fn = _ir_search
    if drill_handler is None:
        drill_handler = _default_drill_handler

    out: dict[str, Any] = {
        "query": query,
        "namespace": namespace,
        "stage1_hits": [],
        "candidate_items": [],
        "drilled": {},
    }

    # --- Stage 1: search summary nodes ---
    scope = f"{namespace}:" if namespace else None
    raw = ir_search_fn(
        query,
        source="summary",
        scope=scope,
        top_k=top_k,
        method=method,
    )
    if isinstance(raw, str):  # error string
        out["error"] = raw
        return out
    if not isinstance(raw, list):
        out["error"] = f"unexpected ir.search return: {type(raw).__name__}"
        return out

    stage1: list[dict[str, Any]] = []
    for r in raw:
        meta = r.get("metadata") or {}
        ns = meta.get("namespace") or ""
        item_id = meta.get("item_id") or ""
        if not item_id:
            continue
        extra = meta.get("extra") or {}
        title = extra.get("title") or ""
        stage1.append({
            "doc_id": r.get("doc_id"),
            "namespace": ns,
            "item_id": item_id,
            "level": meta.get("level"),
            "title": title,
            "summary": r.get("display_text") or "",
            "score": r.get("score", 0.0),
            "source_ref": meta.get("source_ref"),
            "generated_at": meta.get("generated_at"),
            "model": meta.get("model"),
        })
    out["stage1_hits"] = stage1

    # --- Stage 1.5: aggregate to candidate items ---
    by_item: dict[tuple[str, str], dict[str, Any]] = {}
    for h in stage1:
        key = (h["namespace"], h["item_id"])
        agg = by_item.get(key)
        if agg is None:
            agg = {
                "namespace": h["namespace"],
                "item_id": h["item_id"],
                "best_score": h["score"],
                "n_hits": 0,
                "top_titles": [],
            }
            by_item[key] = agg
        agg["n_hits"] += 1
        if h["score"] > agg["best_score"]:
            agg["best_score"] = h["score"]
        if h["title"] and h["title"] not in agg["top_titles"]:
            if len(agg["top_titles"]) < 3:
                agg["top_titles"].append(h["title"])
    candidates = sorted(
        by_item.values(),
        key=lambda a: a["best_score"],
        reverse=True,
    )
    out["candidate_items"] = candidates

    # --- Stage 2: drill into top items ---
    if drill:
        drilled: dict[str, Any] = {}
        for cand in candidates[:drill_top_k]:
            ns = cand["namespace"]
            iid = cand["item_id"]
            res = drill_handler(ns, iid, query, method, drill_per_item_top_k)
            drilled[iid] = res
        out["drilled"] = drilled

    return out
