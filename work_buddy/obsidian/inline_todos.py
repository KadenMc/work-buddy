"""Discover and manage #wb/TODO inline markers across the Obsidian vault.

Users leave ``#wb/TODO`` tags in any note as instructions for work-buddy:

    * 7:20 AM - Working from home. #wb/TODO describe what I was doing, please!

This module provides:
- ``discover_inline_todos()`` — find all #wb/TODO lines vault-wide
- ``cleanup_handled_todos()`` — replace handled tags with #wb/DONE
"""

import re
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.obsidian.tags import search_by_tag, get_file_tags
from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_TODO_TAG = "#wb/TODO"
_DONE_TAG = "#wb/DONE"

# Matches #wb/TODO optionally followed by colon/comma and the instruction text.
_TODO_RE = re.compile(r"#wb/TODO\b\s*[,:]?\s*")


def _parse_instruction(line: str) -> str:
    """Extract the instruction text after #wb/TODO on a line.

    Handles: ``#wb/TODO describe...``, ``#wb/TODO: remind...``,
    ``#wb/TODO, create...``, and bare ``#wb/TODO`` (returns "").
    """
    m = _TODO_RE.search(line)
    if not m:
        return ""
    after = line[m.end():].strip()
    # Strip trailing tags that aren't part of the instruction
    # e.g., "#wb/TODO do something #wb/journal/log" — keep "do something"
    # but don't strip if the entire remainder is the instruction
    return after


def _context_lines(lines: list[str], idx: int, window: int = 2) -> tuple[str, str]:
    """Get context lines before and after a given line index."""
    start = max(0, idx - window)
    end = min(len(lines), idx + window + 1)
    before = "\n".join(lines[start:idx])
    after = "\n".join(lines[idx + 1:end])
    return before, after


def discover_inline_todos(limit: int = 100) -> dict[str, Any]:
    """Find all #wb/TODO markers across the vault.

    Uses the tag search integration (metadataCache via eval_js) to find
    files, then reads each file to extract the full line and instruction.

    Args:
        limit: Maximum number of files to scan.

    Returns a dict with:
    - count: int — total TODO items found
    - items: list of dicts, each with:
      - file_path: str — vault-relative path
      - line_number: int — 0-indexed line number
      - full_line: str — the entire line text
      - instruction: str — text after #wb/TODO
      - context_before: str — 2 lines before
      - context_after: str — 2 lines after
    """
    bridge.require_available()

    # Step 1: Find files containing #wb/TODO
    search_result = search_by_tag(_TODO_TAG, mode="exact", limit=limit)
    files = search_result.get("files", [])

    if not files:
        return {"count": 0, "items": []}

    # Filter out work-buddy repo files (workflow docs mention #wb/TODO as literal text)
    _EXCLUDE_PREFIXES = ("repos/work-buddy/",)
    files = [f for f in files if not any(f["path"].startswith(p) for p in _EXCLUDE_PREFIXES)]

    if not files:
        return {"count": 0, "items": []}

    items = []

    for file_info in files:
        path = file_info["path"]

        # Step 2: Get line-level tag positions
        try:
            file_tags = get_file_tags(path)
        except RuntimeError:
            logger.warning("Could not get tags for %s, skipping", path)
            continue

        # Find which lines have #wb/TODO
        todo_lines = set()
        for tag_entry in file_tags.get("tags", []):
            if tag_entry["tag"].lower() == _TODO_TAG.lower() and tag_entry["line"] is not None:
                todo_lines.add(tag_entry["line"])

        if not todo_lines:
            # Tag might be in frontmatter — skip (can't act on frontmatter tags)
            continue

        # Step 3: Read file content
        content = bridge.read_file(path)
        if content is None:
            logger.warning("Could not read %s, skipping", path)
            continue

        lines = content.split("\n")

        for line_num in sorted(todo_lines):
            if line_num >= len(lines):
                continue

            full_line = lines[line_num]
            # Double-check the line actually contains #wb/TODO
            if _TODO_TAG.lower() not in full_line.lower():
                continue

            instruction = _parse_instruction(full_line)
            ctx_before, ctx_after = _context_lines(lines, line_num)

            items.append({
                "file_path": path,
                "line_number": line_num,
                "full_line": full_line,
                "instruction": instruction,
                "context_before": ctx_before,
                "context_after": ctx_after,
            })

    return {"count": len(items), "items": items}


@requires_consent(
    operation="inline_todos.cleanup",
    reason="Replace #wb/TODO tags with #wb/DONE in vault files (modifies file contents for handled items)",
    risk="moderate",
    default_ttl=15,
)
def cleanup_handled_todos(
    handled_items: list[dict],
    replacement: str = _DONE_TAG,
) -> dict[str, Any]:
    """Replace #wb/TODO with #wb/DONE for handled items.

    Groups edits by file for efficiency. Validates each line still matches
    before modifying (guards against mid-workflow edits).

    Args:
        handled_items: List of item dicts from discover_inline_todos().
            Each must have file_path, line_number, full_line.
        replacement: Tag to replace #wb/TODO with. Default "#wb/DONE".
            Pass "" to strip the tag entirely.

    Returns a dict with:
    - cleaned: int — items successfully cleaned
    - skipped: int — items skipped due to mismatch
    - errors: list[str] — descriptions of any issues
    - files_modified: list[str] — paths of files that were written
    """
    bridge.require_available()

    # Group items by file
    by_file: dict[str, list[dict]] = {}
    for item in handled_items:
        by_file.setdefault(item["file_path"], []).append(item)

    cleaned = 0
    skipped = 0
    errors = []
    files_modified = []

    for path, file_items in by_file.items():
        content = bridge.read_file(path)
        if content is None:
            errors.append(f"Could not read {path}")
            skipped += len(file_items)
            continue

        lines = content.split("\n")
        modified = False

        for item in sorted(file_items, key=lambda x: x["line_number"], reverse=True):
            ln = item["line_number"]
            if ln >= len(lines):
                errors.append(f"{path}:{ln} — line number out of range")
                skipped += 1
                continue

            current_line = lines[ln]
            # Validate the line still contains #wb/TODO
            if _TODO_TAG.lower() not in current_line.lower():
                errors.append(f"{path}:{ln} — line no longer contains {_TODO_TAG}")
                skipped += 1
                continue

            # Replace the tag
            new_line = re.sub(
                r"#wb/TODO\b",
                replacement,
                current_line,
                flags=re.IGNORECASE,
            )
            # Clean up double spaces if tag was stripped
            if not replacement:
                new_line = re.sub(r"  +", " ", new_line).rstrip()

            lines[ln] = new_line
            modified = True
            cleaned += 1

        if modified:
            new_content = "\n".join(lines)
            success = bridge.write_file(path, new_content)
            if success:
                files_modified.append(path)
            else:
                errors.append(f"Failed to write {path}")

    return {
        "cleaned": cleaned,
        "skipped": skipped,
        "errors": errors,
        "files_modified": files_modified,
    }
