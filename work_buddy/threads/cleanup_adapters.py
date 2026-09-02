"""Cleanup adapter implementations.

Each adapter knows how to mutate one inciting source.
This module ships the journal-note adapter (the canonical case);
Stage 4.13 adds the Chrome adapter.

Per UX.md §6:
- Inciting source determines whether cleanup applies.
- ``source_already_gone`` is treated as success (the user's intent
  was "this is handled" — fulfilled either way).
- Adapter writes a CleanupResult; the FSM handler that calls it
  records the appropriate event + transitions the Thread.

Bootstrap: ``register_default_adapters()`` is called from the
sidecar's ``bootstrap_threads()`` after the cleanup framework is in
place. Tests can call it explicitly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from work_buddy.threads.cleanup import (
    CleanupAdapter,
    CleanupResult,
    register_cleanup_adapter,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Journal-note adapter
# ---------------------------------------------------------------------------
#
# Inciting-event-summary shape expected on the Thread:
#   {
#       "source": "journal_note",
#       "note_path": "Daily/2026-05-02.md",  # vault-relative
#       "line_text": "- [ ] Buy gift for Sarah",  # exact match
#       "line_number": 42,  # 1-based, optional (diagnostic only)
#   }
#
# Behavior:
# - read the file
# - search for an exact-text line match (line_text)
# - if found: remove that one line + write back; return success
# - if not found: return source_already_gone=True (user already
#   handled it manually)
# ---------------------------------------------------------------------------


def _journal_note_can_clean_up(thread) -> bool:  # type: ignore[no-untyped-def]
    """We can clean up iff the inciting summary has a note_path
    AND a line_text. Without both, we can't find the target."""
    if not _legacy_journal_cleanup_available():
        return False
    summary = getattr(thread, "inciting_event_summary", None) or {}
    if summary.get("source") != "journal_note":
        return False
    if not summary.get("note_path"):
        return False
    if not summary.get("line_text"):
        return False
    return True


def _journal_note_cleanup(thread) -> CleanupResult:  # type: ignore[no-untyped-def]
    """Remove the inciting line from the journal note."""
    from work_buddy.health.preferences import is_wanted

    if is_wanted("obsidian") is False:
        return CleanupResult(
            success=False,
            detail="legacy Journal-note cleanup is retired under native authority",
        )
    from work_buddy.journal_capture.authority import (
        JournalAuthorityStateError,
        legacy_markdown_write_guard,
    )

    try:
        with legacy_markdown_write_guard():
            return _journal_note_cleanup_locked(thread)
    except JournalAuthorityStateError:
        return CleanupResult(
            success=False,
            detail="legacy Journal-note cleanup is retired under native authority",
        )


def _journal_note_cleanup_locked(thread) -> CleanupResult:  # type: ignore[no-untyped-def]
    """Clean a Journal line while the durable compatibility lock is held."""

    summary = getattr(thread, "inciting_event_summary", None) or {}
    note_path: str = summary.get("note_path")
    line_text: str = summary.get("line_text")
    if not note_path or not line_text:
        return CleanupResult(
            success=False,
            detail="inciting summary missing note_path or line_text",
        )

    from work_buddy.config import load_config
    from work_buddy.journal_capture.content_adapter import JournalContentAdapter

    path = Path(note_path.replace("\\", "/"))
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or ".." in path.parts
        or path.suffix.lower() != ".md"
    ):
        return CleanupResult(success=False, detail="journal note path is invalid")
    adapter = JournalContentAdapter(
        load_config()["vault_root"],
        journal_dir=path.parent,
    )
    try:
        snapshot = adapter.snapshot(path.stem)
        content = snapshot.content
    except RuntimeError:
        content = None
    if content is None:
        # File missing or bridge unreachable — we can't tell which.
        # Conservative: report as failure (don't claim source-gone
        # on bridge unavailability; that masks real outages).
        return CleanupResult(
            success=False,
            detail=f"could not read {note_path!r} (bridge unreachable or file missing)",
        )

    lines = content.split("\n")
    target = line_text.strip()

    matching_indices = [
        i for i, line in enumerate(lines) if line.strip() == target
    ]
    if not matching_indices:
        # Source already gone — user manually edited the line out.
        return CleanupResult(
            success=True,
            source_already_gone=True,
            detail=f"line not found in {note_path!r} (user edited it out?)",
        )

    # Remove only the FIRST occurrence (defensive — if there are
    # duplicates, we don't want to nuke them all).
    idx = matching_indices[0]
    new_lines = lines[:idx] + lines[idx + 1:]
    new_content = "\n".join(new_lines)

    try:
        adapter.write_day_cas(
            path.stem,
            expected_file_sha256=snapshot.file_sha256,
            content=new_content,
            content_hint="wb:journal-thread-cleanup/v1",
        )
    except RuntimeError:
        return CleanupResult(
            success=False,
            detail=f"Journal content adapter refused the update for {note_path!r}",
        )
    return CleanupResult(
        success=True,
        detail=f"removed line {idx + 1} from {note_path!r}: {target[:60]}",
    )


def _legacy_journal_cleanup_available() -> bool:
    """Check the durable authority before any archive/config inspection."""

    try:
        from work_buddy.journal_capture.authority import existing_authority_mode

        if existing_authority_mode() != "legacy_compatibility":
            return False
        from work_buddy.health.preferences import is_wanted

        return is_wanted("obsidian") is not False
    except Exception:
        # An unreadable native authority must never reopen a compatibility
        # mutator or advertise it in the dashboard.
        return False


JOURNAL_NOTE_ADAPTER = CleanupAdapter(
    source="journal_note",
    can_clean_up=_journal_note_can_clean_up,
    cleanup=_journal_note_cleanup,
    description="Remove the inciting line from its source journal note.",
)


# ---------------------------------------------------------------------------
# Default-adapter registration
# ---------------------------------------------------------------------------


def register_default_adapters() -> None:
    """Register the Stage 4 default adapters. Called from
    bootstrap_threads() at sidecar startup.

    Idempotent: register_cleanup_adapter overwrites by source.
    """
    register_cleanup_adapter(JOURNAL_NOTE_ADAPTER)
    # Stage 4.13 — Chrome-tab adapter ships as a stub (closing tabs
    # from Python isn't supported by the existing native-messaging
    # host). The stub registers so the UI's Clean Up button shows on
    # Chrome-tab Threads (legacy per-tab sub-threads) — adapter now
    # calls the working ``chrome_collector.close_tabs`` mutator.
    try:
        from work_buddy.collectors.chrome_cleanup_adapter import (
            register_chrome_tab_cleanup_adapter,
        )
        register_chrome_tab_cleanup_adapter()
        logger.info(
            "cleanup: registered default adapters "
            "[journal_note, chrome_tab]",
        )
    except Exception as e:
        logger.warning("Chrome-tab adapter registration failed: %s", e)
        logger.info("cleanup: registered default adapters [journal_note]")
