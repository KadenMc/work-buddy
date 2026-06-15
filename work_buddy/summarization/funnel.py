"""Coarse-to-fine retrieval funnel over summarization-framework summaries.

Two stages:

1. **Coarse** — search the `summary` IR source for query-matching `SummaryNode`s.
   Cheap because the corpus is the compressed layer (TLDRs + topic
   titles/summaries/keywords), not raw spans. Each hit carries the
   `namespace` + `item_id` of its parent summarized item, so candidates can
   be ranked by best-summary-score per item.
2. **Fine** (optional) — for each top candidate, drill into its raw source.
   The drill handler is resolved via `work_buddy.summarization.drill_registry`
   (keyed by IR source name). The built-in `summary`-source handler
   dispatches by `namespace` — `conversation_session` routes to
   `session_search`, other namespaces have no drill (returns `None`),
   the funnel still surfaces the coarse hits.

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


def _summary_namespace_drill_dispatch(
    namespace: str,
    item_id: str,
    query: str,
    method: str,
    top_k: int,
) -> Any:
    """Internal namespace dispatcher for the `summary` source's drill handler.

    `conversation_session` -> `session_search`. Other namespaces have no
    registered drill (returns `None` to indicate "no drill available for
    this domain"); the funnel still surfaces the coarse hits.
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


def _default_drill_handler(
    namespace: str,
    item_id: str,
    query: str,
    method: str,
    top_k: int,
) -> Any:
    """Back-compat shim — the original namespace dispatcher.

    Preserved for tests and any out-of-tree consumer that imports it
    directly. Routes through the new namespace dispatcher so behavior
    is identical to the pre-registry version.
    """
    return _summary_namespace_drill_dispatch(
        namespace, item_id, query, method, top_k,
    )


def summary_search(
    query: str,
    *,
    scope: str | None = None,
    top_k: int = 8,
    drill: bool = False,
    drill_top_k: int = 5,
    drill_per_item_top_k: int = 5,
    method: str = "keyword,semantic",
    ir_search_fn: Callable[..., Any] | None = None,
    drill_handler: DrillHandler | None = None,
) -> dict[str, Any]:
    """Two-stage retrieval over framework summaries.

    Args:
        query: Natural-language query.
        scope: Restrict stage-1 to one summary namespace (e.g.
            ``"conversation_session"``); ``None`` searches across all
            summary namespaces. Named ``scope`` to match
            ``context_search`` / ``agent_docs`` vocabulary — it's a
            doc-id prefix filter on the IR index.
        top_k: Stage-1 (coarse) cap — how many summary nodes to consider.
        drill: When True, run stage 2 over candidates. Defaults to False —
            the locating pass returns only the compact ranking layer; raw
            spans are an explicit opt-in to avoid oversized payloads.
        drill_top_k: How many distinct items to drill into (deduplicated
            from `top_k` stage-1 hits by `item_id`, ranked by best score).
        drill_per_item_top_k: How many raw-span hits to return per drilled item.
        method: Search method passed through to both stages
            (`"keyword"`, `"semantic"`, or `"keyword,semantic"`).
        ir_search_fn: Override for tests — defaults to `ir.search.search`.
        drill_handler: Override the per-namespace drill dispatcher; defaults
            to the handler registered under the `summary` source in
            `work_buddy.summarization.drill_registry`. Passing this
            override bypasses the registry entirely (useful for tests).

    Returns a dict with three keys (always present, may be empty):

    - ``stage1_hits`` — list of per-node hits. Each entry has
      ``doc_id`` (IR identity), ``drill_node_id`` (ready to pass straight
      to ``drill_tree(domain="summary", ...)``), ``namespace``,
      ``item_id``, ``level``, ``ordinal``, ``title``, ``summary``,
      ``score``, ``source_ref``, ``generated_at``, ``model``.
    - ``candidate_items`` — per-item aggregate, ranked by best hit. Each
      entry has ``namespace``, ``item_id``, ``best_score``, ``n_hits``,
      ``top_titles`` (up to 3 distinct titles seen for that item),
      ``drill_node_id`` (the item-root drill coordinate).
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
        # Lazy resolve from the registry so tests that swap registry state
        # see their override; falls back to the legacy namespace dispatcher
        # if the registry is empty (e.g. after _reset_for_tests with no
        # re-registration).
        from work_buddy.summarization.drill_registry import get_drill_handler

        drill_handler = (
            get_drill_handler("summary") or _summary_namespace_drill_dispatch
        )

    out: dict[str, Any] = {
        "query": query,
        "scope": scope,
        "stage1_hits": [],
        "candidate_items": [],
        "drilled": {},
    }

    # --- Stage 1: search summary nodes ---
    # `scope` is a namespace string (e.g. "conversation_session"); the IR
    # engine wants a doc-id prefix, so append ':' to match all docs under
    # that namespace.
    ir_scope = f"{scope}:" if scope else None
    raw = ir_search_fn(
        query,
        source="summary",
        scope=ir_scope,
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
        ordinal = meta.get("ordinal")
        # Build the drill-coordinate so callers don't have to translate
        # between IR `doc_id` format (`{ns}:{id}:n{ord}`) and `drill_tree`
        # `node_id` format (`{ns}:{id}#n{ord}` or bare `{ns}:{id}` for
        # the root).
        if isinstance(ordinal, int) and ordinal != 0:
            drill_node_id = f"{ns}:{item_id}#n{ordinal}"
        else:
            drill_node_id = f"{ns}:{item_id}"
        stage1.append({
            "doc_id": r.get("doc_id"),
            "drill_node_id": drill_node_id,
            "namespace": ns,
            "item_id": item_id,
            "level": meta.get("level"),
            "ordinal": ordinal,
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
                # Item-root drill coordinate (no ordinal).
                "drill_node_id": f"{h['namespace']}:{h['item_id']}",
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
