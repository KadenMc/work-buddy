"""Summarization-domain ops.

Each op is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).

Lazy imports inside the callables: the funnel pulls in `ir.search` and
`sessions.inspector` which themselves can import sqlite3 / asyncio things;
keeping imports lazy avoids any import-time ordering surprises in the
gateway boot path.
"""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op


def summary_search_op(
    query: str,
    *,
    scope: str | None = None,
    top_k: int = 8,
    drill: bool = False,
    drill_top_k: int = 5,
    drill_per_item_top_k: int = 5,
    method: str = "keyword,semantic",
) -> dict[str, Any]:
    """Coarse-to-fine summary search — surfaces matching summary nodes and
    optionally drills into their source items for raw-span hits.

    ``drill`` defaults to False: a locating pass returns only the compact
    ranking layer (``stage1_hits`` + ``candidate_items``, ~10 KB). Pass
    ``drill=True`` to also inline raw spans for the top items — opt into the
    larger payload once you've picked a candidate."""
    from work_buddy.summarization.funnel import summary_search

    return summary_search(
        query,
        scope=scope,
        top_k=top_k,
        drill=drill,
        drill_top_k=drill_top_k,
        drill_per_item_top_k=drill_per_item_top_k,
        method=method,
    )


def _register() -> None:
    register_op("op.wb.summary_search", summary_search_op)


_register()
