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

    def total_turns(self, session_id: str) -> int | None:
        """Return the total number of turns in a session, or `None` if the
        session cannot be loaded (e.g. missing JSONL).

        Required by INCREMENTAL strategies (PRD §5.1) to compute the
        finalization boundary and detect "nothing fresh to summarize."
        """
        try:
            from work_buddy.sessions.inspector import ConversationSession

            session = ConversationSession(session_id)
            session._ensure_loaded()
        except FileNotFoundError:
            return None

        turns = getattr(session, "turns", None)
        if turns is None:
            return None
        return len(turns)

    def render_from(self, session_id: str, from_turn: int) -> str | None:
        """Render only the turns at index ``from_turn`` and after.

        Returns the formatted prompt text or `None` if the session cannot
        be loaded. The total-chars cap is DISABLED for v2's incremental
        path — the chunked pathway handles oversize input explicitly; the
        v1-style silent truncation here would otherwise mask session size
        from the pathway-selection logic. Per-turn 4k char truncation still
        applies (any single turn over 4k is excessive context).
        """
        try:
            from work_buddy.sessions.inspector import ConversationSession

            session = ConversationSession(session_id)
            session._ensure_loaded()
        except FileNotFoundError:
            return None

        if not getattr(session, "_span_map", None):
            return None

        return _build_session_prompt(
            session, from_turn=from_turn, max_total_chars=None,
        )

    def render_range(
        self, session_id: str, from_turn: int, to_turn: int,
    ) -> str | None:
        """Render turns in the half-open range [from_turn, to_turn).

        Used by the chunked pathway to slice a long fresh tail into
        per-call chunks. No char cap applied — the caller is bounding
        by turn count and is responsible for keeping chunks under budget.

        Returns `None` if the session cannot be loaded or `from_turn >= to_turn`.
        """
        if to_turn <= from_turn:
            return None
        try:
            from work_buddy.sessions.inspector import ConversationSession

            session = ConversationSession(session_id)
            session._ensure_loaded()
        except FileNotFoundError:
            return None

        if not getattr(session, "_span_map", None):
            return None

        return _build_session_prompt_range(session, from_turn, to_turn)

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


def _build_session_prompt_range(session: Any, from_turn: int, to_turn: int) -> str:
    """Render a precise turn range without the char cap.

    Used by chunked pathway. Per-turn 4k char truncation still applies
    (any single turn over 4k is excessive context anyway), but the
    total-chars cap that ``_build_session_prompt`` uses to truncate the
    tail is NOT applied here — the caller is bounding by turn count.

    Turn numbering uses absolute session indices so the LLM's emitted
    span_range values align with the session's real turn array.
    """
    end = min(to_turn, len(session.turns))
    chunks: list[str] = []
    for i in range(from_turn, end):
        turn = session.turns[i]
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
    return "\n\n".join(chunks)


def _build_session_prompt(
    session: Any,
    from_turn: int = 0,
    *,
    max_total_chars: int | None = 40_000,
) -> str:
    """Render a session's turns into compact prompt text.

    Strategy mirrors the previous in-tree `_build_user_prompt`: per-turn
    truncation, optional total cap.

    `from_turn` (v2 addition): start rendering at this absolute turn index.
    Default 0 (whole session — same as v1). Used by INCREMENTAL strategies
    via `SessionSource.render_from` to feed only the fresh tail to the LLM.
    The turn-index prefix in each line is the ABSOLUTE index, so span_range
    values emitted by the LLM line up with the session's real turn array
    regardless of whether we sliced or not.

    `max_total_chars` (v2 fix, 2026-05-28): None disables the total-chars
    cap. v1's `render` keeps the 40k default (legacy producer relies on it).
    v2's `render_from` passes None so the incremental algorithm's pathway-
    selection logic sees the TRUE input size — without this, a long session
    would silently truncate to ~40k chars and the model would emit topics
    over only the visible prefix (the v1 pathology). The chunked pathway
    handles oversized inputs explicitly.
    """
    chunks: list[str] = []
    used = 0
    turns_iter = session.turns[from_turn:] if from_turn > 0 else session.turns
    for offset, turn in enumerate(turns_iter):
        i = from_turn + offset  # absolute turn index
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
        if max_total_chars is not None and used >= max_total_chars:
            remaining = len(session.turns) - i - 1
            if remaining > 0:
                chunks.append(
                    f"[…{remaining} more turns truncated…]"
                )
            break
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Factory + lazy singleton
# ---------------------------------------------------------------------------


_summarizer_singleton: Summarizer | None = None


def build_session_summarizer(use_incremental: bool = False) -> Summarizer:
    """Build a fresh `Summarizer` for conversation sessions.

    Tests can call this directly to get an isolated instance (and pair with
    a monkey-patched DB path). Production callers go through
    `get_session_summarizer()` for the lazy singleton (always v1) or
    `build_session_summarizer(use_incremental=True)` for v2 directly.

    `use_incremental` (PRD §10 OQ19 + P7 wiring):
    - `True` → v2: `IncrementalLayeredStrategy` (prompt_v=2, schema_v=2)
      + `DurableSummaryStore(selection=2, cache=2)`. The triplet (2,2,2,2)
      marks all v1-shape rows stale on next refresh, triggering re-
      summarization via the queue worker.
    - `False` (default) → v1: `LayeredDisclosureStrategy` (prompt_v=1,
      schema_v=1) + `DurableSummaryStore(selection=1, cache=1)`. v1-shape
      legacy callers (tests, query helpers, the v1 cron) always get this.

    The feature flag `conversation_observability.summaries.use_incremental`
    is consulted by the WORKER directly (see `summarization/worker.py`) and
    by `refresh_observed_sessions` for the auto-enqueue gate — NOT by this
    function. This separation prevents the lazy singleton from flipping
    legacy callers to v2 strategy when the worker flag is enabled.
    """
    if use_incremental:
        from work_buddy.summarization.strategies import IncrementalLayeredStrategy

        return Summarizer(
            name="conversation_session",
            source=SessionSource(),
            strategy=IncrementalLayeredStrategy(),
            store=DurableSummaryStore(
                namespace=_NAMESPACE,
                selection_version=2,
                cache_version=2,
            ),
        )

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
