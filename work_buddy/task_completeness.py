"""``/wb-task-completeness`` orchestration helper — gather completion evidence.

A single pure-ish callable that the ``tasks/task-completeness`` workflow
uses for its ``gather-evidence`` auto_run code step.

:func:`gather_completeness_evidence` composes the work-buddy-native
signals an agent needs to judge whether a task was *already* completed,
so the agent doesn't have to issue a dozen tool calls by hand:

* the task payload + linked note (``read_task`` — already includes
  ``assigned_sessions``),
* per assigned session: the commits attributed to it (with a targeted,
  bounded refresh so freshly-landed fixes show up), its file writes, and
  its cached topic summary.

The agent then does the *adaptive* part in the reasoning step — grepping
the actual code, ``git log --grep`` / ``gh`` keyword search, running
tests — none of which is work-buddy-native and so can't be auto-run.

Like the morning routine's collectors and ``task_me.load_context_for_task_me``,
every sub-call degrades gracefully: a failure sets ``status="degraded"``
and appends to ``errors`` rather than aborting the whole gather, so the
investigate step always gets *something* to reason against.

The workflow definition lives in the knowledge store under
``tasks/task-completeness`` (authored via ``workflow_create``); the
slash-command launcher in ``.claude/commands/wb-task-completeness.md``.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def gather_completeness_evidence(task_id: str) -> dict[str, Any]:
    """Collect work-buddy-native evidence about a task's completion state.

    Args:
        task_id: Task ID to investigate (e.g. ``"t-99b8a4ff"``).

    Returns:
        A dict the investigate step reasons against:

        - ``status``: ``"ok"`` | ``"degraded"`` | ``"error"``
          (``"error"`` only when the task itself can't be read)
        - ``task_id``: echoed back
        - ``task``: the read-only task payload (text, state, urgency,
          contract, note_content, …) — empty dict on read failure
        - ``assigned_sessions``: list of ``{task_id, session_id,
          assigned_at}`` rows (the sessions that ever claimed this task)
        - ``session_evidence``: per-session
          ``{session_id, assigned_at, commits, writes, summary}``
        - ``cache_note``: human-readable note on data freshness +
          guidance (e.g. "no sessions assigned — rely on git/PR search")
        - ``now_iso``: assembly timestamp
        - ``errors``: list of ``{step, error}`` for any degraded sub-call
    """
    out: dict[str, Any] = {
        "status": "ok",
        "task_id": task_id,
        "task": {},
        "assigned_sessions": [],
        "session_evidence": [],
        "cache_note": "",
        "now_iso": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "errors": [],
    }

    # --- Task payload (also carries assigned_sessions) ------------------
    try:
        from work_buddy.obsidian.tasks import mutations
        payload = mutations.read_task(task_id)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("task_completeness: read_task failed: %s", exc)
        out["status"] = "error"
        out["errors"].append({"step": "read_task", "error": str(exc)})
        out["cache_note"] = (
            "Could not read the task. The investigate step must fall back "
            "to native git/PR keyword search."
        )
        return out

    if not payload.get("success"):
        out["status"] = "error"
        out["task"] = payload
        out["cache_note"] = payload.get("message", "Task not found.")
        return out

    out["task"] = payload
    sessions = payload.get("assigned_sessions") or []
    out["assigned_sessions"] = sessions

    if not sessions:
        out["cache_note"] = (
            "No sessions are assigned to this task. There is no "
            "session->commit linkage to lean on — the investigate step "
            "should rely on native `git log --grep`, `gh` PR/commit "
            "search, and reading the code/tests directly."
        )
        return out

    # --- Per-session evidence ------------------------------------------
    # Refresh each assigned session's commits individually: passing an
    # explicit session_id forces a single-session rescan (bounded work,
    # so we stay within the auto_run timeout even on a cold cache), and
    # guarantees a fix that landed today is attributed before we query.
    from work_buddy.conversation_observability import commits as commits_mod
    from work_buddy.conversation_observability import writes as writes_mod

    writes_stale = False
    for sess in sessions:
        sid = sess.get("session_id")
        if not sid:
            continue
        entry: dict[str, Any] = {
            "session_id": sid,
            "assigned_at": sess.get("assigned_at"),
            "commits": [],
            "writes": [],
            "summary": None,
            "note": None,
        }

        # Commits (targeted refresh, then read). The refresh is a
        # freshening optimization — if the session has no Claude Code
        # transcript to scan (sidecar-synthesized session id, or a
        # pruned/older session), it raises "no session found". That is
        # NOT a degradation: it just means there's no session->commit
        # linkage for this assignment, so we note it (steering the agent
        # to native git/PR search) and still query whatever is cached.
        try:
            commits_mod.refresh_session_commits(session_id=sid)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug(
                "task_completeness: commit refresh skipped for %s: %s", sid, exc
            )
            entry["note"] = (
                "No conversation transcript for this session "
                "(sidecar/pruned) — no session->commit linkage; use native "
                "git/PR keyword search to attribute the work."
            )
        try:
            entry["commits"] = commits_mod.query_session_commits(session_id=sid)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug(
                "task_completeness: commit query failed for %s: %s", sid, exc
            )
            out["status"] = "degraded"
            out["errors"].append(
                {"step": f"query_commits:{sid}", "error": str(exc)}
            )

        # File writes (read the existing cache; a broad refresh would
        # scan every recent session's JSONL + run git, which risks the
        # auto_run timeout — flag staleness instead and let the
        # investigate step force a refresh via the
        # conversation_observability_refresh capability if it matters).
        try:
            entry["writes"] = writes_mod.query_session_writes(session_id=sid)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug(
                "task_completeness: writes query failed for %s: %s", sid, exc
            )
            out["status"] = "degraded"
            out["errors"].append(
                {"step": f"query_writes:{sid}", "error": str(exc)}
            )
        else:
            writes_stale = True

        # Topic summary (best-effort; often absent).
        try:
            from work_buddy.conversation_observability.session_summary_row import (
                session_summary_row,
            )
            entry["summary"] = session_summary_row(sid)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug(
                "task_completeness: summary lookup failed for %s: %s", sid, exc
            )

        out["session_evidence"].append(entry)

    notes = [
        "Commit attribution was refreshed per-session before querying.",
    ]
    if writes_stale:
        notes.append(
            "File-write rows come from the existing cache and may be stale; "
            "call `conversation_observability_refresh` then re-query if the "
            "dirty/committed state is decision-relevant."
        )
    out["cache_note"] = " ".join(notes)
    return out
