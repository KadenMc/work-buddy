"""Render contract for the claude_session_summary context source."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.conversation_observability_fixtures import (
    assistant_text,
    assistant_write,
    commit_scenario,
    user_turn,
    write_scenario,
    write_session,
)


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
# Context source registration
# ---------------------------------------------------------------------------


def test_context_source_registered_under_expected_name() -> None:
    from work_buddy.context.registry import get

    # Force import to trigger registration.
    import work_buddy.context.sources  # noqa: F401

    source = get("claude_session_summary")
    assert source is not None
    assert source.name == "claude_session_summary"
