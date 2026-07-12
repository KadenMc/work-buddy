"""Conversation-time windowing.

Covers the transcript-provider window plumbing (mtime floor +
discover_sessions pass-through) and the observed-session recency contract:
recency is measured by *conversation* time, not row-refresh time, so a resumed
old session does not resurface as "recent".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

UTC = timezone.utc


# ---------------------------------------------------------------------------
# mtime_floor
# ---------------------------------------------------------------------------


def test_mtime_floor_semantics():
    import time

    from work_buddy.transcripts.models import mtime_floor

    assert mtime_floor(0, None) == 0.0  # days<=0 = all history
    assert mtime_floor(None, None) == 0.0  # unspecified = all history
    since = datetime(2026, 7, 7, tzinfo=UTC)
    assert mtime_floor(None, since) == since.timestamp()  # since is authoritative
    # days>0 resolves to a floor of `now - days`.
    floor = mtime_floor(2, None)
    assert time.time() - 2 * 86400 - 5 <= floor <= time.time() - 2 * 86400 + 5


# ---------------------------------------------------------------------------
# discover_sessions passes the window down to providers
# ---------------------------------------------------------------------------


def test_discover_sessions_passes_window_to_providers():
    from work_buddy.transcripts import registry

    captured: dict = {}

    class StubProvider:
        id = "stub-window"
        harness_id = "stub-window"
        label = "Stub"

        def discover(self, *, days=None, since=None, until=None, project_filter=None):
            captured.update(days=days, since=since, until=until)
            return []

        def session_from_path(self, path):  # pragma: no cover - unused here
            return None

        def iter_turns(self, session):  # pragma: no cover - unused here
            return []

        def iter_tool_calls(self, session):  # pragma: no cover - unused here
            return []

    registry.register_provider(StubProvider(), replace=True)
    try:
        since = datetime(2026, 7, 7, tzinfo=UTC)
        until = datetime(2026, 7, 8, tzinfo=UTC)
        registry.discover_sessions(since=since, until=until, provider_ids=["stub-window"])
        assert captured == {"days": None, "since": since, "until": until}
    finally:
        registry._PROVIDERS.pop("stub-window", None)


# ---------------------------------------------------------------------------
# list_observed_sessions — conversation-time filtering
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


def _insert(sid, *, start, end, observed_at, project="alpha"):
    from work_buddy.conversation_observability.db import get_connection

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO observed_sessions "
            "(session_id, project_name, source_path, source_mtime, observed_at, "
            " start_time, end_time, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'ok')",
            (sid, project, f"/x/{sid}.jsonl", 0.0, observed_at, start, end),
        )
        conn.commit()
    finally:
        conn.close()


def test_recency_by_conversation_time_not_observed_at(co_db):
    # THE regression: a months-old conversation re-observed just now (its file
    # was touched) must NOT resurface in a days=7 window.
    from work_buddy.conversation_observability.sessions import list_observed_sessions

    now = datetime.now(UTC)
    fresh_observed = now.isoformat()
    old = (now - timedelta(days=60)).isoformat()
    recent = (now - timedelta(hours=3)).isoformat()

    _insert("s-old", start=old, end=old, observed_at=fresh_observed)
    _insert("s-recent", start=recent, end=recent, observed_at=fresh_observed)

    got = {r["session_id"] for r in list_observed_sessions(days=7)}
    assert "s-recent" in got
    assert "s-old" not in got


def test_since_until_overlap_filtering(co_db):
    from work_buddy.conversation_observability.sessions import list_observed_sessions

    since = datetime(2026, 7, 7, 10, 40, tzinfo=UTC)
    until = datetime(2026, 7, 8, 5, 0, tzinfo=UTC)
    now_iso = datetime.now(UTC).isoformat()

    # Stored timestamps use the Z suffix (as Claude Code emits) while the bounds
    # are +00:00 datetimes — exercising the parse-then-compare path.
    _insert("span", start="2026-07-07T09:00:00Z", end="2026-07-07T12:00:00Z", observed_at=now_iso)
    _insert("before", start="2026-07-06T00:00:00Z", end="2026-07-06T02:00:00Z", observed_at=now_iso)
    _insert("after", start="2026-07-08T09:00:00Z", end="2026-07-08T10:00:00Z", observed_at=now_iso)
    _insert("nullend", start="2026-07-07T11:00:00Z", end=None, observed_at=now_iso)

    got = {r["session_id"] for r in list_observed_sessions(since=since, until=until)}
    # 'span' straddles the start boundary (overlap), 'nullend' is open-ended.
    assert got == {"span", "nullend"}


def test_project_filter_still_applies(co_db):
    from work_buddy.conversation_observability.sessions import list_observed_sessions

    now_iso = datetime.now(UTC).isoformat()
    _insert("a1", start=now_iso, end=now_iso, observed_at=now_iso, project="alpha")
    _insert("b1", start=now_iso, end=now_iso, observed_at=now_iso, project="beta")

    got = {r["session_id"] for r in list_observed_sessions(project="alpha")}
    assert got == {"a1"}


# ---------------------------------------------------------------------------
# css collector forwards the window to list_observed_sessions
# ---------------------------------------------------------------------------


def test_css_collector_forwards_window(monkeypatch):
    import work_buddy.conversation_observability.sessions as sessions_mod

    captured: dict = {}

    def fake_list(days=None, project=None, since=None, until=None):
        captured.update(since=since, until=until)
        return []  # empty -> collector short-circuits before the DB prefetch

    monkeypatch.setattr(sessions_mod, "list_observed_sessions", fake_list)

    from work_buddy.collectors import agent_session_summary_collector as css

    css.collect(
        {
            "since": "2026-07-07T10:40:00",
            "until": "2026-07-08T05:00:00",
            "refresh": False,
        }
    )
    assert captured["since"] is not None and captured["since"].tzinfo is not None
    assert captured["until"] is not None and captured["until"].tzinfo is not None
