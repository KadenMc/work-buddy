"""Observed-session refresh + span/turn helpers.

Enriches the ``observed_sessions`` table with metadata captured from
``ConversationSession`` (message count, span count, duration, tool
usage summary) so the journal, context bundle, and dashboard can scan
the DB instead of re-instantiating sessions per call. The
``source_path``/``source_mtime`` columns drive stale-only refresh —
unchanged JSONL files don't get re-loaded.

Span/turn helpers are thin wrappers over
``ConversationSession.span_to_turns`` so we don't duplicate the
chunking-replay logic — that algorithm has subtle invariants (the
"empty span" filter that mirrors the IR source) and must remain in one
place.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.conversation_observability.db import get_connection


# ---------------------------------------------------------------------------
# Refresh — enrich observed_sessions with ConversationSession metadata
# ---------------------------------------------------------------------------


def refresh_observed_sessions(
    days: int = 30,
    stale_only: bool = True,
    max_sessions: int | None = None,
) -> dict[str, Any]:
    """Walk recent JSONL session files and refresh ``observed_sessions``.

    Stale-only by default: a row whose ``source_mtime`` matches the
    on-disk mtime is skipped. Pass ``stale_only=False`` to force every
    session to re-load (useful after a schema bump).

    ``max_sessions`` caps the per-call work so the refresh stays
    bounded inside time-sensitive contexts (e.g. an IR-index hook).

    Returns a summary: ``{observed, skipped, errored, total_scanned}``.
    """
    from work_buddy.sessions.inspector import (
        ConversationSession,
        _recent_sessions,
    )

    paths = _recent_sessions(days)
    now_iso = datetime.now(timezone.utc).isoformat()
    observed = 0
    skipped = 0
    errored = 0
    total = 0

    conn = get_connection()
    try:
        for path, sid in paths:
            total += 1
            if max_sessions is not None and observed >= max_sessions:
                break
            try:
                mtime = path.stat().st_mtime
            except OSError:
                errored += 1
                continue

            if stale_only:
                row = conn.execute(
                    "SELECT source_mtime, message_count FROM observed_sessions "
                    "WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                # message_count NULL = ledger row exists from commit
                # refresh but never received metadata. Treat as stale
                # so we backfill it.
                if (
                    row is not None
                    and abs(row["source_mtime"] - mtime) < 1e-6
                    and row["message_count"] is not None
                ):
                    skipped += 1
                    continue

            try:
                session = ConversationSession(sid)
                session._ensure_loaded()
                meta = session.metadata()
                tool_counts: dict[str, int] = {}
                for t in session.turns:
                    for tool in t.get("tools", []) or []:
                        name = tool if isinstance(tool, str) else tool.get("name", "")
                        if name:
                            tool_counts[name] = tool_counts.get(name, 0) + 1

                conn.execute(
                    "INSERT INTO observed_sessions "
                    "(session_id, project_name, project_slug, source_path, "
                    " source_mtime, observed_at, start_time, end_time, "
                    " message_count, span_count, tool_names_json, status, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', NULL) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "  project_name=excluded.project_name, "
                    "  project_slug=excluded.project_slug, "
                    "  source_path=excluded.source_path, "
                    "  source_mtime=excluded.source_mtime, "
                    "  observed_at=excluded.observed_at, "
                    "  start_time=excluded.start_time, "
                    "  end_time=excluded.end_time, "
                    "  message_count=excluded.message_count, "
                    "  span_count=excluded.span_count, "
                    "  tool_names_json=excluded.tool_names_json, "
                    "  status='ok', error=NULL",
                    (
                        sid,
                        path.parent.name,  # raw slug; resolved below if helper present
                        path.parent.name,
                        str(path),
                        mtime,
                        now_iso,
                        meta.get("start_time"),
                        meta.get("end_time"),
                        meta.get("message_count"),
                        len(session._span_map or {}),
                        json.dumps(tool_counts, ensure_ascii=False),
                    ),
                )
                observed += 1
            except Exception as exc:  # pragma: no cover — defensive
                errored += 1
                conn.execute(
                    "INSERT INTO observed_sessions "
                    "(session_id, source_path, source_mtime, observed_at, "
                    " status, error) "
                    "VALUES (?, ?, ?, ?, 'error', ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "  status='error', error=excluded.error, "
                    "  observed_at=excluded.observed_at",
                    (
                        sid,
                        str(path),
                        mtime,
                        now_iso,
                        f"{type(exc).__name__}: {exc}",
                    ),
                )

        conn.commit()
    finally:
        conn.close()

    return {
        "observed": observed,
        "skipped": skipped,
        "errored": errored,
        "total_scanned": total,
    }


# ---------------------------------------------------------------------------
# Span/turn helpers
# ---------------------------------------------------------------------------


def span_range_to_turn_range(
    session: Any,
    span_start: int,
    span_end: int,
) -> tuple[int, int]:
    """Map ``[span_start, span_end)`` to ``[turn_start, turn_end)``.

    Bounds: the returned turn range covers from the first turn of the
    starting span to the last turn of the ending span (inclusive on the
    span side, half-open on the turn side, matching ``range``).

    Single-source-of-truth: relies on
    ``ConversationSession.span_to_turns`` so the chunking-replay logic
    stays in one place. Passing a ``span_end`` past the session's last
    span clamps to the last span — useful when callers don't know how
    many spans a session has.
    """
    if span_start < 0 or span_end <= span_start:
        raise ValueError(
            f"invalid span range [{span_start}, {span_end})"
        )

    span_map = session._span_map or {}
    if not span_map:
        return (0, 0)

    max_span = max(span_map)
    last_span = min(span_end - 1, max_span)
    if span_start > max_span:
        return (0, 0)

    turn_start, _ = session.span_to_turns(span_start)
    _, turn_end = session.span_to_turns(last_span)
    return (turn_start, turn_end)


def query_observed_session(session_id: str) -> dict[str, Any] | None:
    """Look up a single observed-session row by session_id."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM observed_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    record = dict(row)
    try:
        record["tool_names"] = json.loads(record.pop("tool_names_json", "{}"))
    except (ValueError, TypeError):
        record["tool_names"] = {}
    return record


def list_observed_sessions(
    days: int | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """List observed sessions, optionally filtered by recency and project."""
    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("project_name = ?")
        params.append(project)
    if days is not None:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        clauses.append("observed_at >= ?")
        params.append(cutoff)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM observed_sessions{where} "
            f"ORDER BY end_time DESC, observed_at DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        rec = dict(r)
        try:
            rec["tool_names"] = json.loads(rec.pop("tool_names_json", "{}"))
        except (ValueError, TypeError):
            rec["tool_names"] = {}
        out.append(rec)
    return out
