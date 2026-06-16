"""Render contract for the claude_session_summary context source."""

from __future__ import annotations

from pathlib import Path

import pytest
from freezegun import freeze_time

from tests.unit.conversation_observability_fixtures import (
    assistant_text,
    assistant_write,
    commit_scenario,
    user_turn,
    write_scenario,
    write_session,
)


# ---------------------------------------------------------------------------
# Frozen clock — the fixtures below carry fixed timestamps (2026-05-13 .. 2026-05-27)
# and the collector windows them against "now" (days=7/30). Pin "now" just past the
# latest fixture so every window is deterministic and no test can expire as the real
# clock advances. New dated fixtures must sit at or before this anchor.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _frozen_now():
    with freeze_time("2026-05-28T12:00:00Z"):
        yield


# ---------------------------------------------------------------------------
# Fixture: isolated env, real DB at tmp path
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

    # Fake repos_root with one repo for the writes refresh.
    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    repo = repos_root / "alpha"
    repo.mkdir()
    (repo / ".git").mkdir()

    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"repos_root": str(repos_root)},
    )
    # No dirty files by default.
    monkeypatch.setattr(
        "work_buddy.collectors.git_collector._get_status",
        lambda repo_path: "",
    )

    return {"projects": projects, "db": db_file, "repos_root": repos_root, "repo": repo}


# ---------------------------------------------------------------------------
# Empty-state
# ---------------------------------------------------------------------------


def test_collector_empty_state_when_no_sessions(co_env) -> None:
    from work_buddy.collectors.claude_session_summary_collector import collect

    out = collect({"days": 7})
    assert "Claude Session Summary" in out
    # Empty marker present (rather than a session list).
    assert "_" in out


# ---------------------------------------------------------------------------
# Happy path — one session, one commit, one uncommitted file
# ---------------------------------------------------------------------------


def test_collector_renders_session_with_commit(co_env) -> None:
    from work_buddy.collectors.claude_session_summary_collector import collect

    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(
            sid, commit_hash="deadbee123", message="Fix the thing",
        ),
    )

    out = collect({"days": 30})

    assert "### alpha" in out
    assert sid[:8] in out
    assert "1 commit" in out
    assert "deadbee" in out
    assert "Fix the thing" in out


def test_collector_renders_uncommitted_files(co_env, monkeypatch) -> None:
    from work_buddy.collectors.claude_session_summary_collector import collect

    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    file_a = co_env["repo"] / "leftover.py"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(sid, files=[str(file_a)]),
    )

    # leftover.py shows as dirty.
    monkeypatch.setattr(
        "work_buddy.collectors.git_collector._get_status",
        lambda repo_path: " M leftover.py\n" if repo_path.name == "alpha" else "",
    )

    out = collect({"days": 30})
    assert "1 uncommitted file" in out
    assert "leftover.py" in out


def test_collector_groups_by_project(co_env) -> None:
    from work_buddy.collectors.claude_session_summary_collector import collect

    write_session(
        co_env["projects"] / "alpha",
        session_id="11111111-1111-1111-1111-111111111111",
        entries=[
            user_turn("a", "2026-05-13T09:00:00Z"),
            assistant_text("a-reply", "2026-05-13T09:00:01Z"),
        ],
    )
    write_session(
        co_env["projects"] / "beta",
        session_id="22222222-2222-2222-2222-222222222222",
        entries=[
            user_turn("b", "2026-05-13T09:30:00Z"),
            assistant_text("b-reply", "2026-05-13T09:30:01Z"),
        ],
    )

    out = collect({"days": 30})
    assert "### alpha" in out
    assert "### beta" in out


# ---------------------------------------------------------------------------
# Timezone — session times render in the user's local zone
# ---------------------------------------------------------------------------


def test_format_time_range_renders_local(monkeypatch) -> None:
    """_format_time_range converts UTC message timestamps to local wall-clock."""
    from zoneinfo import ZoneInfo

    import work_buddy.config as config
    from work_buddy.collectors.claude_session_summary_collector import (
        _format_time_range,
    )

    monkeypatch.setattr(config, "_USER_TZ_CACHE", ZoneInfo("America/Toronto"))

    # 10:42–11:31 UTC → 06:42–07:31 local (UTC-4).
    assert (
        _format_time_range("2026-05-27T10:42:00Z", "2026-05-27T11:31:00Z")
        == "2026-05-27 06:42–07:31"
    )
    # Both endpoints absent → the "—" sentinel.
    assert _format_time_range(None, None) == "—"


