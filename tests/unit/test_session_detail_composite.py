"""Composite per-session view, topic timestamps, and first-user-message capture."""

from __future__ import annotations

import pytest

from tests.unit.conversation_observability_fixtures import (
    assistant_text,
    user_turn,
    write_session,
)


# ---------------------------------------------------------------------------
# conversation_observability_get composite (session_detail)
# ---------------------------------------------------------------------------


def test_flags_off_is_bare_observed_row(monkeypatch):
    import work_buddy.conversation_observability.sessions as sessions_mod

    row = {"session_id": "s1", "project_name": "alpha", "message_count": 5}
    monkeypatch.setattr(sessions_mod, "query_observed_session", lambda sid: dict(row))

    from work_buddy.conversation_observability.session_detail import session_detail

    got = session_detail("s1")
    assert got == row  # content-identical to the raw observed row
    assert "summary" not in got and "commits" not in got


def test_none_when_session_unobserved(monkeypatch):
    import work_buddy.conversation_observability.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "query_observed_session", lambda sid: None)

    from work_buddy.conversation_observability.session_detail import session_detail

    assert session_detail("nope", include_summary=True, include_commits=True) is None


def _patch_atoms(monkeypatch):
    import work_buddy.conversation_observability.commits as commits_mod
    import work_buddy.conversation_observability.prs as prs_mod
    import work_buddy.conversation_observability.session_summary_row as ssr_mod
    import work_buddy.conversation_observability.sessions as sessions_mod
    import work_buddy.conversation_observability.writes as writes_mod

    monkeypatch.setattr(sessions_mod, "query_observed_session", lambda sid: {"session_id": sid})
    monkeypatch.setattr(
        ssr_mod,
        "session_summary_row",
        lambda sid: {"session_id": sid, "tldr": "t", "topics": [{"title": "x"}]},
    )
    monkeypatch.setattr(commits_mod, "query_session_commits", lambda session_id=None: [{"hash": "abc"}])
    monkeypatch.setattr(writes_mod, "query_session_writes", lambda session_id=None: [{"file_path": "a.py"}])
    monkeypatch.setattr(prs_mod, "query_session_prs", lambda session_id=None: [{"pr_number": 1}])


def test_each_flag_adds_its_key(monkeypatch):
    _patch_atoms(monkeypatch)
    from work_buddy.conversation_observability.session_detail import session_detail

    got = session_detail(
        "s1",
        include_summary=True,
        include_commits=True,
        include_writes=True,
        include_prs=True,
    )
    assert got["commits"] == [{"hash": "abc"}]
    assert got["writes"] == [{"file_path": "a.py"}]
    assert got["prs"] == [{"pr_number": 1}]
    # include_summary alone drops the (potentially large) topic list.
    assert got["summary"]["tldr"] == "t"
    assert "topics" not in got["summary"]


def test_topics_imply_summary_and_keep_topics(monkeypatch):
    _patch_atoms(monkeypatch)
    from work_buddy.conversation_observability.session_detail import session_detail

    got = session_detail("s1", include_topics=True)
    assert "summary" in got  # topics implied summary
    assert got["summary"]["topics"] == [{"title": "x"}]  # topic list kept


# ---------------------------------------------------------------------------
# topic timestamp derivation
# ---------------------------------------------------------------------------


def test_turn_timestamp_clamps_and_reads():
    from work_buddy.conversation_observability.session_summary_row import _turn_timestamp

    class FakeSession:
        turns = [
            {"timestamp": "2026-07-07T10:00:00Z"},
            {"timestamp": "2026-07-07T11:00:00Z"},
            {"timestamp": "2026-07-07T12:00:00Z"},
        ]

    s = FakeSession()
    assert _turn_timestamp(s, 0) == "2026-07-07T10:00:00Z"
    assert _turn_timestamp(s, 2) == "2026-07-07T12:00:00Z"
    assert _turn_timestamp(s, 99) == "2026-07-07T12:00:00Z"  # clamped to last turn
    assert _turn_timestamp(s, -1) == "2026-07-07T10:00:00Z"  # clamped to first turn
    assert _turn_timestamp(None, 0) is None
    assert _turn_timestamp(s, None) is None  # no turn index


# ---------------------------------------------------------------------------
# first_user_message capture at refresh time
# ---------------------------------------------------------------------------


@pytest.fixture
def co_env(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    db_file = tmp_path / "co.db"

    from work_buddy.sessions import inspector

    monkeypatch.setattr(inspector, "_CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db._default_db_path", lambda: db_file
    )
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db.db_path", lambda cfg=None: db_file
    )
    inspector._commit_cache.clear()
    return {"projects": projects}


def test_refresh_captures_first_user_message(co_env):
    from work_buddy.conversation_observability.sessions import (
        query_observed_session,
        refresh_observed_sessions,
    )

    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=[
            user_turn("Please do the thing", "2026-05-13T10:00:00Z"),
            assistant_text("Done", "2026-05-13T10:00:01Z"),
        ],
    )

    refresh_observed_sessions(days=3650)
    row = query_observed_session(sid)
    assert row is not None
    assert row["first_user_message"] == "Please do the thing"
