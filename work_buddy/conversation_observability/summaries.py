"""Bounded LLM topic summaries for observed Claude Code sessions.

Generates per-session ``tldr`` + ``topic_summary`` rows and persists
them to ``session_summaries`` / ``topic_summaries``. Used by:

* ``/wb-session-identify`` — as candidate sanity checks before drilling
  into raw IR hits.
* ``claude_session_summary`` context source — when enabled, the tldr
  replaces the descriptors-only line.

Design constraints:

* **Bounded by default.** ``refresh_session_summaries(max_sessions=3)``
  caps per-call work; the sidecar cron uses a small N because each
  call hits the LLM.
* **Stale detection is multi-axis.** A summary is stale if the source
  file's mtime changed OR the prompt / schema / selection / cache
  versions bumped. Every version is recorded on the row.
* **Failures don't corrupt prior good summaries.** If the LLM errors
  or returns invalid JSON, the row's ``status`` flips to ``'error'``
  with the exception detail, but the previous good tldr stays present
  unless the caller explicitly forces an overwrite.
* **LLM caller is injectable** for testability — pass ``llm_call=`` to
  ``summarize_session`` and ``refresh_session_summaries``. Default is
  ``work_buddy.llm.runner_v2.llm_call`` with the FRONTIER_FAST tier.

Disabled by default. Set ``conversation_observability.summaries.enabled``
to True in ``config.local.yaml`` to opt in.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from work_buddy.conversation_observability.db import get_connection
from work_buddy.conversation_observability.sessions import (
    refresh_observed_sessions,
)

logger = logging.getLogger(__name__)


# Version stamps. Bumping any of these invalidates cached summaries for
# **every** session — handle with care. See module docstring.
PROMPT_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
SELECTION_VERSION = 1
CACHE_VERSION = 1


SYSTEM_PROMPT = """\
You are an analyst producing compact, factual recaps of Claude Code
agent-user conversations. Each conversation is a sequence of turns
(user + assistant) interleaved with tool calls (Bash, Edit, Write, etc.)
and tool outputs.

Produce two things:
1. tldr: ONE sentence (≤25 words) capturing what was accomplished or
   attempted. No greetings, no commentary on tone. Concrete enough that
   the user can recognize the session a week from now.
2. topic_summary: an ordered list of distinct topics within the session.
   Each topic has a short title (≤8 words), a one-sentence summary, a
   span_range covering the spans it spans, and 2-5 keywords. Cap at 8
   topics; merge fine-grained sub-topics rather than emitting many
   nearly-identical entries.

