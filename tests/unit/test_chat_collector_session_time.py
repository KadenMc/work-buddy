"""Regression: chat_collector windows on real session time and renders local.

A Claude Code session JSONL is rewritten (mtime bumped to the present) whenever
the session is resumed. Two correctness contracts are pinned here:

1. **Windowing** — a session is selected by its real message-derived time (or, for
   SpecStory, its filename stamp), not the file mtime. A June-5 conversation
   resumed on June-14 must NOT be pulled into a June-14 window. Sessions with no
   parseable timestamp fall back to the mtime decision so they aren't dropped.
2. **Timezone** — session times render in the user's local zone (USER_TZ), matching
   git's local commit times and the journal's local Log entries, with no "UTC"
   label. Tests pin America/Toronto (UTC-4 in June) so a missed conversion fails.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import work_buddy.config as config
from work_buddy.collectors import chat_collector as cc


@pytest.fixture
def pin_tz(monkeypatch):
    """Pin USER_TZ to America/Toronto via the lazy cache (no config load)."""
    monkeypatch.setattr(config, "_USER_TZ_CACHE", ZoneInfo("America/Toronto"))
    return ZoneInfo("America/Toronto")


# ---------------------------------------------------------------------------
# _format_session_when — real start/end, converted to local; fallback only absent
# ---------------------------------------------------------------------------


def test_format_session_when_same_day_range(pin_tz):
    out = cc._format_session_when("2026-06-05T17:30:00Z", "2026-06-05T18:11:00Z")
    assert out == "2026-06-05 13:30–14:11"  # UTC-4


def test_format_session_when_cross_day_range(pin_tz):
    # 23:30Z → 19:30 local, 01:11Z(next) → 21:11 local: collapses to one local day.
    out = cc._format_session_when("2026-06-05T23:30:00Z", "2026-06-06T01:11:00Z")
    assert out == "2026-06-05 19:30–21:11"


def test_format_session_when_falls_back_only_without_timestamps(pin_tz):
    # No real timestamps → use the supplied fallback (e.g. mtime), as a last resort.
    assert cc._format_session_when(None, None, fallback="2026-06-14 19:58") == "2026-06-14 19:58"
    # But a real start_time always wins over the fallback (and renders local).
    assert cc._format_session_when(
        "2026-06-05T17:30:00Z", None, fallback="2026-06-14 19:58"
    ) == "2026-06-05 13:30"


# ---------------------------------------------------------------------------
# collect() — windowing by real conversation time
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Isolated ~/.claude with one project dir; cache redirected to tmp."""
    home = tmp_path / "home"
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)

    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cc, "_CACHE_PATH", home / ".claude" / "chat_cache.json")
    # Defeat the in-process memo so the redirected cache path is honored.
    monkeypatch.setattr(cc, "_parsed_cache", None)
    monkeypatch.setattr(cc, "_parsed_cache_state", None)
    return projects


def _write_session(project_dir, session_id, turns, mtime_iso):
    """Write a JSONL session with the given turns, then stamp its file mtime."""
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")
    mtime = datetime.fromisoformat(mtime_iso.replace("Z", "+00:00")).timestamp()
    os.utime(path, (mtime, mtime))
    return path


def _turn(role, ts, text):
    msg = (
        {"role": "user", "content": text}
        if role == "user"
        else {"role": "assistant", "content": [{"type": "text", "text": text}]}
    )
    entry = {"type": role, "message": msg}
    if ts is not None:
        entry["timestamp"] = ts
    return entry


def _june14_window():
    # Covers the June-14 mtime but not the June-5 conversation.
    return {"since": "2026-06-14T00:00:00", "until": "2026-06-15T00:00:00"}


