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

import functools
import json
import re
from pathlib import Path
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


# Per-source detection keys for an explicit "the session pulled this
# task's content" action. Ordered strongest-first for display.
_READ_SOURCES = ("read_tool", "task_read_mcp", "task_assign_mcp")


def _scan_session_for_task(
    path: Path,
    task_id: str,
    note_uuid: str | None = None,
) -> dict[str, Any]:
    """Single-pass JSONL scan for one session's awareness of one task.

    The shared detector behind both :func:`session_awareness_of_task`
    (which collapses this to a single grade) and
    :func:`sessions_who_read_task` / the durable note-reads collector
    (which want the per-source breakdown). One detection implementation,
    three consumers — so the fingerprint logic can never drift between
    them.

    Distinguishes three explicit-read sources, each a deliberate "agent
    pulled this task's content" action:

    - ``read_tool`` — a ``Read`` tool call on ``tasks/notes/<uuid>.md``.
    - ``task_read_mcp`` — a ``task_read`` MCP call carrying the task id.
    - ``task_assign_mcp`` — a ``task_assign`` MCP call carrying the id.

    Returns::

        {
          "sources": {
             "<source>": {"first": iso|None, "last": iso|None, "count": int},
             ...                       # only keys that actually fired
          },
          "saw_id": bool,   # task id / note path appeared in any raw line
        }

    Raises nothing it can avoid: an unreadable file yields the empty
    shape (``{"sources": {}, "saw_id": False}``); the caller decides
    whether that means ``no_transcript`` (path didn't resolve) or
    ``none`` (file present, task absent).
    """
    note_frag = f"tasks/notes/{note_uuid}.md" if note_uuid else None
    sources: dict[str, dict[str, Any]] = {}
    saw_id = False

    def _record(source: str, ts: str | None) -> None:
        slot = sources.get(source)
        if slot is None:
            sources[source] = {"first": ts, "last": ts, "count": 1}
            return
        slot["count"] += 1
        if ts:
            if slot["first"] is None or ts < slot["first"]:
                slot["first"] = ts
            if slot["last"] is None or ts > slot["last"]:
                slot["last"] = ts

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
                ts = entry.get("timestamp") if isinstance(entry, dict) else None
                for block in _iter_tool_use_blocks(entry):
                    name = block.get("name", "") or ""
                    inp = block.get("input", {}) or {}
                    # (a) Read tool on the note file.
                    if (
                        note_frag
                        and name == "Read"
                        and note_frag in str(inp.get("file_path", ""))
                    ):
                        _record("read_tool", ts)
                    # (b) task_read / task_assign MCP call carrying the id.
                    inp_str = json.dumps(inp, default=str)
                    if task_id in inp_str:
                        if "task_read" in inp_str:
                            _record("task_read_mcp", ts)
                        if "task_assign" in inp_str:
                            _record("task_assign_mcp", ts)
    except OSError:
        return {"sources": {}, "saw_id": False}

    return {"sources": sources, "saw_id": saw_id}


