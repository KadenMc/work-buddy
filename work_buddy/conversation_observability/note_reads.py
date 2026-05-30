"""Durable task-note-read attribution storage and refresh.

Mirrors ``commits.py`` / ``writes.py``: replaces a per-call full-history
JSONL scan with a SQLite-backed cache. Each session JSONL contributes
rows of ``(session_id, task_id, source)`` for the three explicit
"the session pulled this task's content" signals —

  - ``read_tool``: a ``Read`` tool call on ``tasks/notes/<uuid>.md``
  - ``task_read_mcp``: a ``task_read`` MCP call carrying the task id
  - ``task_assign_mcp``: a ``task_assign`` MCP call carrying the id

— so ``sessions_who_read_task`` (and the ``note_reader`` role in
``/wb-task-completeness``) can resolve via O(1) SQL instead of scanning
229 transcripts on every call.

The weaker ``saw_id`` signal (id text without an explicit read) is NOT
persisted; see the schema comment and ``provenance.sessions_who_read_task``'s
``include_saw_id`` for live access.

**No detection logic lives here.** Source detection is delegated to
``provenance._scan_session_for_task`` — the single shared detector behind
both the per-task awareness path and this collector — so the fingerprint
rules can never drift between the two. This module only handles
*enumeration* (which sessions, which candidate tasks) and *persistence*.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.conversation_observability.db import get_connection
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# task id inside free text; note-file path → uuid. Kept local so the
# collector's *candidate discovery* (a cheap per-session prefilter) is
# self-contained; the authoritative per-source detection is in provenance.
_TASK_ID_INLINE_RE = re.compile(r"t-[0-9a-f]{8}")
_NOTE_PATH_RE = re.compile(r"tasks/notes/([0-9a-fA-F-]{36})\.md")


# ---------------------------------------------------------------------------
# Task-store note_uuid ↔ task_id maps (orphan-note guard built in)
# ---------------------------------------------------------------------------


def _task_note_maps() -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Return ``(uuid_to_task, task_to_uuid, known_task_ids)``.

    Reads the SQLite task store directly (bridge-independent). Only live
    tasks appear — a note whose task was deleted is absent, so reads of an
    orphan note never produce a row.
    """
    from work_buddy.obsidian.tasks import store

    uuid_to_task: dict[str, str] = {}
    task_to_uuid: dict[str, str] = {}
    known: set[str] = set()
    conn = store.get_connection()
    try:
        for row in conn.execute(
            "SELECT task_id, note_uuid FROM task_metadata"
        ):
            tid = row["task_id"]
            known.add(tid)
            nu = row["note_uuid"]
            if nu:
                uuid_to_task[nu] = tid
                task_to_uuid[tid] = nu
    finally:
        conn.close()
    return uuid_to_task, task_to_uuid, known


def _candidate_tasks_in_text(
    text: str, uuid_to_task: dict[str, str], known: set[str]
) -> set[str]:
    """Task ids plausibly referenced in one transcript (cheap prefilter).

    Union of (a) task ids that appear literally and are live, and (b)
    tasks whose note path appears. Narrows the set of tasks we run the
    authoritative per-task detector against — typically a handful per
    session, not the whole task table.
    """
    present_ids = {t for t in _TASK_ID_INLINE_RE.findall(text) if t in known}
    present_via_note = {
        uuid_to_task[u]
        for u in _NOTE_PATH_RE.findall(text)
        if u in uuid_to_task
    }
    return present_ids | present_via_note


# ---------------------------------------------------------------------------
# Refresh — scan, persist (mirrors writes.refresh_session_writes)
# ---------------------------------------------------------------------------


