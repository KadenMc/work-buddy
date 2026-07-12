"""Bundle contracts + labels: the raw-chat agent-conversation suppression flag,
empty->no-file, honest window labels, the per-file window banner + manifest
window, and the enriched agent-session-summary rendering (PRs line +
first-message fallback).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

UTC = timezone.utc


# ---------------------------------------------------------------------------
# chat collector: include_agent flag + empty -> ""
# ---------------------------------------------------------------------------


def _specstory_entry():
    return {
        "repo": "r",
        "title": "t",
        "timestamp": "2026-07-07 10:00",
        "preview": "",
        "file": "x.md",
    }


def test_include_agent_flag_gates_fetch_and_section(monkeypatch, tmp_path):
    from work_buddy.collectors import chat_collector as cc

    monkeypatch.setattr(cc, "_find_specstory_files", lambda *a, **k: [_specstory_entry()])
    monkeypatch.setattr(cc, "_parse_claude_history", lambda *a, **k: [])
    calls = {"n": 0}

    def fake_agent(*a, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(cc, "_get_agent_conversations", fake_agent)

    # Flag off: agent source not fetched, no agent section emitted.
    out_off = cc.collect({"repos_root": str(tmp_path), "include_agent_conversations": False})
    assert calls["n"] == 0
    assert "## Agent Conversations" not in out_off
    assert "## SpecStory Sessions" in out_off  # specstory present -> not empty

    # Flag on (default behavior): agent source fetched, section present.
    out_on = cc.collect({"repos_root": str(tmp_path), "include_agent_conversations": True})
    assert calls["n"] == 1
    assert "## Agent Conversations" in out_on


def test_chat_empty_window_returns_empty_string(monkeypatch, tmp_path):
    from work_buddy.collectors import chat_collector as cc

    monkeypatch.setattr(cc, "_find_specstory_files", lambda *a, **k: [])
    monkeypatch.setattr(cc, "_parse_claude_history", lambda *a, **k: [])
    monkeypatch.setattr(cc, "_get_agent_conversations", lambda *a, **k: [])

    # Nothing in the window -> empty string, so the bundle writer emits no file.
    assert cc.collect({"repos_root": str(tmp_path)}) == ""


def test_window_label_honest():
    from work_buddy.collectors.chat_collector import _window_label

    assert _window_label(None, None, 7) == "last 7 days"
    lbl = _window_label("2026-07-07T14:40:00+00:00", "2026-07-08T09:00:00+00:00", 7)
    assert "→" in lbl and "2026-07-07" in lbl and "last 7 days" not in lbl


# ---------------------------------------------------------------------------
# window banner + manifest window
# ---------------------------------------------------------------------------


def test_window_header_banner():
    from work_buddy.collect import _window_header

    s = datetime(2026, 7, 7, 14, 40, tzinfo=UTC)
    u = datetime(2026, 7, 8, 9, 0, tzinfo=UTC)
    hdr = _window_header(s, u)
    assert hdr.startswith("*Window:") and "→" in hdr and hdr.endswith("\n\n")
    assert _window_header(None, None) == ""


def test_write_meta_records_window(tmp_path):
    from work_buddy.collect import _write_meta

    _write_meta(
        tmp_path,
        {"vault_root": "v", "repos_root": "r"},
        ["git"],
        since=datetime(2026, 7, 7, tzinfo=UTC),
        until=datetime(2026, 7, 8, tzinfo=UTC),
    )
    meta = json.loads((tmp_path / "bundle_meta.json").read_text(encoding="utf-8"))
    assert meta["window"]["since"].startswith("2026-07-07")
    assert meta["window"]["until"].startswith("2026-07-08")


# ---------------------------------------------------------------------------
# collect_bundle: bundle default suppresses agent conversations in raw chat
# ---------------------------------------------------------------------------


def test_collect_bundle_defaults_suppress_agent_conversations(monkeypatch):
    import pathlib

    import work_buddy.collect as collect_mod

    captured: dict = {}

    def fake_run_collection(cfg, only=None, dry_run=False, *, since=None, until=None):
        captured["cfg"] = cfg
        return pathlib.Path("/tmp/bundle")

    monkeypatch.setattr(collect_mod, "run_collection", fake_run_collection)

    from work_buddy.mcp_server.context_wrappers import collect_bundle

    collect_bundle(hours=24)
    assert captured["cfg"]["chats"]["include_agent_conversations"] is False

    # An explicit override wins over the bundle default.
    collect_bundle(hours=24, overrides={"chats": {"include_agent_conversations": True}})
    assert captured["cfg"]["chats"]["include_agent_conversations"] is True


# ---------------------------------------------------------------------------
# agent_session_summary rendering: PRs line + first-message fallback
# ---------------------------------------------------------------------------


@pytest.fixture
def co_db(tmp_path, monkeypatch):
    db_file = tmp_path / "co.db"
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db._default_db_path", lambda: db_file
    )
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db.db_path", lambda cfg=None: db_file
    )
    return db_file


def test_collector_renders_prs_and_first_message_fallback(co_db, monkeypatch):
    from work_buddy.conversation_observability.db import get_connection

    now = datetime.now(UTC).isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO observed_sessions "
        "(session_id, project_name, source_path, source_mtime, observed_at, "
        " start_time, end_time, message_count, first_user_message, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok')",
        ("sess-pr", "alpha", "/x/sess-pr.jsonl", 0.0, now, now, now, 3, "Do the PR thing"),
    )
    conn.execute(
        "INSERT INTO session_prs "
        "(session_id, pr_number, pr_url, repo, action, ts, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("sess-pr", 42, "https://github.com/x/y/pull/42", "x/y", "created", now, now),
    )
    conn.commit()
    conn.close()

    # Summaries off -> no tldr -> the first-message fallback line is rendered.
    monkeypatch.setattr(
        "work_buddy.summarization.policy.summaries_active", lambda: False
    )

    from work_buddy.collectors.agent_session_summary_collector import collect

    out = collect({"refresh": False, "include_tldr": True, "include_topics": True})
    assert "first message: Do the PR thing" in out
    assert "PRs: #42 created" in out
    # Drill footer names the composite + search capabilities.
    assert "conversation_observability_get" in out
    assert "summary_search" in out
