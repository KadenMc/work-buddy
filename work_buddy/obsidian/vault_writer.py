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

    # Write via bridge if available, fall back to direct file write.
    # ConsentRequired propagates to the gateway's consent flow handler.
    # ObsidianError subclasses propagate to the gateway exception path
    # (where post-write-verify catches ObsidianPostWriteUncertain via
    # the insert_text witness, and other types classify normally).
    from work_buddy.consent import ConsentRequired

    try:
        ok = vault_write(
            note_rel_str, note_abs, new_content,
            # Section-aware insert: hint is the inserted text itself, so
            # post-write-verify's substring search will match if the write
            # actually landed even if the bridge timed out client-side.
            write_mode="insert",
            content_hint=insert_text,
        )
    except ConsentRequired as exc:
        return {
            "status": "consent_required",
            "operation": exc.operation,
            "reason": exc.reason,
            "risk": exc.risk,
            "default_ttl": exc.default_ttl,
        }

    if not ok:
        # vault_write returns False only for direct-filesystem-fallback OSError
        # (post-CP6). All other failure modes raise typed exceptions that
        # propagate to the gateway. So this path is exceptional and rare.
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
        from work_buddy.obsidian.bridge import read_file_raw, is_available
        if is_available():
            # read_file_raw raises a typed ObsidianError on a transient — it is
            # intentionally NOT caught here, so the write is retried rather than
            # computing the insertion position from a possibly-stale on-disk
            # copy. A genuine 404 → None falls through to the direct read.
            content = read_file_raw(vault_rel_path)
            if content is not None:
                return content
    except ImportError:
        pass  # bridge module unavailable — fall back to the direct read

    # Direct fallback — used when the bridge app/module is unavailable, or for
    # a genuinely-absent file (404).
    try:
        return abs_path.read_text(encoding="utf-8")
    except OSError:
        return None


