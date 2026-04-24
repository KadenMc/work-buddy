"""Rewrite the Running Notes section after backlog routing decisions.

Operates on **line-range** thread dicts (each carrying its own
``lines: list[int]`` of 1-based line numbers in the original Running
Notes body) plus a routing record describing what the user decided to
do with each thread. Produces a new Running Notes string that strips
processed content and preserves the rest.

This replaces the deleted tagged-text rewrite, which stripped inline
``<!-- [t_xxx] -->`` annotations. The new approach is substrate-aware:
the line-range segmentation already tells us *which* original lines
belong to *which* thread, so we just emit the lines of threads we're
keeping plus all unassigned lines.

Decision rules (per thread, from ``routing_record["items"]``):

  * ``action="skip"``    → keep the thread's lines.
  * ``action="route"``   → drop the thread's lines.
  * ``action="delete"``  → drop the thread's lines.
  * ``action="split"``   → use ``rewrite_map[id]``:
      - ``str``  → drop original lines, insert this string at the
        position where the thread's first line was.
      - ``None`` → drop the thread's lines.
      - missing → log warning, conservatively keep the lines (treat
        as skip). Silent data loss is the worse failure mode here.

Multi-thread overlap rule:

  A line that appears in two or more threads is kept if **any** of its
  memberships is in the keep-decision set; only dropped if **all**
  memberships are drop-decisions. Conservative because losing a line
  because of a drop on one of its memberships when another keeps it
  would be silent data loss.

Unassigned lines (blanks, ``---`` separators, anything outside any
thread's ``lines`` list) are always kept. The line-range segmentation
guarantees every non-blank content line is in at least one thread, so
"unassigned" should mean structural content.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Section-locating regexes (unchanged from the deleted tagged-text rewrite)
# ---------------------------------------------------------------------------

_RUNNING_NOTES_HEADER_RE = re.compile(
    r"^#\s+\*{0,2}Running Notes\s*/\s*Considerations\*{0,2}\s*$",
    re.MULTILINE,
)
_RUNNING_END_RE = re.compile(r"^%\s*RUNNING\s+END\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^#\s+\*{0,2}[A-Z]", re.MULTILINE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_KEEP_ACTIONS = frozenset({"skip"})
_DROP_ACTIONS = frozenset({"route", "delete"})


def build_rewrite_preview(
    *,
    original_text: str,
    threads: list[dict[str, Any]],
    routing_record: dict[str, Any],
    rewrite_map: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Build a preview of the rewritten Running Notes without writing.

    Args:
        original_text: The Running Notes section body as a single string
            (1-based line indexing aligned with the line-range
            segmentation that produced ``threads``).
        threads: Thread dicts. Each MUST carry ``id`` (str) and
            ``lines`` (list of 1-based ints). The other fields
            (``raw_text``, ``line_count``, …) are not required here.
        routing_record: Output of the routing step. Expected shape:
            ``{"items": [{"id": str, "action": str, ...}, ...]}``.
            Items lacking an action are treated as ``skip`` (keep).
        rewrite_map: Optional ``{thread_id: replacement_text_or_None}``
            override. ``None`` means drop, string means replace with
            the given text. Required for ``action="split"`` threads;
            ignored for skip/route/delete actions.

    Returns:
        Dict with:
            ``rewritten_text`` (str): the new Running Notes body.
            ``removed_ids``   (list[str]): threads whose content was dropped.
            ``kept_ids``      (list[str]): threads whose content was kept.
            ``summary``       (str): short human-readable counts.
    """
    rewrite_map = rewrite_map or {}
    decision_by_id: dict[str, str] = {}
    for item in routing_record.get("items", []) or []:
        tid = item.get("id")
        if isinstance(tid, str):
            decision_by_id[tid] = item.get("action", "skip")

    # Resolve each thread's effective keep/drop and split replacement text.
    keep_threads: set[str] = set()
    drop_threads: set[str] = set()
    split_replacements: dict[str, str | None] = {}

    for t in threads:
        tid = t.get("id")
        if not isinstance(tid, str):
            continue
        action = decision_by_id.get(tid, "skip")

        if action == "split":
            if tid in rewrite_map:
                split_replacements[tid] = rewrite_map[tid]
            else:
                logger.warning(
                    "rewrite: split thread %s has no entry in rewrite_map; "
                    "keeping its lines (conservative). Provide a "
                    "rewrite_map entry to drop or replace.",
                    tid,
                )
                keep_threads.add(tid)
        elif action in _KEEP_ACTIONS:
            keep_threads.add(tid)
        elif action in _DROP_ACTIONS:
            drop_threads.add(tid)
        else:
            # Unknown action — be conservative, keep.
            logger.warning(
                "rewrite: unknown action %r for thread %s; keeping its lines.",
                action, tid,
            )
            keep_threads.add(tid)

    # Build a per-line decision map: 1-based line number → set of thread ids
    # it belongs to (for overlap resolution).
    n_lines = len(original_text.split("\n"))
    line_threads: dict[int, set[str]] = {}
    for t in threads:
        tid = t.get("id")
        if not isinstance(tid, str):
            continue
        for ln in t.get("lines", []) or []:
            if isinstance(ln, int) and 1 <= ln <= n_lines:
                line_threads.setdefault(ln, set()).add(tid)

    # Precompute split-replacement insertion points: at the FIRST line
    # of each split thread, emit the replacement text (or skip emission
    # if the replacement is None / drop).
    split_first_line: dict[int, str | None] = {}
    split_lines: set[int] = set()
    for tid, replacement in split_replacements.items():
        thread = next((t for t in threads if t.get("id") == tid), None)
        if thread is None:
            continue
        lines = sorted(int(ln) for ln in thread.get("lines", []) if isinstance(ln, int))
        if not lines:
            continue
        split_first_line[lines[0]] = replacement
        split_lines.update(lines)

    # Walk the original text line-by-line, applying decisions.
    raw_lines = original_text.split("\n")
    output: list[str] = []
    for i, line in enumerate(raw_lines, start=1):
        # Split-thread membership takes precedence over keep/drop
        # decisions for the lines covered by the split.
        if i in split_lines:
            if i in split_first_line:
                replacement = split_first_line[i]
                if replacement is not None:
                    output.append(replacement)
            # All other lines of the split are dropped (the replacement,
            # if any, was emitted at the first-line position).
            continue

        memberships = line_threads.get(i, set())
        if not memberships:
            # Unassigned line — always kept.
            output.append(line)
            continue

        # Multi-thread keep/drop resolution: keep if ANY membership is
        # a keep-decision; drop only if ALL memberships are drop-decisions.
        any_keep = any(m in keep_threads for m in memberships)
        all_drop = memberships and all(m in drop_threads for m in memberships)
        if any_keep or not all_drop:
            output.append(line)

    rewritten = _collapse_blank_runs("\n".join(output))

    kept_ids = sorted(keep_threads)
    # Removed = drop threads + split threads that produced None replacement.
    removed = set(drop_threads)
    for tid, repl in split_replacements.items():
        if repl is None:
            removed.add(tid)
    removed_ids = sorted(removed)

    summary = (
        f"kept {len(kept_ids)} thread(s), removed {len(removed_ids)}, "
        f"split-replaced {sum(1 for r in split_replacements.values() if r is not None)}"
    )

    return {
        "rewritten_text": rewritten,
        "kept_ids": kept_ids,
        "removed_ids": removed_ids,
        "summary": summary,
    }


