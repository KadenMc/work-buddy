"""Backfill ``task_metadata.created_by_session`` from note prose.

The ``created_by_session`` column (migration v11) is populated at
create time going forward, but historical tasks have their creating
session recorded only in the note body — handoff notes open with a
line like::

    This is a handoff prompt from session 9474f4c7-2a88-47fe-93c1-...

This module is a **re-runnable maintenance pass** (NOT a migration — it
performs Obsidian-bridge / filesystem reads, which migrations must never
do). For every task whose ``created_by_session`` is still NULL and which
has a linked note, it reads the note, extracts the session id from that
line, and records it — but **only on a single unambiguous match**. Tasks
with no match, or with conflicting session ids, are left untouched and
reported so the caller can see what was skipped and why.

Idempotent: rows already populated are skipped, so re-running only ever
fills new gaps.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.obsidian import bridge
from work_buddy.obsidian.tasks import store

logger = get_logger(__name__)

TASK_NOTES_DIR = "tasks/notes"

# "handoff prompt from session <uuid|short-id>" — case-insensitive. The
# id is a Claude Code session UUID (36 chars) or an 8+ char prefix.
_HANDOFF_SESSION_RE = re.compile(
    r"handoff prompt from session\s+([0-9a-f][0-9a-f-]{7,35})",
    re.IGNORECASE,
)


def _read_note(note_uuid: str) -> str | None:
    """Read a task note body by uuid: bridge first, filesystem fallback.

    Mirrors the read path in ``mutations._load_task_payload`` so behavior
    matches the live read surface (and degrades the same way when the
    bridge is down).
    """
    note_path = f"{TASK_NOTES_DIR}/{note_uuid}.md"
    content = bridge.read_file(note_path)
    if content is None:
        fs_path = Path(load_config()["vault_root"]) / note_path
        if fs_path.exists():
            content = fs_path.read_text(encoding="utf-8")
    return content


def _extract_creator_session(note_body: str) -> str | None | _Ambiguous:
    """Return the creating session id from a note body.

    - Exactly one distinct match → that id.
    - No match → None.
    - Two or more *distinct* ids → ``_AMBIGUOUS`` (never guess).
    """
    matches = {m.group(1).lower() for m in _HANDOFF_SESSION_RE.finditer(note_body)}
    if not matches:
        return None
    if len(matches) > 1:
        return _AMBIGUOUS
    return next(iter(matches))


class _Ambiguous:
    """Sentinel: multiple distinct session ids found — refuse to guess."""


_AMBIGUOUS = _Ambiguous()


def _tasks_missing_created_by() -> list[dict[str, Any]]:
    """task_id + note_uuid for live rows whose created_by_session is NULL
    and which have a linked note (the only rows we can backfill from prose)."""
    conn = store.get_connection()
    try:
        rows = conn.execute(
            """SELECT task_id, note_uuid FROM task_metadata
               WHERE created_by_session IS NULL
                 AND note_uuid IS NOT NULL
                 AND deleted_at IS NULL"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _record_created_by(task_id: str, session_id: str) -> None:
    """Set ``created_by_session`` on one row (only called for NULL rows)."""
    conn = store.get_connection()
    try:
        conn.execute(
            "UPDATE task_metadata SET created_by_session = ? WHERE task_id = ?",
            (session_id, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def backfill_created_by(*, dry_run: bool = False) -> dict[str, Any]:
    """Populate ``created_by_session`` for historical tasks from note prose.

    Re-runnable and idempotent. Returns a structured report::

        {
          "scanned": int,           # rows considered (NULL + has note)
          "updated": [task_id, ...],
          "skipped_no_match": [task_id, ...],
          "skipped_ambiguous": [task_id, ...],   # >1 distinct session id
          "skipped_no_note_body": [task_id, ...],# note unreadable/empty
          "errors": [{"task_id", "error"}, ...],
          "dry_run": bool,
        }

    With ``dry_run=True`` nothing is written — useful to preview which
    rows would be filled before committing.
    """
    report: dict[str, Any] = {
        "scanned": 0,
        "updated": [],
        "skipped_no_match": [],
        "skipped_ambiguous": [],
        "skipped_no_note_body": [],
        "errors": [],
        "dry_run": dry_run,
    }

    candidates = _tasks_missing_created_by()
    report["scanned"] = len(candidates)

    for row in candidates:
        task_id = row["task_id"]
        try:
            body = _read_note(row["note_uuid"])
            if not body:
                report["skipped_no_note_body"].append(task_id)
                continue
            result = _extract_creator_session(body)
            if result is None:
                report["skipped_no_match"].append(task_id)
            elif isinstance(result, _Ambiguous):
                report["skipped_ambiguous"].append(task_id)
            else:
                if not dry_run:
                    _record_created_by(task_id, result)
                report["updated"].append(task_id)
        except Exception as exc:  # pragma: no cover — defensive per-row
            report["errors"].append({"task_id": task_id, "error": str(exc)})
            logger.warning("backfill_created_by: %s failed: %s", task_id, exc)

    logger.info(
        "backfill_created_by: scanned=%d updated=%d no_match=%d ambiguous=%d "
        "no_body=%d errors=%d (dry_run=%s)",
        report["scanned"], len(report["updated"]),
        len(report["skipped_no_match"]), len(report["skipped_ambiguous"]),
        len(report["skipped_no_note_body"]), len(report["errors"]), dry_run,
    )
    return report
