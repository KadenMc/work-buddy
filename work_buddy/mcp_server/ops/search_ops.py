"""Search-domain ops.

`find` is the structured-returning universal IR search verb. Returns a
plain `list[dict]` when `drill=False` (mirroring `ir.search.search`'s
output) or the funnel-shaped dict (`stage1_hits` + `candidate_items` +
`drilled`) when `drill=True`. Sources with a registered drill handler
(see `work_buddy.summarization.drill_registry`) get per-hit drill; sources
without one get an empty `drilled` block.

`context_search` is the markdown-formatted twin (see
`work_buddy.mcp_server.ops.context_ops`). The two ops share the same
underlying IR engine but emit different return shapes for different
consumers.

Lazy imports inside the callables to avoid pulling backends into the
gateway boot path.
"""

from __future__ import annotations

import logging
from typing import Any

from work_buddy.mcp_server.op_registry import register_op

logger = logging.getLogger(__name__)


def find_op(
    query: str,
    *,
    source: str | None = None,
    scope: str | None = None,
    drill: bool = False,
    top_k: int = 10,
    method: str = "keyword,semantic",
    recency: bool | None = None,
    drill_top_k: int = 5,
    drill_per_item_top_k: int = 5,
) -> Any:
    """Structured IR search across any indexed source.

    Args:
        query: Natural-language query string.
        source: Filter by source name (``"conversation"``, ``"summary"``,
            ``"chrome"``, ``"task_note"``, ``"docs"``, ``"projects"``).
            Omit for cross-source search.
        scope: Doc-id prefix filter (source-specific). For
            ``source="conversation"``, a ``session_id``. For
            ``source="summary"``, a ``namespace`` (the funnel appends
            the trailing ``:`` automatically).
        drill: When True, after stage-1 ranking, run a per-source drill
            handler against the top items. Returns the funnel-shape dict
            (``stage1_hits`` + ``candidate_items`` + ``drilled``).
        top_k: Stage-1 cap (default 10).
        method: ``"keyword"``, ``"semantic"``, ``"keyword,semantic"`` or
            ``"substring"``. ``"substring"`` is solo-only.
        recency: Apply recency bias (default per config; pass False for
            time-insensitive ranking).
        drill_top_k: When drilling, how many distinct items to drill
            (default 5).
        drill_per_item_top_k: When drilling, how many raw-span hits per
            item (default 5).

    Returns:
        - ``list[dict]`` when ``drill=False`` — the raw IR hit list from
          ``ir.search.search`` (each dict has ``doc_id``, ``score``,
          ``source``, ``display_text``, ``metadata``).
        - ``dict`` when ``drill=True`` — the funnel shape
          (``{query, scope, stage1_hits, candidate_items, drilled}``).
          On error, ``{stage1_hits: [], candidate_items: [], drilled: {},
          error: "..."}``.
        - ``str`` (error string) when ``drill=False`` and IR returns one
          (e.g. embedding service unavailable).
    """
    from work_buddy.ir.search import search as ir_search

    if not drill:
        # Route the plain (non-drill) path to the consolidated index when the `find`
        # consumer flag is on and the request is in the supported subset (single source,
        # no scope, hybrid-mappable method); fall back to the IR engine on any miss. The
        # helper returns the same list[dict] shape ir.search.search does. (drill=True keeps
        # using the IR engine + its source-specific drill handlers.)
        from work_buddy.mcp_server.ops.context_consolidated import (
            search_context_via_consolidated,
        )
        routed = search_context_via_consolidated(
            query, top_k=top_k, source=source, scope=scope,
            method=method, recency=recency, consumer="find",
        )
        if routed is not None:
            return routed
        return ir_search(
            query,
            top_k=top_k,
            source=source,
            scope=scope,
            method=method,
            recency=recency,
        )

    # drill=True — route through the funnel shape with per-source drill
    # handler lookup.
    from work_buddy.summarization.drill_registry import get_drill_handler
    from work_buddy.summarization.funnel import summary_search

    # For the `summary` source, the existing funnel shape is exactly right.
    # Funnel internally consults the registry for the `summary` handler.
    if source == "summary" or source is None:
        return summary_search(
            query,
            scope=scope,
            top_k=top_k,
            drill=True,
            drill_top_k=drill_top_k,
            drill_per_item_top_k=drill_per_item_top_k,
            method=method,
        )

    # For non-summary sources, run a generic funnel-shape: stage-1 IR,
    # aggregate, run drill handler if registered (else empty drilled).
    out: dict[str, Any] = {
        "query": query,
        "scope": scope,
        "stage1_hits": [],
        "candidate_items": [],
        "drilled": {},
    }
    raw = ir_search(
        query,
        top_k=top_k,
        source=source,
        scope=scope,
        method=method,
        recency=recency,
    )
    if isinstance(raw, str):
        out["error"] = raw
        return out
    if not isinstance(raw, list):
        out["error"] = f"unexpected ir.search return: {type(raw).__name__}"
        return out

    # The non-summary stage1 shape mirrors summary stage1 where applicable;
    # fields not present in the source's metadata are simply omitted.
    stage1: list[dict[str, Any]] = []
    by_item: dict[tuple[str, str], dict[str, Any]] = {}
    for r in raw:
        meta = r.get("metadata") or {}
        # Non-summary sources may not have `namespace` / `item_id`; fall
        # back to the source name and `doc_id` so aggregation still works
        # per-doc rather than crashing.
        ns = meta.get("namespace") or source or ""
        item_id = meta.get("item_id") or r.get("doc_id") or ""
        if not item_id:
            continue
        entry = {
            "doc_id": r.get("doc_id"),
            "namespace": ns,
            "item_id": item_id,
            "title": meta.get("title") or "",
            "summary": r.get("display_text") or "",
            "score": r.get("score", 0.0),
            "metadata": meta,
        }
        stage1.append(entry)
        key = (ns, item_id)
        agg = by_item.get(key)
        if agg is None:
            agg = {
                "namespace": ns,
                "item_id": item_id,
                "best_score": entry["score"],
                "n_hits": 0,
                "top_titles": [],
            }
            by_item[key] = agg
        agg["n_hits"] += 1
        if entry["score"] > agg["best_score"]:
            agg["best_score"] = entry["score"]
        if entry["title"] and entry["title"] not in agg["top_titles"]:
            if len(agg["top_titles"]) < 3:
                agg["top_titles"].append(entry["title"])
    out["stage1_hits"] = stage1
    out["candidate_items"] = sorted(
        by_item.values(), key=lambda a: a["best_score"], reverse=True,
    )

    handler = get_drill_handler(source) if source else None
    if handler is not None:
        drilled: dict[str, Any] = {}
        for cand in out["candidate_items"][:drill_top_k]:
            res = handler(
                cand["namespace"],
                cand["item_id"],
                query,
                method,
                drill_per_item_top_k,
            )
            drilled[cand["item_id"]] = res
        out["drilled"] = drilled
    else:
        logger.debug(
            "find(drill=True) — no drill handler registered for source %r",
            source,
        )

    return out


def _register() -> None:
    # ``replace=True`` so a registry reload (via the importlib.reload path
    # in ``op_registry.load_builtin_ops``) re-binds the op cleanly rather
    # than crashing on the already-registered name. Pytest's test
    # collection triggers this path when one test directly imports an op
    # module and a later test calls ``load_builtin_ops`` — without
    # ``replace=True`` the reload re-runs this ``_register()`` and the
    # duplicate-registration check fires.
    register_op("op.wb.find", find_op, replace=True)


_register()