def refresh_session_note_reads(
    days: int = 7,
    project: str | None = None,
    max_sessions: int | None = None,
    stale_only: bool = True,
) -> dict[str, Any]:
    """Scan recent JSONL sessions and update the note-read table.

    Stale-only by default via the ``note_reads_scanned_mtime`` ledger
    column on ``observed_sessions`` (this refresher owns it, never
    conflated with the commits/writes/prs columns). ``max_sessions`` caps
    cache MISSES per call so a cold scan splits across cron firings.

    Returns ``{rows_written, scanned_sessions}``.
    """
    from work_buddy.obsidian.tasks.provenance import _scan_session_for_task
    from work_buddy.sessions.inspector import _recent_sessions

    now_iso = datetime.now(timezone.utc).isoformat()
    uuid_to_task, task_to_uuid, known = _task_note_maps()
    if not known:
        return {"rows_written": 0, "scanned_sessions": 0}

    session_paths = _recent_sessions(days, project=project)

    # Pre-pass: pull the scan ledger without holding a connection open.
    conn = get_connection()
    try:
        ledger_rows = conn.execute(
            "SELECT session_id, note_reads_scanned_mtime FROM observed_sessions"
        ).fetchall()
    finally:
        conn.close()
    scanned_at = {
        r["session_id"]: r["note_reads_scanned_mtime"] for r in ledger_rows
    }

    sessions_scanned: set[str] = set()
    rows_written = 0
    misses = 0
    for jsonl_path, sid in session_paths:
        try:
            file_mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue

        cached = scanned_at.get(sid)
        if stale_only and cached is not None and abs(cached - file_mtime) < 1e-6:
            sessions_scanned.add(sid)
            continue

        if max_sessions is not None and misses >= max_sessions:
            continue
        misses += 1
        sessions_scanned.add(sid)

        # Candidate discovery: one text read, cheap regex prefilter.
        try:
            text = jsonl_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        candidates = _candidate_tasks_in_text(text, uuid_to_task, known)

        # Per-candidate authoritative detection (the shared detector).
        emitted: list[tuple[str, str, str, str | None, str | None, int]] = []
        for tid in candidates:
            nu = task_to_uuid.get(tid)
            scan = _scan_session_for_task(jsonl_path, tid, nu)
            for source, ev in scan["sources"].items():
                emitted.append((
                    tid, source, nu,
                    ev.get("first"), ev.get("last"), ev.get("count", 1),
                ))

        conn = get_connection()
        try:
            # Replace this session's rows wholesale — cheap, keeps the
            # table consistent if a task's note_uuid changed.
            conn.execute(
                "DELETE FROM session_task_note_reads WHERE session_id = ?",
                (sid,),
            )
            for tid, source, nu, first, last, count in emitted:
                conn.execute(
                    "INSERT OR REPLACE INTO session_task_note_reads "
                    "(id, session_id, task_id, note_uuid, source, "
                    " first_seen_at, last_seen_at, occurrence_count, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{sid}:{tid}:{source}",
                        sid, tid, nu, source, first, last, count, now_iso,
                    ),
                )
            conn.execute(
                "INSERT INTO observed_sessions "
                "(session_id, source_path, source_mtime, observed_at, "
                " note_reads_scanned_mtime) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                " source_path=excluded.source_path, "
                " note_reads_scanned_mtime=excluded.note_reads_scanned_mtime",
                (sid, str(jsonl_path), file_mtime, now_iso, file_mtime),
            )
            conn.commit()
        finally:
            conn.close()
        rows_written += len(emitted)

    return {
        "rows_written": rows_written,
        "scanned_sessions": len(sessions_scanned),
    }


# ---------------------------------------------------------------------------
# Read-only queries
# ---------------------------------------------------------------------------


def query_reads_for_task(task_id: str) -> list[dict[str, Any]]:
    """All note-read rows for one task (one row per session×source)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM session_task_note_reads WHERE task_id = ? "
            "ORDER BY last_seen_at DESC",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def query_reads_for_session(session_id: str) -> list[dict[str, Any]]:
    """All note-read rows for one session (one row per task×source)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM session_task_note_reads WHERE session_id = ? "
            "ORDER BY last_seen_at DESC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