def test_collect_excludes_session_outside_real_time_window(claude_home, tmp_path, pin_tz):
    """A June-5 conversation resumed on June-14 is NOT pulled into a June-14 window."""
    project_dir = claude_home / "C--code-repos-myproject"
    turns = [
        _turn("user", "2026-06-05T17:30:00.000Z", "let's reconcile the merge"),
        _turn("assistant", "2026-06-05T18:11:00.000Z", "Looking at both files now."),
    ]
    # JSONL rewritten on 2026-06-14 (resume) → mtime is June-14.
    _write_session(project_dir, "7fb19cb6-aaaa-bbbb-cccc-000000000000", turns, "2026-06-14T19:58:00Z")

    cfg = {"repos_root": str(tmp_path / "empty_repos"), "chats": {}, **_june14_window()}
    out = cc.collect(cfg)

    # The real conversation time falls outside the window → excluded entirely.
    assert "7fb19cb6" not in out
    # And the misleading mtime date never appears as the session's time.
    assert "2026-06-14 19:58" not in out


def test_collect_includes_in_window_session_with_local_render(claude_home, tmp_path, pin_tz):
    """A session whose real time is in-window is included, rendered in local time."""
    project_dir = claude_home / "C--code-repos-myproject"
    turns = [
        _turn("user", "2026-06-14T15:00:00.000Z", "kick off the run"),
        _turn("assistant", "2026-06-14T15:30:00.000Z", "Run started."),
    ]
    _write_session(project_dir, "abc12345-aaaa-bbbb-cccc-000000000000", turns, "2026-06-14T15:30:00Z")

    cfg = {"repos_root": str(tmp_path / "empty_repos"), "chats": {}, **_june14_window()}
    out = cc.collect(cfg)

    assert "abc12345" in out
    # 15:00–15:30 UTC → 11:00–11:30 local (UTC-4).
    assert "2026-06-14 11:00–11:30" in out
    # The Collected header is local — no "UTC" label anywhere in the summary.
    assert "UTC" not in out


def test_collect_keeps_timestampless_session_via_mtime_fallback(claude_home, tmp_path, pin_tz):
    """A session with no parseable timestamps falls back to the mtime decision."""
    project_dir = claude_home / "C--code-repos-myproject"
    turns = [
        _turn("user", None, "no timestamps here"),
        _turn("assistant", None, "still answering"),
    ]
    # mtime is in-window; real timestamps are absent.
    _write_session(project_dir, "deadbeef-aaaa-bbbb-cccc-000000000000", turns, "2026-06-14T12:00:00Z")

    cfg = {"repos_root": str(tmp_path / "empty_repos"), "chats": {}, **_june14_window()}
    out = cc.collect(cfg)

    # HARD CONSTRAINT: timestamp-less sessions are not silently dropped.
    assert "deadbeef" in out


# ---------------------------------------------------------------------------
# SpecStory — windowed by filename stamp, mtime fallback when unparseable
# ---------------------------------------------------------------------------


def _write_specstory(repos_root, repo, filename, content="_User_\nhello\n", mtime_iso=None):
    history = repos_root / repo / ".specstory" / "history"
    history.mkdir(parents=True, exist_ok=True)
    path = history / filename
    path.write_text(content, encoding="utf-8")
    if mtime_iso:
        m = datetime.fromisoformat(mtime_iso.replace("Z", "+00:00")).timestamp()
        os.utime(path, (m, m))
    return path


def test_collect_excludes_specstory_outside_real_time_window(claude_home, tmp_path, pin_tz):
    """A SpecStory file whose filename stamp is old is excluded despite a recent mtime."""
    repos_root = tmp_path / "repos"
    _write_specstory(
        repos_root, "myrepo", "2026-06-05_17-30-00Z-old-conversation.md",
        mtime_iso="2026-06-14T19:58:00Z",
    )
    cfg = {"repos_root": str(repos_root), "chats": {}, **_june14_window()}
    out = cc.collect(cfg)

    assert "Old Conversation" not in out


def test_collect_keeps_specstory_without_parseable_name_via_mtime(claude_home, tmp_path, pin_tz):
    """A SpecStory file with no parseable filename stamp falls back to mtime."""
    repos_root = tmp_path / "repos"
    _write_specstory(
        repos_root, "myrepo", "freeform-notes.md",
        mtime_iso="2026-06-14T12:00:00Z",
    )
    cfg = {"repos_root": str(repos_root), "chats": {}, **_june14_window()}
    out = cc.collect(cfg)

    # Title slug falls back to the filename stem; mtime is in-window → included.
    assert "freeform-notes" in out
