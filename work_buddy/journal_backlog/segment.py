"""Thread segmentation utilities for journal backlog processing.

Provides ID generation, banner stripping, segmentation validation,
thread extraction, and manifest handling for LLM-tagged Running Notes.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Banner pattern: ***'Running Notes / Considerations' carried over from YYYY-MM-DD***
_BANNER_RE = re.compile(
    r"^\*{3}'Running Notes\s*/\s*Considerations'\s*carried over from\s+"
    r"(\d{4}-\d{2}-\d{2})\*{3}\s*$",
    re.MULTILINE,
)

# Thread tag patterns
_OPEN_TAG_RE = re.compile(r"^<!--\s*\[(t_[a-f0-9]{6})\]\s*-->\s*$")
_CLOSE_TAG_RE = re.compile(r"^<!--\s*\[/(t_[a-f0-9]{6})\]\s*-->\s*$")
_MULTI_TAG_RE = re.compile(r"^<!--\s*\[multi\]\s*-->\s*$")
_THREAD_ID_RE = re.compile(r"^t_[a-f0-9]{6}$")

# Structural separators (banner boundaries, not user content)
_SEPARATOR_RE = re.compile(r"^-{3,}\s*$")


def generate_thread_ids(count: int = 50) -> list[str]:
    """Generate a pool of unique thread IDs.

    Args:
        count: Number of IDs to generate.

    Returns:
        Sorted list of unique IDs in format ``t_`` + 6 hex chars.
    """
    ids: set[str] = set()
    while len(ids) < count:
        ids.add(f"t_{uuid.uuid4().hex[:6]}")
    return sorted(ids)


def strip_banners(text: str) -> tuple[str, list[str], list[tuple[int, str]]]:
    """Remove carried-over banners from Running Notes text.

    Args:
        text: Raw Running Notes section content.

    Returns:
        Tuple of:
        - Cleaned text with banners and structural separators removed.
        - List of source date strings extracted from banners.
        - Banner date map: list of (line_number, date_str) tuples for
          attributing threads to source dates.
    """
    source_dates: list[str] = []
    banner_date_map: list[tuple[int, str]] = []
    lines = text.split("\n")
    cleaned_lines: list[str] = []
    current_date: str | None = None

    # Track whether we're in a "banner zone" (banner + surrounding separators)
    prev_was_banner_or_sep = False

    for i, line in enumerate(lines):
        banner_match = _BANNER_RE.match(line)
        if banner_match:
            date_str = banner_match.group(1)
            source_dates.append(date_str)
            current_date = date_str
            banner_date_map.append((len(cleaned_lines), date_str))
            prev_was_banner_or_sep = True
            continue

        if _SEPARATOR_RE.match(line):
            # Only strip separators adjacent to banners
            if prev_was_banner_or_sep:
                continue
            # Check if next non-empty line is a banner
            next_content = _peek_next_content(lines, i + 1)
            if next_content is not None and _BANNER_RE.match(next_content):
                prev_was_banner_or_sep = True
                continue

        prev_was_banner_or_sep = False
        cleaned_lines.append(line)

    # Collapse multiple consecutive blank lines to at most one
    result_lines: list[str] = []
    blank_count = 0
    for line in cleaned_lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 1:
                result_lines.append(line)
        else:
            blank_count = 0
            result_lines.append(line)

    # Strip trailing whitespace
    while result_lines and result_lines[-1].strip() == "":
        result_lines.pop()

    cleaned = "\n".join(result_lines)
    return cleaned, source_dates, banner_date_map


def _peek_next_content(lines: list[str], start: int) -> str | None:
    """Find the next non-empty line starting from index ``start``."""
    for i in range(start, len(lines)):
        if lines[i].strip():
            return lines[i]
    return None


def validate_segmentation(
    tagged_text: str, original_text: str
) -> dict[str, Any]:
    """Validate that LLM-produced segmentation is complete and consistent.

    Checks:
    1. Balanced open/close tags
    2. No nested threads
    3. Content preservation (no lines added, removed, or modified)
    4. Complete coverage (no orphaned content)
    5. ID format compliance

    Args:
        tagged_text: The LLM-annotated text with thread tags.
        original_text: The banner-stripped original text (from strip_banners).

    Returns:
        Dict with ``valid``, ``thread_count``, ``thread_ids``, ``errors``,
        ``warnings`` keys.
    """
    errors: list[str] = []
    warnings: list[str] = []

    tagged_lines = tagged_text.split("\n")

    # Collect open and close tag IDs
    open_ids: list[str] = []
    close_ids: list[str] = []

    for line in tagged_lines:
        om = _OPEN_TAG_RE.match(line.strip())
        if om:
            open_ids.append(om.group(1))
        cm = _CLOSE_TAG_RE.match(line.strip())
        if cm:
            close_ids.append(cm.group(1))

    open_set = set(open_ids)
    close_set = set(close_ids)

    # --- Check 1: balanced tags ---
    unmatched_open = open_set - close_set
    unmatched_close = close_set - open_set
    if unmatched_open:
        errors.append(f"Open tags without close: {sorted(unmatched_open)}")
    if unmatched_close:
        errors.append(f"Close tags without open: {sorted(unmatched_close)}")

    # Duplicate open tags
    if len(open_ids) != len(open_set):
        seen: set[str] = set()
        dupes: set[str] = set()
        for tid in open_ids:
            if tid in seen:
                dupes.add(tid)
            seen.add(tid)
        errors.append(f"Duplicate open tags: {sorted(dupes)}")

    # --- Check 2: no nesting ---
    current_thread: str | None = None
    for line_num, line in enumerate(tagged_lines, 1):
        stripped = line.strip()
        om = _OPEN_TAG_RE.match(stripped)
        cm = _CLOSE_TAG_RE.match(stripped)

        if om:
            if current_thread is not None:
                errors.append(
                    f"Nested thread: {om.group(1)} opened inside "
                    f"{current_thread} at line {line_num}"
                )
            current_thread = om.group(1)
        elif cm:
            if current_thread is None:
                errors.append(
                    f"Close tag {cm.group(1)} without open at line {line_num}"
                )
            elif cm.group(1) != current_thread:
                errors.append(
                    f"Mismatched close: expected {current_thread}, "
                    f"got {cm.group(1)} at line {line_num}"
                )
            current_thread = None

    if current_thread is not None:
        errors.append(f"Thread {current_thread} still open at end of text")

    # --- Check 3: content preservation ---
    original_content = _content_lines(original_text)
    tagged_content = _content_lines(
        _strip_all_tags(tagged_text)
    )

    if len(original_content) != len(tagged_content):
        errors.append(
            f"Content line count mismatch: "
            f"{len(original_content)} original vs {len(tagged_content)} tagged"
        )
    else:
        for i, (orig, tagged) in enumerate(
            zip(original_content, tagged_content)
        ):
            if orig != tagged:
                errors.append(
                    f"Content modified at line {i + 1}: "
                    f"{orig[:60]!r} -> {tagged[:60]!r}"
                )
                if len(errors) > 10:
                    errors.append("(further content differences truncated)")
                    break

    # --- Check 4: complete coverage ---
    current_thread = None
    for line_num, line in enumerate(tagged_lines, 1):
        stripped = line.strip()
        om = _OPEN_TAG_RE.match(stripped)
        cm = _CLOSE_TAG_RE.match(stripped)
        is_multi = _MULTI_TAG_RE.match(stripped)
        is_tag = om or cm or is_multi

        if om:
            current_thread = om.group(1)
            continue
        if cm:
            current_thread = None
            continue
        if is_multi:
            continue

        # Non-tag, non-empty line outside a thread
        if not is_tag and stripped and current_thread is None:
            # Allow blank lines and separators outside threads
            if not _SEPARATOR_RE.match(stripped):
                errors.append(
                    f"Orphaned content at line {line_num}: {stripped[:60]!r}"
                )

    # --- Check 5: ID format ---
    all_ids = open_set | close_set
    for tid in all_ids:
        if not _THREAD_ID_RE.match(tid):
            errors.append(f"Invalid thread ID format: {tid!r}")

    thread_ids = sorted(open_set & close_set)
    return {
        "valid": len(errors) == 0,
        "thread_count": len(thread_ids),
        "thread_ids": thread_ids,
        "errors": errors,
        "warnings": warnings,
    }


def extract_threads(
    tagged_text: str,
    banner_date_map: list[tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Extract thread objects from validated tagged text.

    Args:
        tagged_text: LLM-annotated text that passed validation.
        banner_date_map: Optional list of (line_number, date_str) tuples
            from ``strip_banners()`` for source date attribution.

    Returns:
        List of thread dicts, each with ``id``, ``raw_text``,
        ``line_count``, ``source_dates``, ``has_multi_flag``.
    """
    threads: dict[str, dict[str, Any]] = {}
    current_thread: str | None = None
    current_lines: list[str] = []
    current_start_line: int = 0
    has_multi = False

    lines = tagged_text.split("\n")
    content_line_num = 0  # tracks position in banner-stripped space

    for line in lines:
        stripped = line.strip()
        om = _OPEN_TAG_RE.match(stripped)
        cm = _CLOSE_TAG_RE.match(stripped)
        is_multi = _MULTI_TAG_RE.match(stripped)

        if om:
            current_thread = om.group(1)
            current_lines = []
            current_start_line = content_line_num
            has_multi = False
            continue

        if cm and current_thread:
            raw = "\n".join(current_lines)
            non_empty = [l for l in current_lines if l.strip()]
            source_dates = _attribute_dates(
                current_start_line, content_line_num, banner_date_map
            )
            threads[current_thread] = {
                "id": current_thread,
                "raw_text": raw,
                "line_count": len(non_empty),
                "source_dates": source_dates,
                "has_multi_flag": has_multi,
            }
            current_thread = None
            current_lines = []
            has_multi = False
            continue

        if is_multi:
            has_multi = True
            continue

        if current_thread is not None:
            current_lines.append(line)

        # Count content lines for banner date mapping
        if stripped and not om and not cm and not is_multi:
            content_line_num += 1

    result = list(threads.values())
    logger.info(f"Extracted {len(result)} threads from tagged text")
    return result


