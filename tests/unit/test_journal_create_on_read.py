"""Tests for the ``create_on_read`` contract: reading journal state must not
create a file unless the caller opts in, and opting in can only create today
(a missing non-today date is a "needed but impossible" error).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import work_buddy.journal as jmod
from work_buddy.journal import ensure_journal_exists, read_journal_state


class _SpyObsidian:
    """Stand-in for ObsidianCommands that never touches a real vault."""

    def __init__(self, vault_root):  # noqa: D401 - test stub
        self.vault_root = vault_root


def _spy_daily(record: list[str], *, creates: Path | None = None):
    """Build a DailyNotesCommands stand-in that records open_today() calls.

    If ``creates`` is given, open_today() writes that file (simulating
    Obsidian materialising today's note from template).
    """

    class _SpyDaily:
        def __init__(self, client):  # noqa: D401 - test stub
            self.client = client

        def open_today(self):
            record.append("open_today")
            if creates is not None:
                creates.parent.mkdir(parents=True, exist_ok=True)
                creates.write_text("---\n---\n\n# **Log**\n", encoding="utf-8")

    return _SpyDaily


def _patch_obsidian(monkeypatch, daily_cls):
    import work_buddy.obsidian.commands as cmds_mod
    import work_buddy.obsidian.commands.daily_notes as daily_mod

    monkeypatch.setattr(cmds_mod, "ObsidianCommands", _SpyObsidian, raising=False)
    monkeypatch.setattr(daily_mod, "DailyNotesCommands", daily_cls, raising=False)


# --- ensure_journal_exists -------------------------------------------------


def test_ensure_create_false_missing_is_benign_and_skips_obsidian(tmp_path, monkeypatch):
    calls: list[str] = []
    _patch_obsidian(monkeypatch, _spy_daily(calls))

    result = ensure_journal_exists(tmp_path, "2026-01-01", create=False)

    assert result["exists"] is False
    assert result["created"] is False
    assert result["error"] is None
    # No file written, Obsidian never invoked.
    assert not (tmp_path / "journal" / "2026-01-01.md").exists()
    assert calls == []


def test_ensure_create_true_non_today_missing_errors_and_skips_obsidian(tmp_path, monkeypatch):
    # Pin "today" so the target date is unambiguously a non-today date.
    import datetime as _dt

    fixed = _dt.datetime(2026, 6, 26, 10, 0, 0)
    monkeypatch.setattr(jmod, "user_now", lambda: fixed)
    calls: list[str] = []
    _patch_obsidian(monkeypatch, _spy_daily(calls))

    result = ensure_journal_exists(tmp_path, "2026-05-01", create=True)

    assert result["exists"] is False
    assert result["created"] is False
    assert result["error"] is not None  # needed but impossible
    assert "2026-05-01" in result["error"]
    assert not (tmp_path / "journal" / "2026-05-01.md").exists()
    assert calls == []  # Obsidian can't template a past date, so we don't try


def test_ensure_create_true_existing_past_date_reads_without_error(tmp_path, monkeypatch):
    import datetime as _dt

    fixed = _dt.datetime(2026, 6, 26, 10, 0, 0)
    monkeypatch.setattr(jmod, "user_now", lambda: fixed)
    past = tmp_path / "journal" / "2026-05-01.md"
    past.parent.mkdir(parents=True)
    past.write_text("---\n---\n", encoding="utf-8")

    result = ensure_journal_exists(tmp_path, "2026-05-01", create=True)

    assert result["exists"] is True
    assert result["error"] is None


def test_ensure_create_true_today_missing_creates(tmp_path, monkeypatch):
    import datetime as _dt

    fixed = _dt.datetime(2026, 6, 26, 10, 0, 0)
    monkeypatch.setattr(jmod, "user_now", lambda: fixed)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    today_file = tmp_path / "journal" / "2026-06-26.md"
    calls: list[str] = []
    _patch_obsidian(monkeypatch, _spy_daily(calls, creates=today_file))

    result = ensure_journal_exists(tmp_path, "2026-06-26", create=True)

    assert result["exists"] is True
    assert result["created"] is True
    assert result["error"] is None
    assert calls == ["open_today"]
    assert today_file.exists()


# --- read_journal_state ----------------------------------------------------


def test_read_journal_state_default_does_not_create(tmp_path, monkeypatch):
    monkeypatch.setattr(jmod, "load_config", lambda: {"vault_root": str(tmp_path)})
    calls: list[str] = []
    _patch_obsidian(monkeypatch, _spy_daily(calls))

    # A far-future, never-journaled date: unambiguous and missing.
    state = read_journal_state(target="2099-01-01")

    assert state["exists"] is False
    assert state["created"] is False
    assert state["error"] is None  # benign absence, default create_on_read=False
    assert not (tmp_path / "journal" / "2099-01-01.md").exists()
    assert calls == []


def test_read_journal_state_create_on_read_true_non_today_surfaces_error(tmp_path, monkeypatch):
    monkeypatch.setattr(jmod, "load_config", lambda: {"vault_root": str(tmp_path)})
    calls: list[str] = []
    _patch_obsidian(monkeypatch, _spy_daily(calls))

    state = read_journal_state(target="2099-01-01", create_on_read=True)

    assert state["exists"] is False
    assert state["error"] is not None  # needed but impossible bubbles up
    assert calls == []