Be operational. Prefer concrete nouns ("AFK build of conversation
observability subsystem") over abstract ones ("worked on a feature").
"""


SUMMARY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tldr", "topic_summary"],
    "properties": {
        "tldr": {"type": "string"},
        "topic_summary": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["title", "summary", "span_range", "keywords"],
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "span_range": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {"type": "integer"},
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 5,
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Refresh — find stale candidates and summarize a bounded subset
# ---------------------------------------------------------------------------


def refresh_session_summaries(
    days: int = 7,
    max_sessions: int = 3,
    force: bool = False,
    llm_call: Callable[..., Any] | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Summarize up to ``max_sessions`` stale sessions.

    Stale criteria:
      * No ``session_summaries`` row exists, OR
      * Any version stamp differs from the module constants, OR
      * The session's source file mtime is newer than the summary's
        ``generated_at``, OR
      * ``force=True`` (re-summarize regardless of staleness).

    Returns a summary dict for observability:
    ``{summarized, skipped_fresh, errored, total_candidates}``.
    """
    # Make sure observed_sessions is fresh enough to drive candidate
    # selection. The summarizer only operates on sessions we know about.
    refresh_observed_sessions(days=days, stale_only=True)

    candidates = _select_candidates(days=days)
    summarized = 0
    skipped_fresh = 0
    errored = 0

    for session_id in candidates:
        if summarized >= max_sessions:
            break

        if not force and _is_summary_fresh(session_id):
            skipped_fresh += 1
            continue

        try:
            summarize_session(
                session_id=session_id,
                force=force,
                llm_call=llm_call,
                profile=profile,
            )
            summarized += 1
        except Exception as exc:  # pragma: no cover — defensive
            errored += 1
            logger.warning(
                "conversation_observability: summarize_session(%s) failed: %s",
                session_id, exc,
            )
            _record_summary_error(session_id, exc)

    return {
        "summarized": summarized,
        "skipped_fresh": skipped_fresh,
        "errored": errored,
        "total_candidates": len(candidates),
    }


# ---------------------------------------------------------------------------
# Per-session summarize
# ---------------------------------------------------------------------------


def summarize_session(
    session_id: str,
    *,
    force: bool = False,
    llm_call: Callable[..., Any] | None = None,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Summarize one session and persist its rows.

    Returns the new ``session_summaries`` row (as a dict), or ``None``
    when the session has no usable turns (empty session — nothing to
    summarize). Pass ``llm_call`` to inject a stub during tests; the
    real default lazily imports the LLMRunner the first call.
    """
    from work_buddy.sessions.inspector import ConversationSession

    if llm_call is None:
        llm_call = _default_llm_call

    if not force and _is_summary_fresh(session_id):
        return _load_summary_row(session_id)

    try:
        session = ConversationSession(session_id)
        session._ensure_loaded()
    except FileNotFoundError:
        _record_summary_error(session_id, "session file missing")
        return None

    span_count = len(session._span_map or {})
    if span_count == 0:
        return None

    prompt = _build_user_prompt(session)
    response = llm_call(
        system=SYSTEM_PROMPT,
        user=prompt,
        output_schema=SUMMARY_OUTPUT_SCHEMA,
        profile=profile,
    )

    parsed = _coerce_response(response)
    if not isinstance(parsed, dict) or "tldr" not in parsed:
        _record_summary_error(
            session_id,
            f"invalid LLM response: {str(response)[:200]}",
        )
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    topics = parsed.get("topic_summary") or []
    model = _extract_model(response)
    backend = _extract_backend(response)

    conn = get_connection()
    try:
        # Replace any previous rows for this session — both the summary
        # itself and its topic children.
        conn.execute(
            "DELETE FROM topic_summaries WHERE session_id = ?", (session_id,),
        )
        for i, topic in enumerate(topics):
            span_range = topic.get("span_range") or [None, None]
            span_start = span_range[0] if len(span_range) >= 1 else None
            span_end = span_range[1] if len(span_range) >= 2 else None
            turn_start = turn_end = None
            try:
                if (
                    isinstance(span_start, int)
                    and isinstance(span_end, int)
                    and span_end > span_start
                ):
                    from work_buddy.conversation_observability.sessions import (
                        span_range_to_turn_range,
                    )

                    turn_start, turn_end = span_range_to_turn_range(
                        session, span_start, span_end,
                    )
            except Exception:
                pass

            conn.execute(
                "INSERT INTO topic_summaries "
                "(id, session_id, topic_index, title, summary, "
                " span_start, span_end, turn_start, turn_end, "
                " keywords_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{session_id}:{i}",
                    session_id,
                    i,
                    str(topic.get("title", "")),
                    str(topic.get("summary", "")),
                    span_start,
                    span_end,
                    turn_start,
                    turn_end,
                    json.dumps(topic.get("keywords", []), ensure_ascii=False),
                ),
            )

        conn.execute(
            "INSERT INTO session_summaries "
            "(session_id, tldr, topic_count, generated_at, model, "
            " profile, backend, prompt_version, summary_schema_version, "
            " selection_version, cache_version, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', NULL) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  tldr=excluded.tldr, "
            "  topic_count=excluded.topic_count, "
            "  generated_at=excluded.generated_at, "
            "  model=excluded.model, "
            "  profile=excluded.profile, "
            "  backend=excluded.backend, "
            "  prompt_version=excluded.prompt_version, "
            "  summary_schema_version=excluded.summary_schema_version, "
            "  selection_version=excluded.selection_version, "
            "  cache_version=excluded.cache_version, "
            "  status='ok', error=NULL",
            (
                session_id,
                str(parsed["tldr"]),
                len(topics),
                now_iso,
                model,
                profile,
                backend,
                PROMPT_VERSION,
                SUMMARY_SCHEMA_VERSION,
                SELECTION_VERSION,
                CACHE_VERSION,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return _load_summary_row(session_id)


# ---------------------------------------------------------------------------
# Read-only queries
# ---------------------------------------------------------------------------


def query_session_summary(session_id: str) -> dict[str, Any] | None:
    """Look up a summary by session_id, including its topic list."""
    return _load_summary_row(session_id)


def query_topic_summaries(session_id: str) -> list[dict[str, Any]]:
    """All topic rows for one session, in order."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM topic_summaries WHERE session_id = ? "
            "ORDER BY topic_index",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        rec = dict(r)
        try:
            rec["keywords"] = json.loads(rec.pop("keywords_json", "[]"))
        except (ValueError, TypeError):
            rec["keywords"] = []
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _select_candidates(days: int) -> list[str]:
    """Return session_ids worth summarizing, newest-first.

    Picks observed sessions in the recency window that either have no
    summary, an older summary version, or whose source file is newer
    than the cached summary.
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT obs.session_id, obs.source_mtime, obs.message_count, "
            "       sum.generated_at, sum.prompt_version, "
            "       sum.summary_schema_version, sum.selection_version, "
            "       sum.cache_version "
            "FROM observed_sessions obs "
            "LEFT JOIN session_summaries sum ON sum.session_id = obs.session_id "
            "WHERE obs.observed_at >= ? AND obs.status = 'ok' "
            "  AND obs.message_count IS NOT NULL "
            "  AND obs.message_count > 0 "
            "ORDER BY obs.observed_at DESC",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    candidates: list[str] = []
    for r in rows:
        sid = r["session_id"]
        if r["generated_at"] is None:
            candidates.append(sid)
            continue
        if (
            r["prompt_version"] != PROMPT_VERSION
            or r["summary_schema_version"] != SUMMARY_SCHEMA_VERSION
            or r["selection_version"] != SELECTION_VERSION
            or r["cache_version"] != CACHE_VERSION
        ):
            candidates.append(sid)
            continue
        # mtime-based staleness: if the file changed after the summary
        # was generated, re-summarize.
        try:
            generated_dt = datetime.fromisoformat(
                r["generated_at"].replace("Z", "+00:00")
            )
            if r["source_mtime"] > generated_dt.timestamp() + 1:
                candidates.append(sid)
        except (ValueError, TypeError):
            candidates.append(sid)
    return candidates


def _is_summary_fresh(session_id: str) -> bool:
    """True when a non-stale summary already exists for this session."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT s.generated_at, s.prompt_version, "
            "       s.summary_schema_version, s.selection_version, "
            "       s.cache_version, s.status, "
            "       o.source_mtime "
            "FROM session_summaries s "
            "JOIN observed_sessions o ON o.session_id = s.session_id "
            "WHERE s.session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None or row["status"] != "ok":
        return False
    if (
        row["prompt_version"] != PROMPT_VERSION
        or row["summary_schema_version"] != SUMMARY_SCHEMA_VERSION
        or row["selection_version"] != SELECTION_VERSION
        or row["cache_version"] != CACHE_VERSION
    ):
        return False
    try:
        generated_dt = datetime.fromisoformat(
            row["generated_at"].replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return False
    return row["source_mtime"] <= generated_dt.timestamp() + 1


def _load_summary_row(session_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM session_summaries WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    rec = dict(row)
    rec["topics"] = query_topic_summaries(session_id)
    return rec


def _record_summary_error(session_id: str, exc: Any) -> None:
    """Stamp an error status without overwriting a prior good tldr."""
    now_iso = datetime.now(timezone.utc).isoformat()
    msg = str(exc)
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT tldr FROM session_summaries WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO session_summaries "
                "(session_id, tldr, generated_at, prompt_version, "
                " summary_schema_version, selection_version, "
                " cache_version, status, error) "
                "VALUES (?, '', ?, ?, ?, ?, ?, 'error', ?)",
                (
                    session_id,
                    now_iso,
                    PROMPT_VERSION,
                    SUMMARY_SCHEMA_VERSION,
                    SELECTION_VERSION,
                    CACHE_VERSION,
                    msg,
                ),
            )
        else:
            conn.execute(
                "UPDATE session_summaries SET status='error', error=? "
                "WHERE session_id = ?",
                (msg, session_id),
            )
        conn.commit()
    finally:
        conn.close()


def _build_user_prompt(session: Any) -> str:
    """Render a session's turns into a compact text prompt.

    Strategy: take up to the first 4000 chars per turn, capped at
    ~40 KB total to stay within reasonable input budgets. Tool calls
    are rendered as one-liners; tool outputs are truncated aggressively.
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
            chunks.append(f"[…{len(session.turns) - i - 1} more turns truncated…]")
            break
    return "\n\n".join(chunks)


def _default_llm_call(
    *,
    system: str,
    user: str,
    output_schema: dict[str, Any] | None = None,
    profile: str | None = None,
) -> Any:
    """The real-system default — lazy-loaded to keep the test path cheap."""
    from work_buddy.llm.runner_v2 import LLMRunner
    from work_buddy.llm.tiers import ModelTier

    return LLMRunner().call(
        tier=ModelTier.FRONTIER_FAST,
        system=system,
        user=user,
        output_schema=output_schema,
        max_tokens=1024,
        trace_id="conversation_observability.summary",
    )


def _coerce_response(response: Any) -> Any:
    """Normalize stub / real LLM responses to a parsed JSON dict.

    Tests pass back a plain dict (or a string). The real ``LLMResponse``
    carries ``parsed`` (dict | None) and ``content`` (raw string). We
    accept either path.
    """
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        try:
            return json.loads(response)
        except (ValueError, TypeError):
            return response
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    content = getattr(response, "content", None)
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return content
    return None


def _extract_model(response: Any) -> str | None:
    return getattr(response, "model", None)


def _extract_backend(response: Any) -> str | None:
    backend = getattr(response, "backend", None)
    if backend:
        return str(backend)
    return None
