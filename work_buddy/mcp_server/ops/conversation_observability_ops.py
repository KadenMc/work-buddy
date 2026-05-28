"""Conversation-observability ops — refresh + query the derived-activity DB.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).

Import-deadlock note: these callables run via ``asyncio.to_thread()`` in the
MCP server. Their targets use ``sqlite3`` (already loaded) and ``json`` — safe
by the time the first call lands. Keep imports lazy, inside the callables.
"""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op


def conversation_observability_refresh(
    days: int = 7,
    max_sessions: int | None = None,
    stale_only: bool = True,
) -> dict[str, Any]:
    """Single-call refresh of all three derived tables.

    ``max_sessions`` is forwarded to every refresher so a cold scan can split
    across multiple cron firings rather than running for many minutes inside a
    single call. Cache hits are unbounded; only the MISS path is capped.

    Also pre-warms the per-SHA git-show cache used by the dashboard's
    ``/api/chats`` enrichment — best-effort; failures don't block the refresh.
    """
    from work_buddy.conversation_observability.commits import (
        refresh_session_commits,
    )
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )
    from work_buddy.conversation_observability.writes import (
        refresh_session_writes,
    )

    observed = refresh_observed_sessions(
        days=days, stale_only=stale_only, max_sessions=max_sessions,
    )
    commits = refresh_session_commits(days=days, max_sessions=max_sessions)
    writes = refresh_session_writes(days=days, max_sessions=max_sessions)

    prewarm_count = 0
    try:
        from pathlib import Path

        from work_buddy.config import load_config
        from work_buddy.conversation_observability.db import get_connection
        from work_buddy.conversation_observability.writes import (
            _committed_files_for_sessions,
        )

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT DISTINCT session_id FROM session_commits"
            ).fetchall()
        finally:
            conn.close()
        sids = {r["session_id"] for r in rows}
        if sids:
            cfg = load_config()
            resolved = _committed_files_for_sessions(
                sids, Path(cfg["repos_root"]),
            )
            prewarm_count = len(resolved)
    except Exception:  # pragma: no cover — defensive
        pass

    return {
        "observed_sessions": observed,
        "session_commits": {"commit_count": commits.get("commit_count", 0)},
        "session_writes": writes,
        "sha_cache": {"prewarmed_sessions": prewarm_count},
    }


def summarization_worker_tick(
    bypass_cooldown: bool = False,
    bypass_budget: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run one tick of the summarization queue worker (PRD §6 O2).

    Piggybacks on the existing 5-minute observability cadence. Drains the
    `summarization_queue` table FIFO over the cooldown-passed subset,
    bounded by `daily_budget_usd`.

    `bypass_cooldown` and `bypass_budget` are for explicit user-triggered
    refresh (e.g. `/wb-summarize-now`); routine sidecar drainage uses the
    defaults.
    """
    from work_buddy.summarization.worker import run_worker_tick

    return run_worker_tick(
        bypass_cooldown=bypass_cooldown,
        bypass_budget=bypass_budget,
        limit=limit,
    )


def _register() -> None:
    from work_buddy.conversation_observability.session_summary_row import (
        session_summary_row,
    )
    from work_buddy.conversation_observability.sessions import (
        list_observed_sessions,
        query_observed_session,
    )
    from work_buddy.conversation_observability.summaries import (
        refresh_session_summaries,
    )
    from work_buddy.conversation_observability.writes import uncommitted_report

    register_op("op.wb.conversation_observability_refresh",
                conversation_observability_refresh)
    register_op("op.wb.conversation_observability_uncommitted", uncommitted_report)
    register_op("op.wb.conversation_observability_get", query_observed_session)
    register_op("op.wb.conversation_observability_list", list_observed_sessions)
    register_op("op.wb.conversation_observability_summarize",
                refresh_session_summaries)
    register_op("op.wb.summarization_worker_tick",
                summarization_worker_tick, replace=True)
    # Both op IDs bind the same callable so existing
    # `conversation_observability_summary_get` callers keep working;
    # the capability declaration for the long-namespace name is marked
    # as an alias. ``replace=True`` is set so a registry reload (the
    # `importlib.reload` path in `load_builtin_ops`) re-binds cleanly
    # rather than crashing on the already-registered names — important
    # under pytest collection when test ordering triggers the reload path.
    register_op("op.wb.session_summary_get",
                session_summary_row, replace=True)
    register_op("op.wb.conversation_observability_summary_get",
                session_summary_row, replace=True)


_register()
