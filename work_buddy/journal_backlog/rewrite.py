"""Rewrite the Running Notes section after routing decisions are applied."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from work_buddy.consent import requires_consent
from work_buddy.journal_backlog.segment import (
    _CLOSE_TAG_RE,
    _MULTI_TAG_RE,
    _OPEN_TAG_RE,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Section header for locating Running Notes in the full file
_RUNNING_NOTES_HEADER_RE = re.compile(
    r"^#\s+\*{0,2}Running Notes\s*/\s*Considerations\*{0,2}\s*$",
    re.MULTILINE,
)

_RUNNING_END_RE = re.compile(r"^%\s*RUNNING\s+END\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^#\s+\*{0,2}[A-Z]", re.MULTILINE)


def build_rewrite_preview(
    tagged_text: str,
    routing_record: dict[str, Any],
    rewrite_map: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Build a preview of the rewritten Running Notes without writing to disk.

    Args:
        tagged_text: The LLM-annotated text with thread tags.
        routing_record: The routing result from ``execute_routing_plan``.
        rewrite_map: Optional explicit mapping of
            ``{thread_id: replacement_text_or_None}``.
            ``None`` means remove the thread entirely.
            A string means replace the thread content with that text.
            If not provided, derived from ``routing_record``.

    Returns:
        Dict with ``rewritten_text``, ``removed_ids``, ``kept_ids``,
        ``summary`` string.
    """
    if rewrite_map is None:
        rewrite_map = _derive_rewrite_map(routing_record)

    lines = tagged_text.split("\n")
    output_lines: list[str] = []
    current_thread: str | None = None
    current_thread_lines: list[str] = []
    removed_ids: list[str] = []
    kept_ids: list[str] = []

    for line in lines:
        stripped = line.strip()
        om = _OPEN_TAG_RE.match(stripped)
        cm = _CLOSE_TAG_RE.match(stripped)
        is_multi = _MULTI_TAG_RE.match(stripped)

        if om:
            current_thread = om.group(1)
            current_thread_lines = []
            continue

        if cm and current_thread:
            tid = current_thread
            current_thread = None

            if tid in rewrite_map:
                replacement = rewrite_map[tid]
                if replacement is None:
                    # Remove this thread entirely
                    removed_ids.append(tid)
                else:
                    # Replace with provided text
                    output_lines.append(replacement)
                    kept_ids.append(tid)
            else:
                # Not in rewrite map → keep as-is
                output_lines.extend(current_thread_lines)
                kept_ids.append(tid)

            current_thread_lines = []
            continue

        if is_multi:
            # Strip multi annotations from output
            continue

        if current_thread is not None:
            current_thread_lines.append(line)
        else:
            # Content outside thread tags (shouldn't happen in valid
            # segmentation, but preserve it)
            output_lines.append(line)

    # Clean up: collapse multiple blank lines, strip trailing whitespace
    rewritten = _clean_output(output_lines)

    summary = (
        f"{len(removed_ids)} threads removed, "
        f"{len(kept_ids)} threads kept"
    )

    return {
        "rewritten_text": rewritten,
        "removed_ids": removed_ids,
        "kept_ids": kept_ids,
        "summary": summary,
    }


@requires_consent(
    operation="journal_backlog_rewrite_notes",
    reason="Rewriting the Running Notes section of the journal file to remove processed items",
    risk="high",
    default_ttl=10,
)
def rewrite_running_notes(
    journal_path: str | Path,
    tagged_text: str,
    routing_record: dict[str, Any],
    original_file_content: str,
    rewrite_map: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Rewrite the Running Notes section, removing processed items.

    Args:
        journal_path: Path to the journal file.
        tagged_text: The LLM-annotated text with thread tags.
        routing_record: The routing result from ``execute_routing_plan``.
        original_file_content: The full journal file content at extraction time
            (used for concurrent modification check).
        rewrite_map: Optional explicit thread → replacement mapping.

    Returns:
        Dict with ``success``, ``file``, ``message``, ``preview``.
    """
    journal_path = Path(journal_path)

    if not journal_path.exists():
        return {
            "success": False,
            "file": journal_path.as_posix(),
            "message": f"Journal file not found: {journal_path}",
            "preview": None,
        }

    # Concurrent modification check
    try:
        current_content = journal_path.read_text(encoding="utf-8")
    except OSError as e:
        return {
            "success": False,
            "file": journal_path.as_posix(),
            "message": f"Could not read journal: {e}",
            "preview": None,
        }

    if current_content != original_file_content:
        return {
            "success": False,
            "file": journal_path.as_posix(),
            "message": (
                "Journal file has been modified since extraction. "
                "The Running Notes section may have changed. "
                "Re-run extraction to get the current content."
            ),
            "preview": None,
        }

    # Locate the section in the full file
    header_match = _RUNNING_NOTES_HEADER_RE.search(current_content)
    if header_match is None:
        return {
            "success": False,
            "file": journal_path.as_posix(),
            "message": "Running Notes section not found in journal.",
            "preview": None,
        }

    body_start = header_match.end()

    # Find section end
    section_end = len(current_content)
    end_marker = _RUNNING_END_RE.search(current_content, body_start)
    if end_marker:
        section_end = end_marker.start()
    else:
        next_heading = _NEXT_HEADING_RE.search(current_content, body_start)
        if next_heading:
            section_end = next_heading.start()

    # Build the rewritten section content
    preview = build_rewrite_preview(tagged_text, routing_record, rewrite_map)
    rewritten_text = preview["rewritten_text"]

    # Reconstruct the full file
    # Keep header + newline, replace body, keep everything after section_end
    new_content = (
        current_content[:body_start]
        + "\n\n"
        + rewritten_text
        + "\n\n"
        + current_content[section_end:]
    )

    try:
        journal_path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return {
            "success": False,
            "file": journal_path.as_posix(),
            "message": f"File write error: {e}",
            "preview": preview,
        }

    logger.info(
        f"Rewrote Running Notes in {journal_path.name}: {preview['summary']}"
    )

    return {
        "success": True,
        "file": journal_path.as_posix(),
        "message": (
            f"Running Notes rewritten: {preview['summary']}. "
            f"File: {journal_path.name}"
        ),
        "preview": preview,
    }


def _derive_rewrite_map(
    routing_record: dict[str, Any],
) -> dict[str, str | None]:
    """Derive a rewrite map from routing record.

    - ``routed`` and ``deleted`` items → ``None`` (remove)
    - ``skipped`` items → not in map (keep as-is)
    - ``split`` items → need ``rewrite_map`` explicitly (warn if missing)
    """
    rewrite_map: dict[str, str | None] = {}

    for item in routing_record.get("routed", []):
        rewrite_map[item["id"]] = None

    for item in routing_record.get("deleted", []):
        rewrite_map[item["id"]] = None

    # Skipped items are intentionally NOT in the map → kept by default

    for item in routing_record.get("split", []):
        # Splits need explicit rewrite_map. If not provided, remove entirely
        # (the agent should have provided rewrite_map for splits)
        logger.warning(
            f"Split item {item['id']} in routing record without explicit "
            f"rewrite_map — removing entirely. Provide rewrite_map for "
            f"partial removal."
        )
        rewrite_map[item["id"]] = None

    return rewrite_map


def _clean_output(lines: list[str]) -> str:
    """Clean up output lines: collapse blanks, strip trailing whitespace."""
    result: list[str] = []
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 1:
                result.append("")
        else:
            blank_count = 0
            result.append(line)

    # Strip trailing blank lines
    while result and result[-1].strip() == "":
        result.pop()

    return "\n".join(result)
