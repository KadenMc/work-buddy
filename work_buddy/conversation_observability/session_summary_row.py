"""Canonical accessor for the `session_summaries` + `topic_summaries`
row shape.

Returns one Python dict per session, assembled from the summarization
framework's per-node store. Consumed by:

- The dashboard `GET /api/chats/<sid>/topics` endpoint.
- The `claude_session_summary` context collector's `include_tldr` branch.
- The MCP op `session_summary_get` (and its alias
  `conversation_observability_summary_get`).

The compatibility shims in `summaries.py` (`query_session_summary`,
`query_topic_summaries`) wrap the same logic and delegate here for new
code.

**Why this reads `summarization` storage directly, not `drill_tree`:**
the agent-facing `drill_tree(domain="summary", ...)` returns
`TreeView.ChildRef` carrying only `node_id` / `title` / `summary_text` —
each child's `extra` dict (which holds `span_start`, `span_end`,
`keywords`) is intentionally stripped to keep the tree-navigation
contract narrow. The row shape these consumers expect needs all three
per-topic fields, so this module reads `store.load` + `store.load_item_meta`
directly. Callers reaching for *agent* tree navigation use `drill_tree`;
this module is a Python-internal shape transform that needs more than
the tree view exposes.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def session_summary_row(session_id: str) -> dict[str, Any] | None:
    """Return the full `session_summaries` row dict (with embedded
    `topics`) for one session, or `None` if the session has no summary.

    Shape:

        {
          "session_id": str,
          "tldr": str,
          "topic_count": int,
          "generated_at": str | None,
          "model": str | None,
          "profile": str | None,
          "backend": str | None,
          "prompt_version": int | None,
          "summary_schema_version": int | None,
          "selection_version": int | None,
          "cache_version": int | None,
          "status": str | None,
          "error": str | None,
          "topics": [
            {
              "id": f"{session_id}:{i}",
              "session_id": str,
              "topic_index": int,
              "title": str,
              "summary": str,
              "span_start": int | None,
              "span_end": int | None,
              "turn_start": int | None,
              "turn_end": int | None,
              "keywords": list[str],
            },
            ...
          ],
        }

    `turn_start` / `turn_end` are computed lazily by loading the
    `ConversationSession` and mapping spans to turns. Best-effort — if
    the session file is missing or the span map is empty, both fields are
    `None`.
    """
    from work_buddy.conversation_observability.summarizer_binding import (
        get_session_summarizer,
    )

    summarizer = get_session_summarizer()
    store = summarizer.store

    meta = store.load_item_meta(session_id)
    if meta is None:
        return None

    # Always load the tree even if status != 'ok' — `store.record_error`
    # preserves prior nodes when flipping status to 'error', and consumers
    # (dashboard, collector) expect the prior tldr to survive a transient
    # LLM failure.
    node = store.load(session_id)
    tldr = node.summary if node is not None else ""

    topics = _topics_from_node(session_id, node)

    return {
        "session_id": session_id,
        "tldr": tldr,
        "topic_count": meta.get("topic_count", 0),
        "generated_at": meta.get("generated_at"),
        "model": meta.get("model"),
        "profile": meta.get("profile"),
        "backend": meta.get("backend"),
        "prompt_version": meta.get("prompt_version"),
        "summary_schema_version": meta.get("summary_schema_version"),
        "selection_version": meta.get("selection_version"),
        "cache_version": meta.get("cache_version"),
        "status": meta.get("status"),
        "error": meta.get("error"),
        "topics": topics,
    }


def session_topics(session_id: str) -> list[dict[str, Any]]:
    """Return just the topic-list portion of the row.

    Same shape as the `topics` entry in `session_summary_row`'s return.
    Provided as a convenience for callers that only want the topic list.
    """
    from work_buddy.conversation_observability.summarizer_binding import (
        get_session_summarizer,
    )

    summarizer = get_session_summarizer()
    node = summarizer.store.load(session_id)
    if node is None:
        return []
    return _topics_from_node(session_id, node)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _topics_from_node(
    session_id: str,
    node: Any,
) -> list[dict[str, Any]]:
    """Build the per-topic dict list from the loaded SummaryNode tree.

    `node` is the root SummaryNode (from `store.load`) — None means no
    topics. The lazy `ConversationSession` load is done once and reused
    across all children for span→turn conversion.
    """
    if node is None:
        return []

    # Lazy session load for turn_range conversion. Best-effort: if the
    # session file is missing we still return topics without turn_range.
    session: Any | None = None
    try:
        from work_buddy.sessions.inspector import ConversationSession

        session = ConversationSession(session_id)
        session._ensure_loaded()
    except Exception:
        session = None

    span_to_turn = None
    if session is not None:
        try:
            from work_buddy.conversation_observability.sessions import (
                span_range_to_turn_range,
            )

            span_to_turn = span_range_to_turn_range
        except Exception:
            span_to_turn = None

    out: list[dict[str, Any]] = []
    for i, child in enumerate(node.children):
        extra = child.extra or {}
        span_start = extra.get("span_start")
        span_end = extra.get("span_end")
        turn_start: int | None = None
        turn_end: int | None = None
        if (
            session is not None
            and span_to_turn is not None
            and isinstance(span_start, int)
            and isinstance(span_end, int)
            and span_end > span_start
        ):
            try:
                turn_start, turn_end = span_to_turn(
                    session, span_start, span_end,
                )
            except Exception:
                pass

        out.append({
            "id": f"{session_id}:{i}",
            "session_id": session_id,
            "topic_index": i,
            "title": extra.get("title", ""),
            "summary": child.summary,
            "span_start": span_start,
            "span_end": span_end,
            "turn_start": turn_start,
            "turn_end": turn_end,
            "keywords": list(extra.get("keywords") or []),
        })
    return out
