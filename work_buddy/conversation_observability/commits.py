"""Durable session-attributed git commit storage and refresh.

Replaces ``sessions/inspector.py``'s process-local ``_commit_cache``
with a SQLite-backed cache that survives restarts and can be read by
context bundles, the journal update, and the dashboard without
re-parsing every JSONL session file.

Public surface:

* :func:`refresh_session_commits` — scan recent JSONL session files and
  upsert their commit rows. Skips files whose mtime hasn't changed since
  the last scan (the ``observed_sessions`` row is the per-file ledger).
* :func:`query_session_commits` — read-only DB query, optionally
  scoped to a single session or a recency window.
* :func:`get_commit_session_map` — ``{short_sha: full_session_id}``
  mapping for GitSource annotation. Same shape the inspector returns.

The inspector's ``session_commits`` and ``build_session_map`` keep
their public shape by delegating to :func:`refresh_session_commits` and
:func:`get_commit_session_map` respectively. Existing callers
(GitSource session annotation, ``/wb-session-identify``, journal
collectors) need no changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.conversation_observability.db import get_connection


# ---------------------------------------------------------------------------
# Refresh — scan JSONL files and persist commit rows
# ---------------------------------------------------------------------------


def refresh_session_commits(
    session_id: str | None = None,
    days: int = 7,
    project: str | None = None,
) -> dict[str, Any]:
    """Scan recent sessions and persist their commits to the DB.

    Returns the same shape as the legacy ``inspector.session_commits``:

    .. code-block:: python

        {"commit_count": N, "commits": [{...}, ...]}

    Each commit dict carries: session_id, hash, branch, message,
    files_changed, timestamp, message_index. (Sorted newest-first by
    timestamp to match the legacy contract.)

    Stale-only by default: a session file whose mtime matches the row
    in ``observed_sessions`` is skipped. Pass an explicit ``session_id``
    to force a re-scan of one session.
    """
    # Import here so the conversation_observability package can be
    # imported (and its artifact registered) without pulling in the
    # entire sessions/inspector module — useful for the cleanup tick.
    from work_buddy.sessions.inspector import (
        _extract_commits_single_pass,
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
    all_commits: list[dict[str, Any]] = []

    # Pre-pass: pull the scan ledger with one short read connection so
    # the per-session loop doesn't have to keep one open while parsing
    # large JSONL files. ``commits_scanned_mtime`` is owned by this
    # refresher; ``source_mtime`` (owned by ``refresh_observed_sessions``)
    # must not be conflated.
    conn = get_connection()
    try:
        ledger_rows = conn.execute(
            "SELECT session_id, commits_scanned_mtime FROM observed_sessions"
        ).fetchall()
    finally:
        conn.close()
    scanned_at = {
        r["session_id"]: r["commits_scanned_mtime"] for r in ledger_rows
    }

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
            # Cache hit — read existing rows in one short transaction
            # and move on.
            conn = get_connection()
            try:
                cached = conn.execute(
                    "SELECT * FROM session_commits WHERE session_id = ?",
                    (sid,),
                ).fetchall()
            finally:
                conn.close()
            all_commits.extend(_row_to_commit(r) for r in cached)
            continue

        # Cache miss: parse OUTSIDE the connection, then write in a
        # short transaction. Per-session commit boundary keeps the
        # write lock from being held across the whole loop.
        commits = _extract_commits_single_pass(path, sid)

        conn = get_connection()
        try:
            conn.execute(
                "DELETE FROM session_commits WHERE session_id = ?", (sid,)
            )
            for c in commits:
                conn.execute(
                    "INSERT OR REPLACE INTO session_commits "
                    "(sha, short_sha, session_id, branch, message, "
                    " files_changed, committed_at, message_index, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        c.get("hash", ""),
                        (c.get("hash") or "")[:7],
                        c["session_id"],
                        c.get("branch"),
                        c.get("message"),
                        c.get("files_changed"),
                        c.get("timestamp"),
                        c.get("message_index"),
                        now_iso,
                    ),
                )
            conn.execute(
                "INSERT INTO observed_sessions "
                "(session_id, source_path, source_mtime, observed_at, "
                " commits_scanned_mtime) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                " source_path=excluded.source_path, "
                " commits_scanned_mtime=excluded.commits_scanned_mtime",
                (sid, str(path), mtime, now_iso, mtime),
            )
            conn.commit()
        finally:
            conn.close()
        all_commits.extend(commits)

    # Match the legacy sort order: newest first by timestamp.
    all_commits.sort(key=lambda c: c.get("timestamp", "") or "", reverse=True)
    return {"commit_count": len(all_commits), "commits": all_commits}


# ---------------------------------------------------------------------------
# Read-only queries
# ---------------------------------------------------------------------------


def query_session_commits(
    session_id: str | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Read commits from the DB without scanning JSONL.

    Use when you know the cache is fresh (e.g. immediately after a
    refresh, or in a sidecar that runs after the IR index rebuild).
    """
    conn = get_connection()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if days is not None:
            cutoff = (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
            )
            # Approximation: compare committed_at lexicographically
            # against an ISO-string cutoff. Works for the ISO-8601
            # `Z`-suffixed timestamps that Claude Code emits.
            from datetime import timedelta

            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
            clauses.append("committed_at >= ?")
            params.append(cutoff_dt.isoformat())

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM session_commits{where} ORDER BY committed_at DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_commit(r) for r in rows]


def get_commit_session_map(days: int = 7) -> dict[str, str]:
    """Return ``{short_sha: full_session_id}`` for commits in the window.

    Triggers a stale-only refresh first so the map reflects the latest
    JSONL state — matches the legacy ``build_session_map`` contract that
    the caller does not need to refresh separately.
    """
    refresh_session_commits(days=days)
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT short_sha, session_id FROM session_commits "
            "WHERE short_sha != ''"
        ).fetchall()
    finally:
        conn.close()
    return {r["short_sha"]: r["session_id"] for r in rows}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_commit(row: Any) -> dict[str, Any]:
    """Project a session_commits row back into the legacy commit dict."""
    return {
        "session_id": row["session_id"],
        "hash": row["sha"],
        "branch": row["branch"],
        "message": row["message"],
        "files_changed": row["files_changed"],
        "timestamp": row["committed_at"],
        "message_index": row["message_index"],
    }
