"""Regression: chat_collector must render a conversation's real time, not file mtime.

A Claude Code session JSONL is rewritten (mtime bumped to the present) whenever
the session is resumed. The chat summary used to display that mtime as the
session's timestamp, which silently misdated resumed sessions by days — a
collected June-5 conversation showed up under a June-14 header and nearly drove a
fabricated journal entry. The session's own message timestamps are the
authoritative "when did this happen" signal; these tests pin that contract.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from work_buddy.collectors import chat_collector as cc


# ---------------------------------------------------------------------------
# _format_session_when — prefers real start/end, falls back only when absent
# ---------------------------------------------------------------------------


def test_format_session_when_same_day_range():
    out = cc._format_session_when("2026-06-05T17:30:00Z", "2026-06-05T18:11:00Z")
    assert out == "2026-06-05 17:30–18:11"


def test_format_session_when_cross_day_range():
    out = cc._format_session_when("2026-06-05T23:30:00Z", "2026-06-06T01:11:00Z")
    assert out == "2026-06-05 23:30–2026-06-06 01:11"


def test_format_session_when_falls_back_only_without_timestamps():
    # No real timestamps → use the supplied fallback (e.g. mtime), as a last resort.
    assert cc._format_session_when(None, None, fallback="2026-06-14 19:58") == "2026-06-14 19:58"
    # But a real start_time always wins over the fallback.
    assert cc._format_session_when(
        "2026-06-05T17:30:00Z", None, fallback="2026-06-14 19:58"
    ) == "2026-06-05 17:30"


# ---------------------------------------------------------------------------
# collect() — a resumed session renders its real date, never its mtime
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


def test_collect_uses_session_time_not_mtime(claude_home, tmp_path):
    project_dir = claude_home / "C--code-repos-myproject"
    # Conversation actually happened on 2026-06-05 ...
    turns = [
        {
            "type": "user",
            "timestamp": "2026-06-05T17:30:00.000Z",
            "message": {"role": "user", "content": "let's reconcile the merge"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-05T18:11:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Looking at both files now."}],
            },
        },
    ]
    # ... but the JSONL was rewritten on 2026-06-14 (resume), bumping its mtime.
    _write_session(project_dir, "7fb19cb6-aaaa-bbbb-cccc-000000000000", turns, "2026-06-14T19:58:00Z")

    cfg = {
        "repos_root": str(tmp_path / "empty_repos"),  # no specstory
        "chats": {},
        # Window covers the June-14 mtime but NOT the June-5 conversation.
        "since": "2026-06-14T00:00:00",
        "until": "2026-06-15T00:00:00",
    }
    out = cc.collect(cfg)

    # The session is pulled in by mtime, but must be labeled with its real date.
    assert "7fb19cb6" in out
    assert "2026-06-05 17:30–18:11" in out
    # The misleading mtime date must NOT appear as the session's time.
    assert "2026-06-14 19:58" not in out
