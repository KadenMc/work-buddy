"""Task↔session **provenance roles**: created-by / assigned / developed-by.

A task relates to sessions in three structurally distinct ways, and they
are *not the same kind of fact*:

- **created_by** — the one session that minted the task. A *declared*
  fact, stored on ``task_metadata.created_by_session`` (migration v11).
- **assigned** — the sessions that claimed it via ``task_assign``. A
  *declared* fact, in the ``task_sessions`` table.
- **developed_by** — the sessions whose work actually satisfied the
  task. A *derived* fact, computed here at read time on a
  confidence-graded ladder:
    - Rung 1 — assigned **and** has commits referencing the task id.
    - Rung 2 — commits reference the task id, no assignment.
    - Rung 3 — developed the *intent* with no structural link
      (done-differently, no id anywhere). NOT detectable structurally;
      it is exactly what ``/wb-task-completeness``'s investigate step
      does. We never emit a Rung-3 row — we hand it to reasoning.

Orthogonal to the rungs is an **awareness** signal: did the developing
session actually *read* the task note (a structural JSONL fingerprint),
or did it converge on the same intent without ever seeing it? This
distinguishes *informed* development from *convergent* development.

Everything here is read-only; ``developed_by`` is never stored (it would
go stale as new commits land).
"""

from __future__ import annotations

import json
import re
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks import store

logger = get_logger(__name__)

# Task id format: ``t-`` + 8 hex (see mutations._generate_task_id).
_TASK_ID_RE = re.compile(r"^t-[0-9a-f]{8}$")
# Same id, unanchored — for finding ids inside commit-message text.
_TASK_ID_INLINE_RE = re.compile(r"t-[0-9a-f]{8}")

_HOOK = "wb-task-completeness"


# ── Awareness (note-read fingerprint from session JSONL) ─────────────