def _attribute_dates(
    start_line: int,
    end_line: int,
    banner_date_map: list[tuple[int, str]] | None,
) -> list[str]:
    """Determine source dates for a thread based on banner positions."""
    if not banner_date_map:
        return []

    dates: list[str] = []
    for banner_line, date_str in banner_date_map:
        if banner_line <= end_line:
            if date_str not in dates:
                dates.append(date_str)

    return dates


def _content_lines(text: str) -> list[str]:
    """Extract non-empty, non-separator lines for comparison."""
    result = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not _SEPARATOR_RE.match(stripped):
            result.append(stripped)
    return result


def _strip_all_tags(text: str) -> str:
    """Remove all thread tags and multi annotations from text."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if _OPEN_TAG_RE.match(stripped):
            continue
        if _CLOSE_TAG_RE.match(stripped):
            continue
        if _MULTI_TAG_RE.match(stripped):
            continue
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manifest (JSONL) — structured metadata per thread
# ---------------------------------------------------------------------------


def validate_manifest(
    manifest_path: Path,
    thread_ids: list[str],
) -> dict[str, Any]:
    """Validate a JSONL manifest against extracted thread IDs.

    Each line must be valid JSON with at least ``id``, ``tags``, ``summary``.
    Every thread ID from the tagged text must have exactly one manifest entry.

    Args:
        manifest_path: Path to the ``.jsonl`` manifest file.
        thread_ids: Thread IDs from ``validate_segmentation`` or
            ``extract_threads``.

    Returns:
        Dict with ``valid``, ``entries`` (parsed list), ``errors``.
    """
    errors: list[str] = []
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as e:
        return {"valid": False, "entries": [], "errors": [f"Read error: {e}"]}

    for line_num, line in enumerate(text.strip().split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"Line {line_num}: invalid JSON — {e}")
            continue

        tid = entry.get("id")
        if not tid:
            errors.append(f"Line {line_num}: missing 'id' field")
            continue
        if tid in seen_ids:
            errors.append(f"Line {line_num}: duplicate id {tid!r}")
        seen_ids.add(tid)

        if "tags" not in entry:
            errors.append(f"Line {line_num} ({tid}): missing 'tags'")
        elif not isinstance(entry["tags"], list):
            errors.append(f"Line {line_num} ({tid}): 'tags' must be a list")

        if "summary" not in entry:
            errors.append(f"Line {line_num} ({tid}): missing 'summary'")

        entries.append(entry)

    # Cross-check against thread IDs
    expected = set(thread_ids)
    missing = expected - seen_ids
    extra = seen_ids - expected
    if missing:
        errors.append(f"Threads missing from manifest: {sorted(missing)}")
    if extra:
        errors.append(f"Manifest has unknown thread IDs: {sorted(extra)}")

    return {
        "valid": len(errors) == 0,
        "entries": entries,
        "errors": errors,
    }


def load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    """Load a JSONL manifest file, returning a list of entry dicts."""
    entries = []
    for line in manifest_path.read_text(encoding="utf-8").strip().split("\n"):
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def generate_review_doc(
    threads: list[dict[str, Any]],
    manifest_entries: list[dict[str, Any]],
    journal_date: str,
    source_dates: list[str],
) -> str:
    """Generate a markdown review document from threads and manifest.

    Groups threads by their tags (from manifest) rather than hardcoded
    categories.

    Args:
        threads: Thread objects from ``extract_threads()``.
        manifest_entries: Parsed manifest entries from ``load_manifest()``.
        journal_date: The source journal date (YYYY-MM-DD).
        source_dates: List of carried-over dates.

    Returns:
        Markdown string for the review document.
    """
    thread_map = {t["id"]: t for t in threads}
    manifest_map = {e["id"]: e for e in manifest_entries}

    # Group by primary tag (first tag in the list)
    groups: dict[str, list[str]] = {}
    for entry in manifest_entries:
        primary = entry["tags"][0] if entry.get("tags") else "#untagged"
        groups.setdefault(primary, []).append(entry["id"])

    lines = [
        "# Segmentation Review",
        "",
        f"**Source:** `journal/{journal_date}.md` Running Notes",
        f"**Threads:** {len(threads)}",
        f"**Carried-over dates:** {', '.join(source_dates[:5])}"
        + (f"... ({len(source_dates)} total)" if len(source_dates) > 5 else ""),
        "",
        "**Instructions:** Scan each thread. Mark any that should be:",
        "- **MERGE** with another thread (note both IDs)",
        "- **SPLIT** into multiple threads",
        "- Looks fine as-is? No action needed.",
        "",
    ]

    for tag, tids in groups.items():
        lines.append("---")
        lines.append(f"## {tag}")
        lines.append("")
        for tid in tids:
            t = thread_map.get(tid)
            m = manifest_map.get(tid)
            if not t or not m:
                continue

            extra_tags = " ".join(m["tags"][1:]) if len(m.get("tags", [])) > 1 else ""
            multi = " `[MULTI]`" if m.get("multi") else ""
            lines.append(f"### `{tid}`{multi} ({t['line_count']} lines)")
            if extra_tags:
                lines.append(f"Tags: {extra_tags}")
            lines.append(f"> {m['summary']}")
            lines.append("")

            content = t["raw_text"].strip()
            content_lines = content.split("\n")
            lines.append("```")
            if len(content_lines) > 15:
                lines.extend(content_lines[:10])
                lines.append(f"... ({len(content_lines)} lines total)")
            else:
                lines.append(content)
            lines.append("```")
            lines.append("")

    return "\n".join(lines)
