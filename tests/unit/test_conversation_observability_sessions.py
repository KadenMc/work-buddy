"""Observed-session refresh + span/turn helpers."""

from __future__ import annotations

import os
import time
import types
from pathlib import Path

import pytest

from tests.unit.conversation_observability_fixtures import (
    assistant_text,
    user_turn,
    write_session,
)


# ---------------------------------------------------------------------------
# Fixture: isolated projects + DB
# ---------------------------------------------------------------------------


@pytest.fixture
def co_env(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    db_file = tmp_path / "co.db"

    from work_buddy.sessions import inspector

    monkeypatch.setattr(inspector, "_CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db._default_db_path",
        lambda: db_file,
    )
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db.db_path",
        lambda cfg=None: db_file,
    )
    inspector._commit_cache.clear()
    return {"projects": projects, "db": db_file}


# ---------------------------------------------------------------------------
# refresh_observed_sessions — metadata population + stale-only skip
# ---------------------------------------------------------------------------


def _basic_session_entries() -> list[dict]:
    return [
        user_turn("First question", "2026-05-13T10:00:00Z"),
        assistant_text("First answer", "2026-05-13T10:00:01Z"),
        user_turn("Second question", "2026-05-13T10:00:05Z"),
        assistant_text("Second answer", "2026-05-13T10:00:06Z"),
    ]


def test_refresh_populates_observed_sessions_metadata(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        query_observed_session,
        refresh_observed_sessions,
    )

    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=_basic_session_entries(),
    )

    summary = refresh_observed_sessions(days=30)
    assert summary["observed"] == 1
    assert summary["errored"] == 0

    rec = query_observed_session(sid)
    assert rec is not None
    assert rec["status"] == "ok"
    assert rec["message_count"] == 4
    assert rec["span_count"] >= 1
    assert rec["start_time"] == "2026-05-13T10:00:00Z"
    assert rec["end_time"] == "2026-05-13T10:00:06Z"
    assert rec["project_name"] == "alpha"


def test_refresh_skips_unchanged_files_when_stale_only(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )

    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=_basic_session_entries(),
    )

    first = refresh_observed_sessions(days=30)
    assert first["observed"] == 1

    # No file change → second refresh should skip the session.
    second = refresh_observed_sessions(days=30, stale_only=True)
    assert second["observed"] == 0
    assert second["skipped"] == 1


def test_refresh_re_observes_when_file_changes(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )

    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    path = write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=_basic_session_entries(),
    )
    refresh_observed_sessions(days=30)

    # Bump mtime.
    new_mtime = time.time() + 10
    os.utime(path, (new_mtime, new_mtime))

    summary = refresh_observed_sessions(days=30)
    assert summary["observed"] == 1


def test_refresh_max_sessions_caps_work(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )

    for i in range(5):
        sid = f"dddddddd-dddd-dddd-dddd-{i:012d}"
        write_session(
            co_env["projects"] / "alpha",
            session_id=sid,
            entries=_basic_session_entries(),
        )

    summary = refresh_observed_sessions(days=30, max_sessions=2)
    assert summary["observed"] == 2


# ---------------------------------------------------------------------------
# span_range_to_turn_range — uses ConversationSession.span_to_turns
# ---------------------------------------------------------------------------


def test_span_range_maps_via_span_to_turns(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        span_range_to_turn_range,
    )
    from work_buddy.sessions.inspector import ConversationSession

    sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    # 8 turns → 2 spans of 4 with default span_max_turns=4.
    entries = []
    for i in range(4):
        entries.append(user_turn(f"q{i}", f"2026-05-13T10:0{i}:00Z"))
        entries.append(assistant_text(f"a{i}", f"2026-05-13T10:0{i}:01Z"))
    write_session(co_env["projects"] / "alpha", session_id=sid, entries=entries)

    session = ConversationSession(sid)
    session._ensure_loaded()
    assert len(session._span_map) == 2

    # span 0 → first 4 turns, span 1 → last 4 turns.
    assert span_range_to_turn_range(session, 0, 1) == (0, 4)
    assert span_range_to_turn_range(session, 1, 2) == (4, 8)
    # Full range.
    assert span_range_to_turn_range(session, 0, 2) == (0, 8)


def test_span_range_clamps_to_max_span(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        span_range_to_turn_range,
    )
    from work_buddy.sessions.inspector import ConversationSession

    sid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    entries = []
    for i in range(4):
        entries.append(user_turn(f"q{i}", f"2026-05-13T11:0{i}:00Z"))
        entries.append(assistant_text(f"a{i}", f"2026-05-13T11:0{i}:01Z"))
    write_session(co_env["projects"] / "alpha", session_id=sid, entries=entries)

    session = ConversationSession(sid)
    session._ensure_loaded()
    # Requesting span_end=999 should clamp to the last available span.
    assert span_range_to_turn_range(session, 0, 999) == (0, 8)


def test_span_range_rejects_invalid_input(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        span_range_to_turn_range,
    )

    fake_session = types.SimpleNamespace(_span_map={0: (0, 4)})
    with pytest.raises(ValueError):
        span_range_to_turn_range(fake_session, -1, 1)
    with pytest.raises(ValueError):
        span_range_to_turn_range(fake_session, 0, 0)


# ---------------------------------------------------------------------------
# list_observed_sessions — read-only filtering
# ---------------------------------------------------------------------------


def test_list_observed_sessions_filters_by_project(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        list_observed_sessions,
        refresh_observed_sessions,
    )

    write_session(
        co_env["projects"] / "alpha",
        session_id="11111111-1111-1111-1111-111111111111",
        entries=_basic_session_entries(),
    )
    write_session(
        co_env["projects"] / "beta",
        session_id="22222222-2222-2222-2222-222222222222",
        entries=_basic_session_entries(),
    )
    refresh_observed_sessions(days=30)

    alpha_only = list_observed_sessions(project="alpha")
    assert len(alpha_only) == 1
    assert alpha_only[0]["project_name"] == "alpha"
