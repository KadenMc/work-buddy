"""LLM topic summaries for observed sessions.

All tests use a stub LLM caller — no real network calls. The contract
under test is the row-persistence + staleness logic, not the LLM
itself.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from tests.unit.conversation_observability_fixtures import (
    assistant_text,
    user_turn,
    write_session,
)


# ---------------------------------------------------------------------------
# Fixture: isolated env
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


def _basic_session(sid: str) -> list[dict[str, Any]]:
    return [
        user_turn("Help me write a parser", "2026-05-13T10:00:00Z"),
        assistant_text("Sure — here's a draft", "2026-05-13T10:00:05Z"),
        user_turn("Now refactor it for tests", "2026-05-13T10:01:00Z"),
        assistant_text("Refactored, tests added", "2026-05-13T10:01:05Z"),
    ]


def _stub_response(tldr: str = "Built and tested a parser.") -> dict[str, Any]:
    """Canned LLM response shape — what the schema validator would accept."""
    return {
        "tldr": tldr,
        "topic_summary": [
            {
                "title": "Initial parser draft",
                "summary": "Wrote a first version of the parser.",
                "span_range": [0, 1],
                "keywords": ["parser", "draft"],
            },
            {
                "title": "Refactor for tests",
                "summary": "Restructured into test-friendly modules.",
                "span_range": [1, 2],
                "keywords": ["refactor", "tests"],
            },
        ],
    }


# ---------------------------------------------------------------------------
# summarize_session — basic happy path
# ---------------------------------------------------------------------------


def test_summarize_session_persists_tldr_and_topics(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )
    from work_buddy.conversation_observability.summaries import (
        summarize_session,
        query_session_summary,
        query_topic_summaries,
    )

    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=_basic_session(sid),
    )
    refresh_observed_sessions(days=30)

    stub_calls: list[dict[str, Any]] = []

    def stub_llm(*, system, user, output_schema=None, profile=None):
        stub_calls.append({"system": system, "user": user})
        return _stub_response()

    result = summarize_session(session_id=sid, llm_call=stub_llm)
    assert result is not None
    assert result["tldr"] == "Built and tested a parser."
    assert result["topic_count"] == 2
    assert result["status"] == "ok"

    topics = query_topic_summaries(sid)
    assert len(topics) == 2
    assert topics[0]["title"] == "Initial parser draft"
    assert topics[0]["keywords"] == ["parser", "draft"]

    # LLM was actually called once.
    assert len(stub_calls) == 1


def test_summarize_session_returns_none_for_empty_session(co_env) -> None:
    from work_buddy.conversation_observability.summaries import summarize_session

    # Write an empty file — no usable turns.
    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    path = co_env["projects"] / "alpha" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")

    result = summarize_session(
        session_id=sid,
        llm_call=lambda **kw: _stub_response(),
    )
    assert result is None


def test_summarize_session_records_error_on_invalid_response(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )
    from work_buddy.conversation_observability.summaries import (
        summarize_session,
        query_session_summary,
    )

    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=_basic_session(sid),
    )
    refresh_observed_sessions(days=30)

    def bad_llm(**_kw):
        return "this is not valid json"

    result = summarize_session(session_id=sid, llm_call=bad_llm)
    assert result is None

    row = query_session_summary(sid)
    assert row is not None
    assert row["status"] == "error"
    assert row["error"]
    assert "valid json" in row["error"].lower() or "invalid llm response" in row["error"].lower()


def test_summarize_session_error_does_not_corrupt_prior_good_summary(
    co_env,
) -> None:
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )
    from work_buddy.conversation_observability.summaries import (
        summarize_session,
        query_session_summary,
    )

    sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=_basic_session(sid),
    )
    refresh_observed_sessions(days=30)

    # Phase 1: good summary lands.
    summarize_session(
        session_id=sid,
        llm_call=lambda **kw: _stub_response("First-good tldr"),
    )
    assert query_session_summary(sid)["tldr"] == "First-good tldr"

    # Phase 2: force re-summarize with an LLM that errors.
    summarize_session(
        session_id=sid,
        force=True,
        llm_call=lambda **kw: "broken",
    )

    # The prior tldr is preserved; only status flips to error.
    row = query_session_summary(sid)
    assert row["tldr"] == "First-good tldr"
    assert row["status"] == "error"


# ---------------------------------------------------------------------------
# Staleness — versions and file mtime
# ---------------------------------------------------------------------------


def test_summary_marked_stale_when_prompt_version_bumps(co_env) -> None:
    from work_buddy.conversation_observability import summaries as summary_mod
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )
    from work_buddy.conversation_observability.summaries import (
        refresh_session_summaries,
        summarize_session,
    )

    sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=_basic_session(sid),
    )
    refresh_observed_sessions(days=30)
    summarize_session(
        session_id=sid,
        llm_call=lambda **kw: _stub_response("v1 tldr"),
    )

    # First refresh: fresh — _select_candidates returns no stale work.
    r1 = refresh_session_summaries(
        days=30, llm_call=lambda **kw: _stub_response("v2 tldr"),
    )
    assert r1["summarized"] == 0
    assert r1["total_candidates"] == 0

    # Bump PROMPT_VERSION → that session becomes stale.
    original = summary_mod.PROMPT_VERSION
    try:
        summary_mod.PROMPT_VERSION = original + 1
        r2 = refresh_session_summaries(
            days=30, llm_call=lambda **kw: _stub_response("v2 tldr"),
        )
        assert r2["summarized"] >= 1
    finally:
        summary_mod.PROMPT_VERSION = original


def test_summary_marked_stale_when_source_file_changes(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )
    from work_buddy.conversation_observability.summaries import (
        refresh_session_summaries,
        summarize_session,
    )

    sid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    path = write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=_basic_session(sid),
    )
    refresh_observed_sessions(days=30)
    summarize_session(
        session_id=sid, llm_call=lambda **kw: _stub_response("first"),
    )

    # Bump mtime + re-observe so source_mtime updates in observed_sessions.
    new_mtime = time.time() + 30
    os.utime(path, (new_mtime, new_mtime))
    refresh_observed_sessions(days=30)

    result = refresh_session_summaries(
        days=30, llm_call=lambda **kw: _stub_response("second"),
    )
    assert result["summarized"] >= 1


# ---------------------------------------------------------------------------
# refresh_session_summaries — bounding
# ---------------------------------------------------------------------------


def test_refresh_session_summaries_caps_at_max_sessions(co_env) -> None:
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )
    from work_buddy.conversation_observability.summaries import (
        refresh_session_summaries,
    )

    for i in range(5):
        sid = f"11111111-1111-1111-1111-{i:012d}"
        write_session(
            co_env["projects"] / "alpha",
            session_id=sid,
            entries=_basic_session(sid),
        )
    refresh_observed_sessions(days=30)

    call_count = 0

    def counter_stub(**kw):
        nonlocal call_count
        call_count += 1
        return _stub_response()

    result = refresh_session_summaries(
        days=30, max_sessions=2, llm_call=counter_stub,
    )
    assert result["summarized"] == 2
    assert call_count == 2
