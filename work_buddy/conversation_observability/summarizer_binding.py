"""conv_obs as the first composition of the summarization framework.

`build_session_summarizer()` returns a `Summarizer` wired with
`SessionSource × LayeredDisclosureStrategy × DurableSummaryStore`. The
backwards-compatible `summaries.py` shims use it to preserve the existing
read/write API (dashboard `/api/chats/<id>/topics`, the
`conversation_observability_summarize` MCP capability, the sidecar job, the
`claude_session_summary` context collector).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from work_buddy.summarization import (
    DiscoveryWindow,
    Summarizer,
    SummaryCapability,
)
from work_buddy.summarization.strategies import LayeredDisclosureStrategy
from work_buddy.summarization.stores import DurableSummaryStore

logger = logging.getLogger(__name__)

_NAMESPACE = "conversation_session"


# ---------------------------------------------------------------------------
# SessionSource
# ---------------------------------------------------------------------------


class SessionSource:
    """`Source` adapter for Claude Code conversation sessions.

    `discover` queries `observed_sessions` for candidates in the window.
    `render` loads a `ConversationSession`, returns `None` if the session has
    no spans, else assembles the prompt text from turns.

    The freshness token is the `source_mtime` (stringified) — bumped whenever
    the underlying JSONL file is rewritten. `token_for(session_id)` exposes
    the current token so the shim's `summarize_session` can drive the
    short-circuit freshness check.
    """

    name = "conversation_session"
    capabilities = frozenset()  # not BATCHED

    def discover(
        self, window: DiscoveryWindow,
    ) -> list[tuple[str, Any]]:
        from datetime import timedelta

        from work_buddy.conversation_observability.db import get_connection

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=window.days)
        ).isoformat()

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT session_id, source_mtime FROM observed_sessions "
                "WHERE observed_at >= ? AND status = 'ok' "
                "  AND message_count IS NOT NULL AND message_count > 0 "
                "ORDER BY observed_at DESC",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()

        return [(r["session_id"], str(r["source_mtime"])) for r in rows]

    def render(self, session_id: str) -> str | None:
        try:
            from work_buddy.sessions.inspector import ConversationSession

            session = ConversationSession(session_id)
            session._ensure_loaded()
        except FileNotFoundError:
            return None

        if not getattr(session, "_span_map", None):
            return None

        return _build_session_prompt(session)

    def render_batch(self, item_ids: list[str]) -> list[str | None]:
        # Not BATCHED — not used by the orchestrator. Provided for the
        # Protocol surface only.
        return [self.render(iid) for iid in item_ids]

    def token_for(self, session_id: str) -> str | None:
        """Return the current freshness token for one session, or `None` if
        the session isn't observed. Used by the `summarize_session` shim to
        short-circuit on freshness."""
        from work_buddy.conversation_observability.db import get_connection

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT source_mtime FROM observed_sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        return str(row["source_mtime"])


def _build_session_prompt(session: Any) -> str:
    """Render a session's turns into compact prompt text.

    Strategy mirrors the previous in-tree `_build_user_prompt`: per-turn
    truncation, total cap. Kept identical so layered-disclosure prompts have
    the same input distribution.
    """
    max_total_chars = 40_000
    chunks: list[str] = []
    used = 0
    for i, turn in enumerate(session.turns):
        role = turn.get("role", "?")
        text = (turn.get("text", "") or "")[:4000]
        tools = turn.get("tools", []) or []
        line = f"[turn {i} | {role}]"
        if tools:
            tool_names = ", ".join(
                t if isinstance(t, str) else t.get("name", "?")
                for t in tools
            )
            line += f" tools=[{tool_names}]"
        if text:
            line += f"\n{text}"
        chunks.append(line)
        used += len(line)
        if used >= max_total_chars:
            chunks.append(
                f"[…{len(session.turns) - i - 1} more turns truncated…]"
            )
            break
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Factory + lazy singleton
# ---------------------------------------------------------------------------


_summarizer_singleton: Summarizer | None = None


def build_session_summarizer() -> Summarizer:
    """Build a fresh `Summarizer` for conversation sessions.

    Tests can call this directly to get an isolated instance (and pair with
    a monkey-patched DB path). Production callers go through
    `get_session_summarizer()` for the lazy singleton.
    """
    return Summarizer(
        name="conversation_session",
        source=SessionSource(),
        strategy=LayeredDisclosureStrategy(),
        store=DurableSummaryStore(
            namespace=_NAMESPACE,
            selection_version=1,
            cache_version=1,
        ),
    )


def get_session_summarizer() -> Summarizer:
    """Lazy singleton accessor."""
    global _summarizer_singleton
    if _summarizer_singleton is None:
        _summarizer_singleton = build_session_summarizer()
    return _summarizer_singleton


def reset_session_summarizer() -> None:
    """Drop the lazy singleton. Used by tests that monkey-patch DB paths
    after the first construction."""
    global _summarizer_singleton
    _summarizer_singleton = None
