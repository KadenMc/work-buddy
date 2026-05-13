"""MCP-registered conversation_observability capabilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.conversation_observability_fixtures import (
    commit_scenario,
    write_scenario,
    write_session,
)


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

    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    repo = repos_root / "alpha"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"repos_root": str(repos_root)},
    )
    monkeypatch.setattr(
        "work_buddy.collectors.git_collector._get_status",
        lambda repo_path: "",
    )
    return {"projects": projects, "db": db_file, "repos_root": repos_root, "repo": repo}


def test_all_four_capabilities_register() -> None:
    from work_buddy.mcp_server.registry import (
        _conversation_observability_capabilities,
    )

    caps = _conversation_observability_capabilities()
    names = {c.name for c in caps}
    assert names == {
        "conversation_observability_refresh",
        "conversation_observability_uncommitted",
        "conversation_observability_get",
        "conversation_observability_list",
    }
    for cap in caps:
        assert cap.category == "conversation_observability"


def test_refresh_capability_runs_all_three_refreshers(co_env) -> None:
    from work_buddy.mcp_server.registry import (
        _conversation_observability_capabilities,
    )

    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="aaa1234"),
    )

    refresh_cap = next(
        c
        for c in _conversation_observability_capabilities()
        if c.name == "conversation_observability_refresh"
    )
    result = refresh_cap.callable(days=30)

    # Three keys in the result summary, each non-empty.
    assert "observed_sessions" in result
    assert "session_commits" in result
    assert "session_writes" in result
    assert result["observed_sessions"]["observed"] >= 1
    assert result["session_commits"]["commit_count"] >= 1


def test_get_capability_returns_session_record(co_env) -> None:
    from work_buddy.mcp_server.registry import (
        _conversation_observability_capabilities,
    )

    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="bbb1234"),
    )

    caps = {c.name: c for c in _conversation_observability_capabilities()}
    caps["conversation_observability_refresh"].callable(days=30)

    rec = caps["conversation_observability_get"].callable(session_id=sid)
    assert rec is not None
    assert rec["session_id"] == sid
    assert rec["status"] == "ok"


def test_get_capability_returns_none_for_unknown_session(co_env) -> None:
    from work_buddy.mcp_server.registry import (
        _conversation_observability_capabilities,
    )

    caps = {c.name: c for c in _conversation_observability_capabilities()}
    # No refresh, no sessions — but capability must still return cleanly.
    result = caps["conversation_observability_get"].callable(
        session_id="00000000-0000-0000-0000-000000000000",
    )
    assert result is None


def test_list_capability_filters_by_project(co_env) -> None:
    from work_buddy.mcp_server.registry import (
        _conversation_observability_capabilities,
    )

    sid_alpha = "11111111-1111-1111-1111-111111111111"
    sid_beta = "22222222-2222-2222-2222-222222222222"

    write_session(
        co_env["projects"] / "alpha",
        session_id=sid_alpha,
        entries=commit_scenario(sid_alpha, commit_hash="aaa0001"),
    )
    write_session(
        co_env["projects"] / "beta",
        session_id=sid_beta,
        entries=commit_scenario(sid_beta, commit_hash="bbb0002"),
    )

    caps = {c.name: c for c in _conversation_observability_capabilities()}
    caps["conversation_observability_refresh"].callable(days=30)

    alpha_only = caps["conversation_observability_list"].callable(project="alpha")
    assert len(alpha_only) == 1
    assert alpha_only[0]["project_name"] == "alpha"


def test_uncommitted_capability_returns_report(co_env, monkeypatch) -> None:
    from work_buddy.mcp_server.registry import (
        _conversation_observability_capabilities,
    )

    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    file_a = co_env["repo"] / "dirty.py"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(sid, files=[str(file_a)]),
    )
    monkeypatch.setattr(
        "work_buddy.collectors.git_collector._get_status",
        lambda repo_path: " M dirty.py\n" if repo_path.name == "alpha" else "",
    )

    cap = next(
        c
        for c in _conversation_observability_capabilities()
        if c.name == "conversation_observability_uncommitted"
    )
    report = cap.callable(days=30)
    assert report["uncommitted_count"] == 1
    assert report["uncommitted"][0]["session_id"] == sid