@requires_consent(
    "journal.rewrite_running_notes",
    reason="Rewrite the Running Notes section of a daily journal file with processed items removed.",
    risk="high",
)
def rewrite_running_notes(
    *,
    journal_path: str | Path,
    original_text: str,
    threads: list[dict[str, Any]],
    routing_record: dict[str, Any],
    original_file_content: str,
    rewrite_map: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Apply the rewrite to the journal file on disk. Consent-gated.

    Args:
        journal_path: Path to the journal file (e.g.
            ``<vault>/journal/2026-04-24.md``).
        original_text: The Running Notes body that ``threads`` was
            segmented from.
        threads: Thread dicts with ``id`` and ``lines`` (see
            :func:`build_rewrite_preview`).
        routing_record: Per-thread routing decisions.
        original_file_content: The full file content captured before
            rewrite — used to detect concurrent modifications. If the
            on-disk content has changed, the write is skipped.
        rewrite_map: Optional explicit per-thread replacement map for
            split actions.

    Returns:
        ``{success, file, message, preview}`` — preview is the
        :func:`build_rewrite_preview` output for inspection.
    """
    path = Path(journal_path)
    preview = build_rewrite_preview(
        original_text=original_text,
        threads=threads,
        routing_record=routing_record,
        rewrite_map=rewrite_map,
    )

    # Concurrent-modification check: refuse to write if the file changed.
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as e:
        return {
            "success": False, "file": str(path),
            "message": f"Read error: {e}", "preview": preview,
        }
    if current != original_file_content:
        return {
            "success": False, "file": str(path),
            "message": (
                "File changed on disk since the rewrite was prepared. "
                "Aborting to avoid clobbering concurrent edits."
            ),
            "preview": preview,
        }

    header = _RUNNING_NOTES_HEADER_RE.search(current)
    if not header:
        return {
            "success": False, "file": str(path),
            "message": "Running Notes section not found in journal.",
            "preview": preview,
        }

    body_start = header.end()
    section_end = len(current)
    end_marker = _RUNNING_END_RE.search(current, body_start)
    if end_marker:
        section_end = end_marker.start()
    else:
        next_heading = _NEXT_HEADING_RE.search(current, body_start)
        if next_heading:
            section_end = next_heading.start()

    new_content = (
        current[:body_start]
        + "\n\n"
        + preview["rewritten_text"]
        + "\n\n"
        + current[section_end:]
    )

    try:
        path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return {
            "success": False, "file": str(path),
            "message": f"File write error: {e}", "preview": preview,
        }

    return {
        "success": True, "file": str(path),
        "message": preview["summary"], "preview": preview,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _collapse_blank_runs(text: str) -> str:
    """Collapse runs of 2+ blank lines to a single blank line; trim trailing whitespace."""
    lines = text.split("\n")
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                out.append(line)
        else:
            blank_run = 0
            out.append(line)
    while out and out[-1].strip() == "":
        out.pop()
    return "\n".join(out)