# ---------------------------------------------------------------------------
# Context source registration
# ---------------------------------------------------------------------------


def test_collector_include_topics_renders_topic_timeline(co_env, tmp_path, monkeypatch) -> None:
    """v2 P6: with `include_topics=True`, each session bullet nests its topic timeline."""
    from work_buddy.collectors.claude_session_summary_collector import collect

    # Point summarization DB at a temp file too (separate from conv_obs DB).
    summ_db = tmp_path / "summarization.db"
    monkeypatch.setattr(
        "work_buddy.summarization.db._default_db_path",
        lambda: summ_db,
    )
    monkeypatch.setattr(
        "work_buddy.summarization.db.db_path",
        lambda cfg=None: summ_db,
    )

    # Write one observed session.
    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=[user_turn("hello", "2026-05-27T10:00:00Z"), assistant_text("world", "2026-05-27T10:00:01Z")],
    )

    # Manually save a v2 summary with topics via the store.
    from work_buddy.summarization.protocol import Provenance, SummaryNode
    from work_buddy.summarization.stores import DurableSummaryStore

    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    prov = Provenance(
        model="m", backend="b", profile="p",
        generated_at=Provenance.now_iso(),
        prompt_version=1, summary_schema_version=1,
        selection_version=1, cache_version=1,
    )
    tree = SummaryNode(
        summary="Worked on test feature.",
        children=[
            SummaryNode(
                summary="Started the discussion.",
                source_ref={"span_start": 0, "span_end": 5},
                extra={
                    "title": "Discussion start",
                    "span_start": 0, "span_end": 5,
                    "turn_start": 0, "turn_end": 5,
                    "topic_index": 0,
                },
            ),
            SummaryNode(
                summary="Implemented it.",
                source_ref={"span_start": 6, "span_end": 12},
                extra={
                    "title": "Implementation",
                    "span_start": 6, "span_end": 12,
                    "turn_start": 6, "turn_end": 12,
                    "topic_index": 1,
                },
            ),
        ],
        extra={"activity_kind": "implementation"},
    )
    store.save(sid, tree, prov, "tok-test")

    out = collect({"days": 7, "include_topics": True})

    # tldr present
    assert "tldr: Worked on test feature." in out
    # Topics header present
    assert "Topics:" in out
    # Both titles rendered with ranges
    assert "Discussion start" in out
    assert "turns 0-5" in out
    assert "Implementation" in out
    assert "turns 6-12" in out


def test_collector_include_topics_implies_include_tldr(co_env, tmp_path, monkeypatch) -> None:
    """include_topics=True forces include_tldr=True for rendering consistency."""
    from work_buddy.collectors import claude_session_summary_collector as mod

    # Sanity: the inside-collect logic flips include_tldr when include_topics is set.
    # We assert this via behavior: passing include_topics=True (without explicit
    # include_tldr) still gets us tldr rendering.
    summ_db = tmp_path / "summarization.db"
    monkeypatch.setattr(
        "work_buddy.summarization.db._default_db_path",
        lambda: summ_db,
    )
    monkeypatch.setattr(
        "work_buddy.summarization.db.db_path",
        lambda cfg=None: summ_db,
    )

    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=[user_turn("topic-only test", "2026-05-27T10:00:00Z")],
    )

    from work_buddy.summarization.protocol import Provenance, SummaryNode
    from work_buddy.summarization.stores import DurableSummaryStore

    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    prov = Provenance(
        model="m", backend="b", profile="p",
        generated_at=Provenance.now_iso(),
        prompt_version=1, summary_schema_version=1,
        selection_version=1, cache_version=1,
    )
    store.save(sid, SummaryNode(summary="A tldr line."), prov, "tok-test")

    # include_topics=True; include_tldr unset.
    out = mod.collect({"days": 7, "include_topics": True})
    assert "tldr: A tldr line." in out


def test_context_source_registered_under_expected_name() -> None:
    from work_buddy.context.registry import get

    # Force import to trigger registration.
    import work_buddy.context.sources  # noqa: F401

    source = get("claude_session_summary")
    assert source is not None
    assert source.name == "claude_session_summary"
