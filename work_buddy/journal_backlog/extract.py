"""Running Notes section extraction from journal files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from work_buddy.journal_backlog.segment import strip_banners
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Section header: # **Running Notes / Considerations**
_RUNNING_NOTES_HEADER_RE = re.compile(
    r"^#\s+\*{0,2}Running Notes\s*/\s*Considerations\*{0,2}\s*$",
    re.MULTILINE,
)

# End marker placed by Templater
_RUNNING_END_RE = re.compile(r"^%\s*RUNNING\s+END\s*$", re.MULTILINE)

# Next top-level heading (signals end of section if no RUNNING END marker)
_NEXT_HEADING_RE = re.compile(r"^#\s+\*{0,2}[A-Z]", re.MULTILINE)


def extract_running_notes(
    journal_path: str | Path,
) -> dict[str, Any]:
    """Extract the Running Notes section from a journal file.

    Args:
        journal_path: Path to a daily journal ``.md`` file.

    Returns:
        Dict with keys:
        - ``success`` -- bool
        - ``raw_text`` -- section content including banners (or None)
        - ``clean_text`` -- banner-stripped text (or None)
        - ``source_dates`` -- list of YYYY-MM-DD from carried-over banners
        - ``banner_date_map`` -- list of (line_number, date) for thread attribution
        - ``full_file_content`` -- entire journal file (needed by rewrite phase)
        - ``section_start`` -- char offset of section header
        - ``section_end`` -- char offset of section end
        - ``line_count`` -- non-empty content lines (excluding banners)
        - ``journal_date`` -- date string from filename stem
        - ``message`` -- status or error string
    """
    journal_path = Path(journal_path)

    if not journal_path.exists():
        return _fail(f"Journal file not found: {journal_path}")

    try:
        content = journal_path.read_text(encoding="utf-8")
    except OSError as e:
        return _fail(f"Could not read journal file: {e}")

    journal_date = journal_path.stem  # e.g. "2026-04-02"

    # Find the section header
    header_match = _RUNNING_NOTES_HEADER_RE.search(content)
    if header_match is None:
        return _fail(
            "No 'Running Notes / Considerations' section found",
            full_file_content=content,
            journal_date=journal_date,
        )

    section_start = header_match.start()
    body_start = header_match.end()

    # Find the section end: RUNNING END marker > next heading > EOF
    section_end = len(content)

    end_marker = _RUNNING_END_RE.search(content, body_start)
    if end_marker:
        section_end = end_marker.start()
    else:
        next_heading = _NEXT_HEADING_RE.search(content, body_start)
        if next_heading:
            section_end = next_heading.start()

    raw_text = content[body_start:section_end].strip()

    if not raw_text:
        return {
            "success": True,
            "raw_text": "",
            "clean_text": "",
            "source_dates": [],
            "banner_date_map": [],
            "full_file_content": content,
            "section_start": section_start,
            "section_end": section_end,
            "line_count": 0,
            "journal_date": journal_date,
            "message": "Running Notes section is empty.",
        }

    clean_text, source_dates, banner_date_map = strip_banners(raw_text)

    # Count non-empty content lines in cleaned text
    line_count = sum(
        1
        for line in clean_text.split("\n")
        if line.strip()
    )

    logger.info(
        f"Extracted Running Notes from {journal_date}: "
        f"{line_count} content lines, {len(source_dates)} carried-over dates"
    )

    return {
        "success": True,
        "raw_text": raw_text,
        "clean_text": clean_text,
        "source_dates": source_dates,
        "banner_date_map": banner_date_map,
        "full_file_content": content,
        "section_start": section_start,
        "section_end": section_end,
        "line_count": line_count,
        "journal_date": journal_date,
        "message": f"Extracted {line_count} content lines from Running Notes.",
    }


def read_running_notes(
    *,
    same_day: bool = False,
    days: int | None = None,
    start: str | None = None,
    stop: str | None = None,
    journal_date: str | None = None,
) -> str:
    """Get Running Notes with day-level filtering.

    The Running Notes section contains today's notes (no banner) followed by
    carried-over sections, each prefixed with a date banner. This function
    extracts the full section then slices by day.

    Filtering modes (mutually exclusive):
    - ``same_day=True``: Only notes from the journal's own date (above the first carried-over banner).
    - ``days=N``: The N most recent days (today counts as day 1).
    - ``start``/``stop``: Date range filter (inclusive, 'YYYY-MM-DD').
      Omit ``start`` to include everything up to ``stop``.
      Omit ``stop`` to include everything from ``start`` onward.
    - No filter: returns the entire section.

    ``days`` cannot be combined with ``start`` or ``stop``.

    Args:
        same_day: If True, return only the journal date's own notes (equivalent to days=1).
        days: Return the N most recent days of notes.
        start: Include notes from this date onward (inclusive).
        stop: Include notes up to this date (inclusive).
        journal_date: Journal file date as 'YYYY-MM-DD'. Defaults to today.

    Returns:
        The filtered Running Notes text as a string. Empty string if
        the section doesn't exist or has no content.
    """
    if same_day:
        days = 1
    if days is not None and (start is not None or stop is not None):
        raise ValueError("Cannot combine 'days' with 'start'/'stop'")

    from work_buddy.journal import journal_path_for_date
    path = journal_path_for_date(journal_date)
    result = extract_running_notes(path)

    if not result["success"] or not result["raw_text"]:
        return ""

    # Split the raw text into day segments using banner dates
    segments = _split_by_day(result["raw_text"], result["journal_date"])

    # Apply filter
    if days is not None:
        segments = segments[:days]
    elif start is not None or stop is not None:
        filtered = []
        for seg in segments:
            if start and seg["date"] < start:
                continue
            if stop and seg["date"] > stop:
                continue
            filtered.append(seg)
        segments = filtered

    return "\n\n".join(seg["text"] for seg in segments if seg["text"].strip())


def _split_by_day(raw_text: str, journal_date: str) -> list[dict[str, str]]:
    """Split Running Notes into per-day segments.

    Returns list of {date, text} dicts, newest first (today first).
    """
    from work_buddy.journal_backlog.segment import _BANNER_RE

    lines = raw_text.split("\n")
    segments: list[dict[str, str]] = []
    current_date = journal_date
    current_lines: list[str] = []

    for line in lines:
        banner_match = _BANNER_RE.match(line)
        if banner_match:
            # Save the current segment
            segments.append({
                "date": current_date,
                "text": "\n".join(current_lines).strip(),
            })
            current_date = banner_match.group(1)
            current_lines = []
        else:
            current_lines.append(line)

    # Save the last segment
    segments.append({
        "date": current_date,
        "text": "\n".join(current_lines).strip(),
    })

    return segments


def _fail(
    message: str,
    full_file_content: str | None = None,
    journal_date: str | None = None,
) -> dict[str, Any]:
    """Return a failure result dict."""
    logger.warning(f"extract_running_notes failed: {message}")
    return {
        "success": False,
        "raw_text": None,
        "clean_text": None,
        "source_dates": [],
        "banner_date_map": [],
        "full_file_content": full_file_content,
        "section_start": None,
        "section_end": None,
        "line_count": 0,
        "journal_date": journal_date,
        "message": message,
    }
