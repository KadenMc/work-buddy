"""Configurable section-aware vault writing.

General-purpose capability for inserting content at a specific location
in a vault note, identified by note path (or resolver) + section header +
position (top/bottom of section).

Note resolvers:
    - ``"latest_journal"`` → most recent daily note (respects day-boundary)
    - ``"today"`` → today's daily note
    - Explicit vault-relative path → used as-is (e.g., ``"journal/2026-04-07.md"``)

Section finding uses header-level boundaries: the section starts after the
matching header line and ends at the next header of equal or higher level,
or at EOF.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.obsidian.retry import bridge_retry
from work_buddy.journal import user_now


# ---------------------------------------------------------------------------
# Note resolvers
# ---------------------------------------------------------------------------

def _resolve_note_path(note: str, vault_root: Path) -> Path | None:
    """Resolve a note specifier to a vault-relative Path.

    Returns None if the note cannot be resolved (e.g., no journal files).
    """
    if note == "latest_journal":
        return _resolve_latest_journal(vault_root)
    elif note == "today":
        date_str = user_now().strftime("%Y-%m-%d")
        return Path("journal") / f"{date_str}.md"
    else:
        # Explicit path — use as-is
        return Path(note)


def _resolve_latest_journal(vault_root: Path) -> Path | None:
    """Find the most recent journal file.

    Respects the day-boundary rule: before ~5 AM, the "latest" journal
    is yesterday's (since the user hasn't started a new day yet).
    """
    journal_dir = vault_root / "journal"
    if not journal_dir.is_dir():
        return None

    # Find all date-named journal files
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
    journal_files = sorted(
        [f.name for f in journal_dir.iterdir()
         if f.is_file() and date_pattern.match(f.name)],
        reverse=True,
    )

    if not journal_files:
        return None

    # Day-boundary: before 5 AM, prefer yesterday's journal if it exists
    now = user_now()
    if 0 <= now.hour < 5 and len(journal_files) >= 2:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d") + ".md"
        if journal_files[0] != yesterday and yesterday in journal_files:
            return Path("journal") / yesterday

    return Path("journal") / journal_files[0]


# ---------------------------------------------------------------------------
# Section finding
# ---------------------------------------------------------------------------

def _strip_formatting(text: str) -> str:
    """Strip Markdown bold/italic markers for comparison."""
    return re.sub(r"\*{1,3}|_{1,3}", "", text).strip()


def _find_section_bounds(
    lines: list[str],
    section: str,
) -> tuple[int, int] | None:
    """Find the line range for a section identified by header text.

    Matches headers like ``## Running Notes``, ``# **Running Notes**``, etc.
    Bold/italic markers are stripped for matching, and the search is
    case-insensitive. A partial match succeeds if the section name appears
    at the start of the header text (so ``"Running Notes"`` matches
    ``"Running Notes / Considerations"``).

    The section body starts on the line after the header and ends at the
    next header of equal or higher level, or at EOF.

    Returns (start_line, end_line) where start_line is the first body line
    and end_line is the line AFTER the last body line (like a slice).
    Returns None if the section is not found.
    """
    section_lower = section.lower()

    for i, line in enumerate(lines):
        header_match = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if not header_match:
            continue

        header_level = len(header_match.group(1))
        header_text = _strip_formatting(header_match.group(2)).lower()

        # Match if section name appears at start of header text
        if not header_text.startswith(section_lower):
            continue

        body_start = i + 1

        # Find section end: next header at same or higher level
        for j in range(body_start, len(lines)):
            other_header = re.match(r"^(#{1,6})\s+", lines[j])
            if other_header and len(other_header.group(1)) <= header_level:
                return (body_start, j)

        # No subsequent header — section extends to EOF
        return (body_start, len(lines))

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@bridge_retry()
def write_at_location(
    content: str,
    note: str = "latest_journal",
    section: str = "Running Notes",
    position: str = "top",
    source: str | None = None,
    vault_root: str | None = None,
) -> dict[str, Any]:
    """Insert content at a specific section in a vault note.

    Args:
        content: Text to insert (one or more lines).
        note: Note path or resolver (``"latest_journal"``, ``"today"``,
            or an explicit vault-relative path).
        section: Header text identifying the target section
            (e.g., ``"Running Notes"``).
        position: ``"top"`` (after header) or ``"bottom"`` (before next section).
        source: Optional source metadata tag appended to content
            (e.g., ``"telegram"`` → adds ``#wb/capture/telegram``).
        vault_root: Override vault root path. Defaults to config value.

    Returns:
        Dict with ``status``, ``note``, ``section``, ``position``, and details.
    """
    if position not in ("top", "bottom"):
        return {"status": "error", "error": f"Invalid position: {position!r}. Use 'top' or 'bottom'."}

    cfg = load_config()
    if vault_root is None:
        vault_root_path = Path(cfg["vault_root"])
    else:
        vault_root_path = Path(vault_root)

    # Resolve note path (always use forward slashes for vault-relative paths)
    note_rel = _resolve_note_path(note, vault_root_path)
    if note_rel is None:
        return {"status": "error", "error": f"Could not resolve note: {note!r}"}
    note_rel_str = str(note_rel).replace("\\", "/")

    note_abs = vault_root_path / note_rel
    if not note_abs.exists():
        return {
            "status": "error",
            "error": f"Note not found: {note_rel_str}",
            "resolved_path": note_rel_str,
        }

    # Read via bridge if available, fall back to direct file read
    file_content = _read_note(note_rel_str, note_abs)
    if file_content is None:
        return {"status": "error", "error": f"Could not read note: {note_rel_str}"}

    lines = file_content.split("\n")

    # Find section
    bounds = _find_section_bounds(lines, section)
    if bounds is None:
        return {
            "status": "error",
            "error": f"Section '{section}' not found in {note_rel_str}",
            "resolved_path": note_rel_str,
        }

    body_start, body_end = bounds

    # Prepare content with optional source tag
    insert_text = content.rstrip("\n")
    if source:
        insert_text += f" #wb/capture/{source}"

    # Insert at position
    if position == "top":
        # Insert after header, before existing body
        # Skip any leading blank line right after the header
        insert_idx = body_start
        insert_lines = [insert_text, ""]
    else:
        # Insert at bottom of section, before the next header
        insert_idx = body_end
        # Add blank line before if section has content
        if body_end > body_start and lines[body_end - 1].strip():
            insert_lines = ["", insert_text]
        else:
            insert_lines = [insert_text]

    new_lines = lines[:insert_idx] + insert_lines + lines[insert_idx:]
    new_content = "\n".join(new_lines)

    # Write via bridge if available, fall back to direct file write
    from work_buddy.consent import ConsentRequired

    try:
        ok = _write_note(note_rel_str, note_abs, new_content)
    except ConsentRequired as exc:
        return {
            "status": "consent_required",
            "operation": exc.operation,
            "reason": exc.reason,
            "risk": exc.risk,
            "default_ttl": exc.default_ttl,
        }

    if not ok:
        return {"status": "error", "error": f"Failed to write note: {note_rel_str}"}

    return {
        "status": "ok",
        "note": note_rel_str,
        "section": section,
        "position": position,
        "content_inserted": insert_text,
        "source": source,
    }


# ---------------------------------------------------------------------------
# File I/O helpers (bridge-first, direct fallback)
# ---------------------------------------------------------------------------

def _read_note(vault_rel_path: str, abs_path: Path) -> str | None:
    """Read a note, preferring Obsidian bridge, falling back to direct read."""
    try:
        from work_buddy.obsidian.bridge import read_file, is_available
        if is_available():
            content = read_file(vault_rel_path)
            if content is not None:
                return content
    except Exception:
        pass

    # Direct fallback
    try:
        return abs_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_note(vault_rel_path: str, abs_path: Path, content: str) -> bool:
    """Write a note, preferring Obsidian bridge, falling back to direct write.

    Re-raises ConsentRequired — that's a hard stop, not a fallback case.
    """
    import logging
    log = logging.getLogger(__name__)
    from work_buddy.consent import ConsentRequired

    # Normalize path separators — bridge expects forward slashes
    vault_rel_path = vault_rel_path.replace("\\", "/")

    try:
        from work_buddy.obsidian.bridge import write_file_raw, is_available
        if is_available():
            log.info("Writing via bridge: %s", vault_rel_path)
            result = write_file_raw(vault_rel_path, content)
            log.info("Bridge write result: %s", result)
            return result
        else:
            log.info("Bridge not available, falling back to direct write")
    except ConsentRequired:
        raise  # Caller must handle consent flow
    except Exception as exc:
        log.warning("Bridge write failed: %s: %s", type(exc).__name__, exc)

    # Direct fallback (atomic)
    try:
        log.info("Direct write: %s", abs_path)
        tmp = abs_path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(abs_path)
        return True
    except OSError as exc:
        log.error("Direct write failed: %s", exc)
        return False
