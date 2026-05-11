"""Density-promotion heuristic.

Detects candidates for promotion from ``density='sparse'`` to
``density='developed'``. Pure flagging — never auto-promotes (that
would violate V2a — capture promise integrity per the user's
recorded hesitation about hallucinated sub-items).

Two signals:

1. **Parenthetical sub-list in the task text itself.** Patterns like
   "Build MCP measurement tool module (PR/QRS/QT/axis/amplitudes)"
   or "Refactor auth (sessions, tokens, csrf)" suggest the user has
   sub-items in mind already. Catches `(item1, item2, item3)` and
   `(a/b/c/d/e)` shapes.

2. **Sub-bullet structure in the linked note.** A task note whose
   body has >2 sub-bullets in a section is structurally already a
   developed task, even if the database calls it sparse. Captures
   the "I wrote this in detail but didn't update the metadata" case.

The user reviews flagged candidates in a one-time batch. Promotion
is opt-in per task; rejected candidates stay sparse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# A parenthetical that contains either:
#   - 3+ comma-separated items: "(a, b, c)", "(alpha, beta, gamma)"
#   - 3+ slash-separated items: "(a/b/c)", "(PR/QRS/QT)"
#   - any "and" or "or" inside a 3+ item list: "(a, b, and c)"
# Conservative — single items in parens (e.g. "(WIP)") don't fire.
_PAREN_LIST_RE = re.compile(
    r"\("
    r"(?:[^)]+?(?:[,/]|\sand\s|\sor\s)){2,}"  # 2+ separators inside ⇒ 3+ items
    r"[^)]+"
    r"\)"
)


# Bullet-line detector for note bodies. Matches "- foo", "* foo",
# "+ foo", "1. foo", with optional indent. Used to count bullets per
# section heading.
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")


@dataclass
class DensityFlag:
    """A reason a task was flagged as a density-promotion candidate."""

    task_id: str
    signals: list[str]
    sample_evidence: str  # short string showing what triggered flagging


def detect_parenthetical_sublist(task_text: str) -> str | None:
    """Return the first paren-list match in ``task_text`` or None."""
    if not task_text:
        return None
    m = _PAREN_LIST_RE.search(task_text)
    return m.group(0) if m else None


def count_max_bullets_per_section(note_body: str) -> int:
    """Return the largest bullet-count found in any section of ``note_body``.

    Sections are delimited by markdown headings. The empty heading
    (top of file) counts. Whitespace-only headings are skipped.
    """
    if not note_body:
        return 0
    max_count = 0
    current = 0
    for line in note_body.splitlines():
        if _HEADING_RE.match(line):
            if current > max_count:
                max_count = current
            current = 0
            continue
        if _BULLET_LINE_RE.match(line):
            current += 1
    if current > max_count:
        max_count = current
    return max_count


def flag_task(
    *,
    task_id: str,
    task_text: str,
    note_body: str | None = None,
    bullet_threshold: int = 3,
) -> DensityFlag | None:
    """Return a DensityFlag if any signal fires, else None.

    Signals are accumulated — a task that fires both gets one flag
    with a list of two signal names.
    """
    signals: list[str] = []
    evidence_parts: list[str] = []

    paren = detect_parenthetical_sublist(task_text)
    if paren:
        signals.append("parenthetical_sublist")
        evidence_parts.append(paren)

    if note_body:
        bullets = count_max_bullets_per_section(note_body)
        if bullets >= bullet_threshold:
            signals.append(f"note_has_{bullets}_bullets_in_one_section")
            evidence_parts.append(f"max bullets/section = {bullets}")

    if not signals:
        return None

    return DensityFlag(
        task_id=task_id,
        signals=signals,
        sample_evidence=" | ".join(evidence_parts),
    )


def flag_all_sparse_tasks(
    *,
    bullet_threshold: int = 3,
    read_note_body: Any = None,
) -> list[DensityFlag]:
    """Walk every sparse task and return density-promotion flags.

    Args:
        bullet_threshold: Minimum bullet count in a single section
            to fire the note-bullet signal. Default 3 — a section
            with 3+ bullets is already developed-shaped.
        read_note_body: Optional callable ``(note_uuid: str) -> str``
            for resolving note bodies. Defaults to a bridge-backed
            reader. Pass a stub for tests.

    Returns:
        List of DensityFlag, one per task that triggered any signal.
        Empty list when nothing fires.
    """
    from work_buddy.obsidian.tasks import store
    if read_note_body is None:
        read_note_body = _default_note_reader

    flags: list[DensityFlag] = []
    sparse_rows = store.query()  # all unarchived tasks
    for row in sparse_rows:
        if row.get("density") != "sparse":
            continue
        # Read task text from the master file via tag cache or lookup
        # — for now we use the note (same source). If no note, only
        # the parenthetical signal fires.
        task_text = row.get("task_text") or ""
        # Most callers won't have task_text on the row (it's not a
        # column). The signal still works on the body text.
        note_body = ""
        note_uuid = row.get("note_uuid")
        if note_uuid:
            try:
                note_body = read_note_body(note_uuid) or ""
            except Exception:
                note_body = ""
        flag = flag_task(
            task_id=row["task_id"],
            task_text=task_text,
            note_body=note_body,
            bullet_threshold=bullet_threshold,
        )
        if flag:
            flags.append(flag)
    return flags


def _default_note_reader(note_uuid: str) -> str | None:
    """Read a task note's body via the bridge."""
    try:
        from work_buddy.obsidian import bridge
        path = f"tasks/notes/{note_uuid}.md"
        return bridge.read_file(path)
    except Exception:
        return None
