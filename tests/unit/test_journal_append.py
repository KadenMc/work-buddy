"""Regression tests for journal log-entry appending, especially ordering
across the midnight day-boundary.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.journal import (
    _effective_minutes,
    _find_chronological_insertion_point,
    _append_to_journal_locked,
)


def test_effective_minutes_shifts_post_midnight_past_end_of_day():
    # 10:29 PM = 22*60 + 29 = 1349
    assert _effective_minutes("10:29 PM") == 1349
    # 12:08 AM should sort AFTER 10:29 PM (same-day continuation)
    assert _effective_minutes("12:08 AM") == 24 * 60 + 8  # 1448
    # 2:05 AM still within post-midnight window
    assert _effective_minutes("2:05 AM") == 24 * 60 + 125  # 1565
    # 5:00 AM is not post-midnight (cutoff is strict <)
    assert _effective_minutes("5:00 AM") == 5 * 60  # 300


def test_effective_minutes_daytime_unchanged():
    assert _effective_minutes("9:45 AM") == 9 * 60 + 45
    assert _effective_minutes("1:00 PM") == 13 * 60
    assert _effective_minutes("2:15 PM") == 14 * 60 + 15


def _make_journal(tmp_path: Path) -> Path:
    j = tmp_path / "journal" / "2026-04-16.md"
    j.parent.mkdir(parents=True)
    j.write_text(
        "---\n---\n\n# **Log**\n"
        '<font color="#a5a5a5">(CTRL-J for log entry capture)</font>\n'
        "* 9:45 AM - Arrived.\n"
        "* 1:00 PM - Meeting.\n"
        "\n# **Next Section**\n",
        encoding="utf-8",
    )
    return j


def test_append_preserves_order_across_midnight(tmp_path, monkeypatch):
    # Neutralize the consent decorator and bridge write so we exercise pure logic
    from work_buddy import journal as jmod

    # Force the safe direct-write path: bridge unavailable AND Obsidian
    # genuinely down (so vault_write's process guard permits the filesystem
    # write rather than re-raising to protect a possibly-open editor).
    import work_buddy.obsidian.bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "is_available", lambda: False, raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: False, raising=False)

    journal_file = _make_journal(tmp_path)
    entries = [
        ("2:15 PM", "PM one"),
        ("10:29 PM", "PM late"),
        ("12:08 AM", "Post-midnight one"),
        ("2:05 AM", "Post-midnight two"),
    ]
    result = _append_to_journal_locked(entries, journal_file, "2026-04-16")
    assert result["success"] is True
    assert result["entries_written"] == 4

    content = journal_file.read_text(encoding="utf-8")
    # Extract log body
    log_body = content.split("# **Log**", 1)[1].split("# **Next Section**", 1)[0]

    # Post-midnight entries must appear AFTER the latest PM entry
    pm_late_pos = log_body.index("PM late")
    pm1_pos = log_body.index("Post-midnight one")
    pm2_pos = log_body.index("Post-midnight two")
    pm_one_pos = log_body.index("PM one")
    arrived_pos = log_body.index("Arrived")
    meeting_pos = log_body.index("Meeting")

    # Daytime stays chronological
    assert arrived_pos < meeting_pos < pm_one_pos < pm_late_pos
    # Post-midnight lands after PM late
    assert pm_late_pos < pm1_pos < pm2_pos


def test_append_refuses_direct_write_when_obsidian_running(tmp_path, monkeypatch):
    """Regression: a transient bridge outage while Obsidian is RUNNING must not
    fall back to a direct filesystem write. A direct write would diverge an
    open editor's buffer from disk and wedge the note with a persistent 409
    editor_dirty. The write must raise (transient) so the retry queue replays
    it; the file on disk must stay untouched (no divergence created).
    """
    from work_buddy.obsidian.errors import ObsidianStartupRace
    import work_buddy.obsidian.bridge as bridge_mod

    # Bridge reports unavailable (startup race / port flap) but the Obsidian
    # process IS up — an editor may be holding the note.
    monkeypatch.setattr(bridge_mod, "is_available", lambda: False, raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: True, raising=False)

    journal_file = _make_journal(tmp_path)
    before = journal_file.read_text(encoding="utf-8")

    with pytest.raises(ObsidianStartupRace):
        _append_to_journal_locked(
            [("2:15 PM", "must not land via direct write")],
            journal_file,
            "2026-04-16",
        )

    # Disk untouched — the open editor cannot have diverged.
    assert journal_file.read_text(encoding="utf-8") == before


def test_append_idempotent_on_replay(tmp_path, monkeypatch):
    """Replay safety: calling _append_to_journal_locked twice with identical
    entries must land each entry only once. This is the regression guard for
    the post-write-uncertain replay-duplication failure mode: a bridge write
    that actually landed but reported ObsidianPostWriteUncertain triggers a
    retry, and without this guard the retry re-inserts every entry.

    The first call lands the entries. The second call sees them already in
    the file via the formatted_line-in-file check and short-circuits to a
    success response with already_present populated and entries_written=0.
    """
    import work_buddy.obsidian.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "is_available", lambda: False, raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: False, raising=False)

    journal_file = _make_journal(tmp_path)
    entries = [
        ("2:15 PM", "Idempotent test entry one"),
        ("3:30 PM", "Idempotent test entry two"),
    ]

    first = _append_to_journal_locked(entries, journal_file, "2026-04-16")
    assert first["success"] is True
    assert first["entries_written"] == 2
    assert first.get("already_present", []) == []

    # Simulate a PWU-triggered replay: same entries, same file (which now
    # already contains them).
    second = _append_to_journal_locked(entries, journal_file, "2026-04-16")
    assert second["success"] is True
    assert second["entries_written"] == 0
    assert second.get("already_present") == ["2:15 PM", "3:30 PM"]

    # File must contain each entry exactly once.
    content = journal_file.read_text(encoding="utf-8")
    assert content.count("Idempotent test entry one") == 1
    assert content.count("Idempotent test entry two") == 1


def test_append_mixed_already_present_and_new(tmp_path, monkeypatch):
    """A replay that includes a partial overlap with file state: some entries
    are already present (from a prior write that landed), some are genuinely
    new. The new ones must land; the already-present ones must be reported in
    already_present without producing duplicates.
    """
    import work_buddy.obsidian.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "is_available", lambda: False, raising=False)
    monkeypatch.setattr(bridge_mod, "is_obsidian_running", lambda: False, raising=False)

    journal_file = _make_journal(tmp_path)
    # Pre-seed one entry as if a prior write had landed it.
    _append_to_journal_locked(
        [("2:15 PM", "Already there")],
        journal_file,
        "2026-04-16",
    )

    mixed = [
        ("2:15 PM", "Already there"),
        ("4:00 PM", "Genuinely new"),
    ]
    result = _append_to_journal_locked(mixed, journal_file, "2026-04-16")
    assert result["success"] is True
    assert result["entries_written"] == 1
    assert result.get("already_present") == ["2:15 PM"]

    content = journal_file.read_text(encoding="utf-8")
    assert content.count("Already there") == 1
    assert content.count("Genuinely new") == 1


def test_append_passes_insert_mode_witness_to_vault_write(tmp_path, monkeypatch):
    """The vault_write call must use write_mode='insert' with an explicit
    content_hint drawn from the first inserted line. The default insert hint
    (first 256 chars of the file content) would always be stable journal
    frontmatter and would cause the post-write verifier to always return
    'verified' regardless of whether the write landed.
    """
    from work_buddy import journal as jmod

    captured: dict = {}

    def fake_vault_write(vault_rel_path, abs_path, content, *, write_mode, content_hint=None):
        captured["write_mode"] = write_mode
        captured["content_hint"] = content_hint
        # Mimic a successful filesystem write so the rest of the function
        # proceeds normally.
        abs_path.write_text(content, encoding="utf-8")
        return True

    # Patch the import inside _append_to_journal_locked (it does a local
    # `from work_buddy.obsidian.vault_writer import vault_write`).
    import work_buddy.obsidian.vault_writer as vw
    monkeypatch.setattr(vw, "vault_write", fake_vault_write, raising=False)

    journal_file = _make_journal(tmp_path)
    entries = [("2:15 PM", "Witness check entry")]
    result = _append_to_journal_locked(entries, journal_file, "2026-04-16")

    assert result["success"] is True
    assert captured["write_mode"] == "insert"
    # Witness should be the formatted line for our new entry, which contains
    # the bullet, the timestamp, and the description.
    assert captured["content_hint"] is not None
    assert "2:15 PM" in captured["content_hint"]
    assert "Witness check entry" in captured["content_hint"]
    assert "#wb/journal/log" in captured["content_hint"]