def vault_write(
    vault_rel_path: str,
    abs_path: Path,
    content: str,
    *,
    write_mode: str = "replace",
    content_hint: str | None = None,
) -> bool:
    """Write a note, preferring Obsidian bridge, falling back to direct write.

    The "safe" vault write entry point for callers that don't own the file
    via a plugin (journals, knowledge units, capture, generic content).

    For task lines, contracts, and anything the Obsidian Tasks plugin owns
    state for, use ``bridge.write_file_raw`` directly — the direct-write
    fallback here would skip the plugin's mutation pipeline and corrupt
    plugin-owned state (recurrence, done-dates, checkbox transitions). See
    the ``obsidian/vault-write-decision`` knowledge unit for the picking
    rule.

    Fallback safety predicate
    -------------------------
    A direct filesystem write is only safe when the Obsidian **process is
    genuinely down** (``bridge.is_obsidian_running()`` is False) — then no
    editor can be holding the note. If Obsidian is running, an open editor's
    in-memory buffer would diverge from the freshly-written disk content (the
    bridge's ``syncOpenEditorsToDisk`` only fires on writes *through* the
    plugin), wedging the note with a persistent ``409 editor_dirty`` on every
    later bridge write. So a transient bridge failure while Obsidian is up
    must NOT direct-write — it re-raises so the gateway / retry queue replays.

    The bridge layer raises typed exceptions per the
    ``work_buddy.obsidian.errors`` hierarchy. Different failure types
    take different recovery paths:

      ObsidianNotRunning
          Obsidian process is down — no editor can hold the note, so a
          direct filesystem write cannot diverge an open editor. FALL BACK.
      ObsidianUnreachable (other subclasses: startup race, plugin
      missing / disabled)
          Obsidian IS running but the bridge is transiently/structurally
          unreachable. An editor may hold the note. RE-RAISE — direct-writing
          would diverge it. The retry queue replays once the bridge recovers.
      ObsidianPostWriteUncertain
          Body MAY have been sent and the plugin MAY have committed.
          Filesystem fallback would overwrite the plugin's write if
          it landed. RE-RAISE so the gateway's verify path runs.
      ObsidianEditorConflict
          User has unsaved typing. Filesystem write would clobber it.
          RE-RAISE — the retry queue handles re-attempt later.
      ObsidianRefused
          Structural refusal (4xx). No retry will help. RE-RAISE.
      ObsidianServerError
          5xx — plugin-side fault. Filesystem fallback would bypass
          the plugin's state machine, risking cache divergence.
          RE-RAISE so the retry queue waits for the plugin to recover.
      ConsentRequired
          Consent gate. RE-RAISE — caller handles the consent flow.

    Returns True on success (bridge or fallback). False only when the
    direct filesystem write itself fails (OSError on the tmp.write).
    """
    import logging
    log = logging.getLogger(__name__)
    from work_buddy.consent import ConsentRequired
    from work_buddy.obsidian.errors import (
        ObsidianEditorConflict,
        ObsidianNotRunning,
        ObsidianPostWriteUncertain,
        ObsidianRefused,
        ObsidianServerError,
        ObsidianStartupRace,
        ObsidianUnreachable,
    )

    # Normalize path separators — bridge expects forward slashes
    vault_rel_path = vault_rel_path.replace("\\", "/")

    try:
        from work_buddy.obsidian.bridge import (
            is_available,
            is_obsidian_running,
            write_file_raw,
        )
        if is_available():
            log.info("Writing via bridge: %s", vault_rel_path)
            result = write_file_raw(
                vault_rel_path, content,
                write_mode=write_mode, content_hint=content_hint,
            )
            log.info("Bridge write result: %s", result)
            return result
        else:
            log.info("Bridge not available; deciding fallback by process state")
    except ConsentRequired:
        raise
    except ObsidianEditorConflict:
        raise  # Don't fall back — would clobber the user's typing.
    except ObsidianPostWriteUncertain:
        raise  # Don't fall back — gateway's verify path runs first.
    except ObsidianRefused:
        raise  # Structural refusal — no point falling back.
    except ObsidianServerError:
        raise  # Plugin-side fault — filesystem would bypass plugin state.
    except ObsidianNotRunning:
        # Obsidian process is genuinely down — no editor can be holding the
        # note, so a direct filesystem write cannot diverge an open editor.
        # Fall through to the safe direct write below.
        log.warning(
            "Obsidian not running; falling back to direct filesystem write: %s",
            vault_rel_path,
        )
    except ObsidianUnreachable as exc:
        # Obsidian IS running but the bridge is transiently unreachable
        # (startup race / port not yet bound, plugin missing or disabled).
        # An editor may be holding this note: a direct filesystem write would
        # diverge its buffer from disk and wedge the bridge with a persistent
        # 409 editor_dirty. Re-raise (transient) so the gateway / retry queue
        # replays once the bridge recovers — do NOT direct-write.
        log.warning(
            "Bridge unreachable but Obsidian is running (%s); re-raising "
            "instead of direct write to avoid diverging an open editor: %s",
            exc.error_kind, vault_rel_path,
        )
        raise

    # Reached only when is_available() returned False, OR ObsidianNotRunning
    # was caught above. Guard the is_available()==False-but-process-up case:
    # a startup race / port flap reports unavailable even though Obsidian is
    # running with the note open, and direct-writing there would diverge the
    # open editor. Only the genuine process-down case is safe to direct-write.
    if is_obsidian_running():
        raise ObsidianStartupRace(
            f"bridge unavailable but Obsidian is running; refusing direct "
            f"write to {vault_rel_path} (would diverge an open editor)"
        )

    # Direct fallback (atomic). Reached only when Obsidian is down.
    try:
        log.info("Direct write: %s", abs_path)
        tmp = abs_path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(abs_path)
        return True
    except OSError as exc:
        log.error("Direct write failed: %s", exc)
        return False
