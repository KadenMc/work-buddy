"""Durable session-attributed GitHub PR-activity storage and refresh.

Mirrors ``commits.py``: scans recent Claude Code JSONL session files for
``gh pr create|merge|close|review`` Bash invocations and persists one
``session_prs`` row per (session, PR, action) event. This is the
structural counterpart to ``session_commits`` — it answers "which PRs
did this session author / merge / close / review" without re-parsing
JSONL on every read, and without the false positives of commit-message
``Closes #NNN`` regex.

Public surface:

* :func:`refresh_session_prs` — scan recent sessions, upsert PR rows.
  Stale-only via the ``prs_scanned_mtime`` column on ``observed_sessions``
  (owned by this refresher; never conflated with the commit/write
  staleness columns).
* :func:`query_session_prs` — read-only DB query, optionally scoped to
  one session or a recency window.

Detection lives in ``sessions.inspector._extract_prs_single_pass``; this
module owns persistence + the stale-only ledger, exactly as ``commits.py``
delegates extraction to ``_extract_commits_single_pass``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from work_buddy.conversation_observability.db import get_connection


# ---------------------------------------------------------------------------
# Refresh — scan JSONL files and persist PR-activity rows
# ---------------------------------------------------------------------------


def refresh_session_prs(
    session_id: str | None = None,
    days: int = 7,
    project: str | None = None,
    max_sessions: int | None = None,
) -> dict[str, Any]:
    """Scan recent sessions and persist their PR-activity events.

    Returns ``{"pr_count": N, "prs": [{...}, ...]}`` with each PR dict
    carrying: session_id, pr_number, pr_url, repo, action, ts,
    message_index. Sorted newest-first by ``ts`` to match the commit
    contract.

    Stale-only by default: a session file whose mtime matches the
    ``prs_scanned_mtime`` row in ``observed_sessions`` is skipped. Pass an
    explicit ``session_id`` to force a re-scan of one session.
    ``max_sessions`` caps the number of cache MISSES per call so a
    cold-start (or full backfill) scan can split across cron firings
    instead of running for minutes; cache hits are unbounded.
    """
    # Import here to mirror commits.py — keeps the package importable
    # without dragging in the whole inspector module for callers that
    # only want the read side.
    from work_buddy.sessions.inspector import (
        _extract_prs_single_pass,
        _recent_sessions,
        resolve_session_path,
    )

    if session_id:
        path, full_sid = resolve_session_path(session_id)
        paths = [(path, full_sid)]
        force_rescan = True
    else:
        paths = _recent_sessions(days, project=project)
        force_rescan = False

    now_iso = datetime.now(timezone.utc).isoformat()
    all_prs: list[dict[str, Any]] = []

    # Pre-pass: pull the scan ledger in one short read so the per-session
    # loop doesn't hold a connection open across JSONL parsing.
    conn = get_connection()
    try:
        ledger_rows = conn.execute(
            "SELECT session_id, prs_scanned_mtime FROM observed_sessions"
        ).fetchall()
    finally:
        conn.close()
    scanned_at = {
        r["session_id"]: r["prs_scanned_mtime"] for r in ledger_rows
    }

    misses = 0
    for path, sid in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        cached_mtime = scanned_at.get(sid)
        if (
            cached_mtime is not None
            and not force_rescan
            and abs(cached_mtime - mtime) < 1e-6
        ):
            conn = get_connection()
            try:
                cached = conn.execute(
                    "SELECT * FROM session_prs WHERE session_id = ?",
                    (sid,),
                ).fetchall()
            finally:
                conn.close()
            all_prs.extend(_row_to_pr(r) for r in cached)
            continue

        # Cache MISS: bounded by ``max_sessions``.
        if max_sessions is not None and misses >= max_sessions:
            continue
        misses += 1

        # Fast binary grep — skip files with no ``gh pr`` invocation at
        # all before paying for a full JSON parse.
        try:
            with open(path, "rb") as fb:
                if b"gh pr" not in fb.read():
                    prs = []
                else:
                    prs = _extract_prs_single_pass(path, sid)
        except OSError:
            continue

        conn = get_connection()
        try:
            conn.execute(
                "DELETE FROM session_prs WHERE session_id = ?", (sid,)
            )
            for p in prs:
                conn.execute(
                    "INSERT OR IGNORE INTO session_prs "
                    "(session_id, pr_number, pr_url, repo, action, ts, "
                    " message_index, source_path, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p["session_id"],
                        p["pr_number"],
                        p["pr_url"],
                        p["repo"],
                        p["action"],
                        p.get("ts"),
                        p.get("message_index"),
                        str(path),
                        now_iso,
                    ),
                )
            conn.execute(
                "INSERT INTO observed_sessions "
                "(session_id, source_path, source_mtime, observed_at, "
                " prs_scanned_mtime) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                " source_path=excluded.source_path, "
                " prs_scanned_mtime=excluded.prs_scanned_mtime",
                (sid, str(path), mtime, now_iso, mtime),
            )
            conn.commit()
        finally:
            conn.close()
        all_prs.extend(prs)

    all_prs.sort(key=lambda p: p.get("ts", "") or "", reverse=True)
    return {"pr_count": len(all_prs), "prs": all_prs}


# ---------------------------------------------------------------------------
# Read-only queries
# ---------------------------------------------------------------------------


def query_session_prs(
    session_id: str | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Read PR-activity rows from the DB without scanning JSONL.

    Use when the cache is known fresh (e.g. immediately after a refresh,
    or from the dashboard which refreshes on its own cadence).
    """
    conn = get_connection()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if days is not None:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
            clauses.append("ts >= ?")
            params.append(cutoff_dt.isoformat())

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM session_prs{where} ORDER BY ts DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_pr(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_pr(row: Any) -> dict[str, Any]:
    """Project a ``session_prs`` row into the public PR dict."""
    return {
        "session_id": row["session_id"],
        "pr_number": row["pr_number"],
        "pr_url": row["pr_url"],
        "repo": row["repo"],
        "action": row["action"],
        "ts": row["ts"],
        "message_index": row["message_index"],
    }