def _iter_tool_use_blocks(entry: dict[str, Any]):
    """Yield ``tool_use`` content blocks from one JSONL entry, defensively."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def session_awareness_of_task(
    session_id: str,
    task_id: str,
    note_uuid: str | None = None,
) -> str:
    """Grade how aware ``session_id`` was of ``task_id`` from its JSONL.

    Returns one of:
    - ``"read_note"`` — strongest detectable: a ``task_read``/``task_assign``
      MCP call carrying the task id, OR a ``Read`` tool call on the task's
      note file. The session demonstrably pulled the task's content.
    - ``"saw_id"`` — the task id or note uuid appears in the transcript
      (e.g. via a task list, or the session's own ``git commit`` command)
      but no explicit read — weak awareness.
    - ``"none"`` — transcript present, but the task is never mentioned.
      Candidate *convergent* development (the session never saw the task,
      yet hit the same intent). **Absence is not proof of independence**
      — a handoff can inject note content with no explicit read call — so
      this is a hypothesis for the Rung-3 reasoning step, never a hard
      label.
    - ``"no_transcript"`` — the session has no readable JSONL
      (sidecar-synthesized / pruned / ambiguous id). Distinct from
      ``"none"``: we *can't tell*, rather than *saw nothing*. Classified
      as ``unknown``, never as convergent.
    """
    try:
        from work_buddy.sessions.inspector import resolve_session_path

        path, _ = resolve_session_path(session_id)
    except Exception:  # no readable transcript → can't tell (not "convergent")
        return "no_transcript"

    note_frag = f"tasks/notes/{note_uuid}.md" if note_uuid else None
    read_note = False
    saw_id = False

    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                hit_id = task_id in raw
                hit_note = bool(note_frag and note_frag in raw)
                if not hit_id and not hit_note:
                    continue  # cheap prefilter — line mentions neither
                saw_id = saw_id or hit_id or hit_note
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                for block in _iter_tool_use_blocks(entry):
                    name = block.get("name", "") or ""
                    inp = block.get("input", {}) or {}
                    # (a) Read tool on the note file.
                    if (
                        note_frag
                        and name == "Read"
                        and note_frag in str(inp.get("file_path", ""))
                    ):
                        read_note = True
                    # (b) task_read / task_assign MCP call carrying the id.
                    inp_str = json.dumps(inp, default=str)
                    if ("task_read" in inp_str or "task_assign" in inp_str) and (
                        task_id in inp_str
                    ):
                        read_note = True
    except OSError:
        return "no_transcript"

    if read_note:
        return "read_note"
    if saw_id:
        return "saw_id"
    return "none"


def _classify(awareness: str) -> str:
    """Map an awareness level to an informed/convergent classification."""
    if awareness in ("assigned", "read_note"):
        return "informed"
    if awareness == "none":
        return "convergent"
    # "saw_id" (aware it exists — e.g. via its own commit convention — but
    # not demonstrably read), "no_transcript" (can't tell), or
    # "not_computed" (awareness scan skipped on a hot path) → unclassifiable.
    # NOTE: a commit-ref developer always has the id in its own transcript,
    # so it classifies as informed/unknown, never convergent — convergent
    # is reserved for the Rung-3 reasoning layer (a developer with no
    # structural link at all), which this function never sees.
    return "unknown"


# ── developed_by derivation ──────────────────────────────────────────


def _committers_for_task(task_id: str) -> dict[str, list[dict[str, Any]]]:
    """``{session_id: [commit, ...]}`` for commits referencing the task id.

    Read-only scan of the durable ``session_commits`` table (no JSONL
    refresh — kept cheap). Returns ``{}`` if the conversation-observability
    DB is unavailable.
    """
    try:
        from work_buddy.conversation_observability import commits as commits_mod

        rows = commits_mod.query_commits_for_task(task_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("query_commits_for_task(%s) failed: %s", task_id, exc)
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for c in rows:
        sid = c.get("session_id")
        if sid:
            out.setdefault(sid, []).append(c)
    return out


def build_developed_by(
    task_id: str,
    *,
    note_uuid: str | None = None,
    assigned_session_ids: list[str] | None = None,
    include_awareness: bool = True,
) -> list[dict[str, Any]]:
    """Derive the structural developer entries for a task (Rungs 1–2).

    One entry per session that committed referencing the task id, each
    tagged with its rung, provenance, awareness, and classification.
    Assigned-but-non-committing sessions stay under ``assigned`` — they
    are *not* developers by this (structural) definition.

    ``include_awareness`` gates the per-session JSONL scan that grades
    note-read awareness. It is ON by default (the dedicated
    ``task_provenance`` capability and the dashboard want it) but callers
    on a hot path (e.g. ``read_task``) pass ``False`` to skip the file
    reads — those entries get ``awareness="not_computed"``. Assigned
    sessions are always ``"assigned"`` (free, no scan).
    """
    assigned = set(assigned_session_ids or [])
    committers = _committers_for_task(task_id)

    entries: list[dict[str, Any]] = []
    for sid, commits in committers.items():
        is_assigned = sid in assigned
        rung = 1 if is_assigned else 2
        provenance = "assigned+commit" if is_assigned else "commit-ref"
        if is_assigned:
            awareness = "assigned"
        elif include_awareness:
            awareness = session_awareness_of_task(sid, task_id, note_uuid)
        else:
            awareness = "not_computed"
        evidence = [
            {
                "kind": "commit",
                "sha": c.get("hash"),
                "committed_at": c.get("timestamp"),
                "message_excerpt": (c.get("message") or "").strip().split("\n")[0][:120],
            }
            for c in commits
        ]
        entries.append({
            "session_id": sid,
            "rung": rung,
            "confidence": "high",
            "provenance": provenance,
            "awareness": awareness,
            "classification": _classify(awareness),
            "evidence": evidence,
        })

    # Strongest rung first (Rung 1 before Rung 2), then by session id for
    # stable ordering.
    entries.sort(key=lambda e: (e["rung"], e["session_id"]))
    return entries


# ── Unified read surface ─────────────────────────────────────────────


def build_task_provenance(
    task_id: str, *, include_awareness: bool = True
) -> dict[str, Any]:
    """The three provenance roles for one task, plus a Rung-3 signpost.

    Shape::

        {
          "task_id": str,
          "created_by": str | None,        # session id, or None if unrecorded
          "assigned": [{task_id, session_id, assigned_at}, ...],
          "developed_by": [ <build_developed_by entries> ],
          "intent_attribution": {"computed": False, "hook": ..., "reason": ...},
        }

    ``intent_attribution`` is always ``computed: False`` — intent-level
    (Rung-3) attribution is a reasoning task, never precomputed here.
    """
    row = store.get(task_id, include_deleted=True)
    note_uuid = row.get("note_uuid") if row else None
    created_by = row.get("created_by_session") if row else None

    assigned = store.get_sessions(task_id)
    assigned_ids = [a["session_id"] for a in assigned]

    developed_by = build_developed_by(
        task_id,
        note_uuid=note_uuid,
        assigned_session_ids=assigned_ids,
        include_awareness=include_awareness,
    )

    if developed_by:
        reason = (
            "developed_by lists only structurally-attributed sessions "
            "(assignment and/or a commit referencing the task id). "
            "Intent-level attribution without a structural link is not "
            f"computed here — confirm via /{_HOOK}."
        )
    else:
        reason = (
            "No structural developer signal: no commit references this task "
            "id and no assignment intersects the commit record. If the task's "
            "intent was satisfied without a task-id reference (e.g. "
            "done-differently), that is Rung-3 / intent-level attribution — "
            f"not computed here; investigate via /{_HOOK}."
        )

    return {
        "task_id": task_id,
        "created_by": created_by,
        "assigned": assigned,
        "developed_by": developed_by,
        "intent_attribution": {
            "computed": False,
            "hook": _HOOK,
            "reason": reason,
        },
    }


# ── Per-session inverse (for the dashboard "Tasks" rail) ─────────────


def _tasks_developed_by_session(session_id: str) -> set[str]:
    """Task ids referenced by this session's own commit subjects."""
    try:
        from work_buddy.conversation_observability import commits as commits_mod

        rows = commits_mod.query_session_commits(session_id=session_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("query_session_commits(%s) failed: %s", session_id, exc)
        return set()
    ids: set[str] = set()
    for c in rows:
        ids.update(_TASK_ID_INLINE_RE.findall(c.get("message") or ""))
    return ids


def build_session_task_roles(session_id: str) -> dict[str, Any]:
    """Per-session inverse of :func:`build_task_provenance`.

    The tasks this session related to, with the role(s) it played in each
    — the data behind the dashboard chat-detail "Tasks" rail. Roles per
    task (a session can hold several):

    - ``created`` — this session minted the task (``created_by_session``).
    - ``assigned`` — this session claimed it via ``task_assign``.
    - ``developed`` — one of this session's commits references the task id.

    Returns ``{"session_id": ..., "tasks": [{task_id, task_text, state,
    roles, assigned_at}, ...]}`` sorted by task id. Bridge-independent
    (reads SQLite + the commit DB), so it stays callable when the Obsidian
    bridge is down.
    """
    created_ids = set(store.get_tasks_created_by(session_id))
    assigned_at = {
        r["task_id"]: r.get("assigned_at")
        for r in store.get_tasks_for_session(session_id)
    }
    developed_ids = _tasks_developed_by_session(session_id)

    tasks: list[dict[str, Any]] = []
    for tid in sorted(created_ids | set(assigned_at) | developed_ids):
        roles = []
        if tid in created_ids:
            roles.append("created")
        if tid in assigned_at:
            roles.append("assigned")
        if tid in developed_ids:
            roles.append("developed")
        rec = store.get(tid) or {}
        tasks.append({
            "task_id": tid,
            "task_text": rec.get("description"),
            "state": rec.get("state"),
            "roles": roles,
            "assigned_at": assigned_at.get(tid),
        })
    return {"session_id": session_id, "tasks": tasks}