def session_awareness_of_task(
    session_id: str,
    task_id: str,
    note_uuid: str | None = None,
) -> str:
    """Grade how aware ``session_id`` was of ``task_id`` from its JSONL.

    Thin collapse over :func:`_scan_session_for_task`. Returns one of:
    - ``"read_note"`` — strongest detectable: any explicit read source
      fired (``task_read``/``task_assign`` MCP call carrying the id, OR a
      ``Read`` tool call on the task's note file). The session
      demonstrably pulled the task's content.
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

    scan = _scan_session_for_task(path, task_id, note_uuid)
    if scan["sources"]:
        return "read_note"
    if scan["saw_id"]:
        return "saw_id"
    return "none"


# ── Inverse enumeration: who read this task's note ───────────────────


def _reader_record(
    session_id: str, scan: dict[str, Any], *, include_saw_id: bool
) -> dict[str, Any] | None:
    """Shape one :func:`_scan_session_for_task` result into a reader row.

    Returns ``None`` when the session doesn't qualify (no explicit read,
    and either no ``saw_id`` or ``include_saw_id`` is off).
    """
    sources = scan.get("sources") or {}
    if sources:
        awareness = "read_note"
    elif scan.get("saw_id") and include_saw_id:
        awareness = "saw_id"
    else:
        return None
    firsts = [s["first"] for s in sources.values() if s.get("first")]
    lasts = [s["last"] for s in sources.values() if s.get("last")]
    return {
        "session_id": session_id,
        "awareness": awareness,
        "sources": dict(sources),
        "first_seen": min(firsts) if firsts else None,
        "last_seen": max(lasts) if lasts else None,
    }


@functools.lru_cache(maxsize=256)
def _jsonl_reader_scan(
    task_id: str, note_uuid: str | None, include_saw_id: bool
) -> tuple[dict[str, Any], ...]:
    """All sessions whose JSONL shows awareness of ``task_id``.

    The full-history fallback for :func:`sessions_who_read_task` — used
    when the durable ``session_task_note_reads`` table isn't populated.
    Enumerates every session (no recency cutoff: a task can be old), with
    a cheap bytes-substring prefilter so files that never mention the task
    are skipped without a line-by-line scan. Cached per
    ``(task_id, note_uuid, include_saw_id)`` — safe within a single
    process; the underlying transcripts don't change mid-run.
    """
    from work_buddy.sessions import inspector

    tid_bytes = task_id.encode()
    note_bytes = (
        f"tasks/notes/{note_uuid}.md".encode() if note_uuid else None
    )
    out: list[dict[str, Any]] = []
    for path, sid in inspector._all_sessions():
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if tid_bytes not in blob and (
            note_bytes is None or note_bytes not in blob
        ):
            continue  # prefilter: this transcript never names the task
        scan = _scan_session_for_task(path, task_id, note_uuid)
        rec = _reader_record(sid, scan, include_saw_id=include_saw_id)
        if rec is not None:
            out.append(rec)
    return tuple(out)


def sessions_who_read_task(
    task_id: str,
    note_uuid: str | None = None,
    *,
    include_saw_id: bool = False,
    exclude_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Sessions whose transcripts show awareness of ``task_id``.

    The inverse of :func:`session_awareness_of_task`: given a task, which
    sessions *read* its note (or merely saw its id pass by). This is the
    Rung-3 surface — a session that read the note and did work but never
    referenced the task id in a commit is invisible to ``developed_by``,
    yet is exactly the "forgot to toggle" developer we want to find.

    Prefers the durable ``session_task_note_reads`` table when it has rows
    for this task (O(1) SQL); otherwise falls back to a full-history JSONL
    scan via :func:`_jsonl_reader_scan`.

    Args:
        task_id: Task id (``t-XXXXXXXX``).
        note_uuid: The task's note UUID; enables ``Read``-tool detection
            on ``tasks/notes/<uuid>.md``. Without it only the MCP-call
            signals fire.
        include_saw_id: When True, also return sessions that merely
            mention the id without an explicit read (weak signal — off by
            default, kept for ad-hoc audits).
        exclude_session_id: Drop this session from the result (e.g. the
            caller's own session, so ``/wb-task-completeness`` doesn't
            list itself).

    Returns:
        ``[{session_id, awareness, sources, first_seen, last_seen}]``,
        ranked ``read_note`` before ``saw_id`` then ``last_seen`` desc.
    """
    records: list[dict[str, Any]] | None = None

    # SQL fast-path (active once the durable table is populated — Piece 3).
    try:
        from work_buddy.conversation_observability import note_reads

        rows = note_reads.query_reads_for_task(task_id)
        if rows:
            agg: dict[str, dict[str, Any]] = {}
            for r in rows:
                sid = r["session_id"]
                slot = agg.setdefault(
                    sid,
                    {
                        "session_id": sid,
                        "awareness": "read_note",  # table stores explicit reads only
                        "sources": {},
                        "first_seen": None,
                        "last_seen": None,
                    },
                )
                slot["sources"][r["source"]] = {
                    "first": r.get("first_seen_at"),
                    "last": r.get("last_seen_at"),
                    "count": r.get("occurrence_count", 1),
                }
                fs, ls = r.get("first_seen_at"), r.get("last_seen_at")
                if fs and (slot["first_seen"] is None or fs < slot["first_seen"]):
                    slot["first_seen"] = fs
                if ls and (slot["last_seen"] is None or ls > slot["last_seen"]):
                    slot["last_seen"] = ls
            records = list(agg.values())
    except Exception as exc:  # pragma: no cover — table/import not present yet
        logger.debug("sessions_who_read_task: SQL path unavailable: %s", exc)

    # Fallback: full JSONL scan.
    if records is None:
        records = [dict(r) for r in _jsonl_reader_scan(
            task_id, note_uuid, include_saw_id
        )]

    if exclude_session_id:
        records = [r for r in records if r["session_id"] != exclude_session_id]

    # Two stable passes: first newest-first (empty/None last_seen sorts
    # last under reverse), then by awareness tier — the tier sort is
    # stable so it preserves the time order within each tier.
    records.sort(key=lambda r: r["last_seen"] or "", reverse=True)
    _rank = {"read_note": 0, "saw_id": 1}
    records.sort(key=lambda r: _rank.get(r["awareness"], 9))
    return records


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
