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
- the `agent_session_summary` context collector
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

    When the on-by-default v2 policy is active, this legacy entry no-ops; the
    queue worker handles refresh on its own cadence.
    """
    from work_buddy.conversation_observability.sessions import (
        _v2_summarization_enabled,
        refresh_observed_sessions,
    )

    # When v2 is on AND the caller didn't pass an explicit stub, the
    # queue worker (sidecar job `summarization-worker`) handles refresh.
    # Don't double-summarize. Tests passing `llm_call=stub_llm` are
    # explicitly invoking the v1 path with a controlled response and
    # should not be short-circuited regardless of activation policy.
    if llm_call is None and _v2_summarization_enabled():
        # Still refresh observed_sessions so the queue gets new entries.
        refresh_observed_sessions(days=days, stale_only=True)
        return {
            "summarized": 0, "skipped_fresh": 0,
            "errored": 0, "total_candidates": 0,
            "v2_mode": True,
        }

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
    """Back-compat shim — delegates to the canonical row helper.

    Prefer
    :func:`work_buddy.conversation_observability.session_summary_row.session_summary_row`
    in new code. This name remains for the sidecar job and any out-of-tree
    caller that imports it directly; both return the same dict (including the
    per-topic turn ranges and timestamps the canonical helper derives).

    Consumed by: the dashboard `/api/chats/<id>/topics` endpoint, the
    `agent_session_summary` context collector, and the `session_summary_get` /
    `conversation_observability_summary_get` MCP capabilities — all via the
    canonical helper.
    """
    from work_buddy.conversation_observability.session_summary_row import (
        session_summary_row,
    )

    return session_summary_row(session_id)


def query_topic_summaries(session_id: str) -> list[dict[str, Any]]:
    """Back-compat shim — delegates to the canonical topic helper.

    Prefer
    :func:`work_buddy.conversation_observability.session_summary_row.session_topics`
    in new code. Same return shape, including the per-topic turn ranges and
    wall-clock timestamps the canonical helper derives.
    """
    from work_buddy.conversation_observability.session_summary_row import (
        session_topics,
    )

    return session_topics(session_id)
