"""Backwards-compatible shims over the summarization framework.

The producer of conversation-session summaries has been migrated to
`work_buddy.summarization` + `work_buddy.conversation_observability.summarizer_binding`.
This module preserves the legacy callable surface — `refresh_session_summaries`,
`summarize_session`, `query_session_summary`, `query_topic_summaries` — so
existing consumers see no change:

- the dashboard `/api/chats/<id>/topics` endpoint (`dashboard/service.py`)
- the `conversation_observability_summarize` MCP capability (op binding in
  `mcp_server/ops/conversation_observability_ops.py`)
- the `conversation_observability_summary_get` MCP capability
- the `claude_session_summary` context collector
- the sidecar job `conversation-observability-summarize.md`
- the legacy test stubs that pass `llm_call=...` as a bare callable

The legacy `session_summaries` and `topic_summaries` SQLite tables remain
defined in `schema.py` (zero-effort rollback) but are no longer written. Data
now lives in the framework's `summarization.db` under namespace
`conversation_session`. A follow-up change removes the unused table
definitions after a shakeout period.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from work_buddy.conversation_observability.summarizer_binding import (
    get_session_summarizer,
)
from work_buddy.summarization import as_caller
from work_buddy.summarization.strategies import LayeredDisclosureStrategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compat re-exports — historical module-level constants
# ---------------------------------------------------------------------------
#
# Tests and (one) other test file import these. They're now owned by
# `LayeredDisclosureStrategy`; the re-exports preserve the import path
# without ownership ambiguity. To change the prompt, edit
# `work_buddy/summarization/strategies.py`.

SYSTEM_PROMPT: str = LayeredDisclosureStrategy.system_prompt
SUMMARY_OUTPUT_SCHEMA: dict[str, Any] = LayeredDisclosureStrategy.output_schema


# ---------------------------------------------------------------------------
# Write-path shims
# ---------------------------------------------------------------------------


def refresh_session_summaries(
    days: int = 7,
    max_sessions: int = 3,
    force: bool = False,
    llm_call: Callable[..., Any] | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Refresh a bounded batch of stale session summaries.

    Legacy entry preserved for the `conversation_observability_summarize` op,
    the sidecar job, and tests. Returns the legacy result-dict shape
    `{summarized, skipped_fresh, errored, total_candidates}`.

    Refreshes `observed_sessions` first (legacy behavior — the source of
    candidate ids), then delegates to `Summarizer.refresh`.
    """
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )

    refresh_observed_sessions(days=days, stale_only=True)

    summarizer = get_session_summarizer()
    report = summarizer.refresh(
        days=days,
        max_items=max_sessions,
        force=force,
        llm_caller=as_caller(llm_call),
        profile=profile,
    )
    return report.to_op_dict()


def summarize_session(
    session_id: str,
    *,
    force: bool = False,
    llm_call: Callable[..., Any] | None = None,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Summarize one session (legacy single-item entry, used by tests).

    Returns the legacy `session_summaries` row dict (via
    `query_session_summary`), or `None` if the session isn't observed / has
    no usable turns.
    """
    summarizer = get_session_summarizer()
    token = summarizer.source.token_for(session_id)
    if token is None:
        return None

    node = summarizer.refresh_one(
        session_id,
        force=force,
        freshness_token=token,
        llm_caller=as_caller(llm_call),
        profile=profile,
    )
    # Legacy behavior: return `None` when the call produced no usable
    # result (empty session OR an LLM/parse error). Consumers can still
    # `query_session_summary` separately to inspect any recorded error row.
    if node is None:
        return None
    return query_session_summary(session_id)


# ---------------------------------------------------------------------------
# Read-path shims
# ---------------------------------------------------------------------------


def query_session_summary(session_id: str) -> dict[str, Any] | None:
    """Return the legacy `session_summaries` row dict (with `topics`).

    Consumed by:
    - the dashboard `/api/chats/<id>/topics` endpoint
    - the `conversation_observability_summary_get` MCP capability
    - the `claude_session_summary` context collector
    """
    summarizer = get_session_summarizer()
    store = summarizer.store

    meta = store.load_item_meta(session_id)
    if meta is None:
        return None

    # Always load the tree, regardless of status — `record_error` preserves
    # prior nodes when flipping status to 'error', and consumers (dashboard,
    # `conversation_observability_summary_get`) expect the prior tldr to
    # survive a transient LLM failure.
    node = store.load(session_id)
    tldr = node.summary if node is not None else ""

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
        "topics": query_topic_summaries(session_id),
    }


def query_topic_summaries(session_id: str) -> list[dict[str, Any]]:
    """Return the legacy `topic_summaries` row dicts (one per child node).

    `turn_start` / `turn_end` are computed lazily from `span_start` /
    `span_end` via `span_range_to_turn_range`. The previous implementation
    pre-computed and persisted these at save time; the framework's
    `SummaryStrategy.parse` doesn't have session context, so computing at
    read time is cleaner. Cost: one `ConversationSession` load per
    dashboard topics-tab view.
    """
    summarizer = get_session_summarizer()
    node = summarizer.store.load(session_id)
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
