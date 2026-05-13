"""Refresh + report semantics for conversation_observability.writes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.unit.conversation_observability_fixtures import (
    assistant_write,
    user_turn,
    write_scenario,
    write_session,
)


# ---------------------------------------------------------------------------
# Fixture: stub the git-collector helpers so the tests don't depend on
# the real repos_root or on git being installed.
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

    # Set up a fake repos_root that is also iterated by _discover_repos.
    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    repo_alpha = repos_root / "alpha"
    repo_alpha.mkdir()
    (repo_alpha / ".git").mkdir()  # make _discover_repos pick it up

    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"repos_root": str(repos_root)},
    )

    return {
        "projects": projects,
        "db": db_file,
        "repos_root": repos_root,
        "repo": repo_alpha,
    }


@pytest.fixture
def stub_git(monkeypatch):
    """Capture/inject what ``_get_status`` returns per repo."""
    state: dict[str, str] = {}

    def fake_status(repo_path: Path) -> str:
        return state.get(repo_path.name, "")

    monkeypatch.setattr(
        "work_buddy.collectors.git_collector._get_status", fake_status,
    )
    # Yield the mutable dict so individual tests can populate it.
    return state


# ---------------------------------------------------------------------------
# refresh_session_writes — DB population
# ---------------------------------------------------------------------------


def test_refresh_writes_persists_rows_per_session_file(co_env, stub_git) -> None:
    from work_buddy.conversation_observability.writes import (
        query_session_writes,
        refresh_session_writes,
    )

    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(
            sid,
            files=[
                str(co_env["repo"] / "a.py"),
                str(co_env["repo"] / "b.py"),
            ],
        ),
    )

    refresh_session_writes(days=30, repos_root=co_env["repos_root"])

    rows = query_session_writes(session_id=sid)
    paths = {Path(r["file_path"]).name for r in rows}
    assert paths == {"a.py", "b.py"}
    # Tool name is preserved (Write in the scenario).
    assert all(r["tool_name"] == "Write" for r in rows)


def test_refresh_marks_dirty_files_based_on_git_status(
    co_env, stub_git,
) -> None:
    from work_buddy.conversation_observability.writes import (
        query_session_writes,
        refresh_session_writes,
    )

    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    file_a = co_env["repo"] / "a.py"
    file_b = co_env["repo"] / "b.py"

    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(sid, files=[str(file_a), str(file_b)]),
    )

    # Only a.py is dirty.
    stub_git["alpha"] = f" M a.py\n"

    refresh_session_writes(days=30, repos_root=co_env["repos_root"])

    rows = {Path(r["file_path"]).name: r for r in query_session_writes(sid)}
    assert rows["a.py"]["currently_dirty"] == 1
    assert rows["b.py"]["currently_dirty"] == 0


# ---------------------------------------------------------------------------
# uncommitted_report — legacy report shape
# ---------------------------------------------------------------------------


def test_uncommitted_report_returns_legacy_shape(co_env, stub_git) -> None:
    from work_buddy.conversation_observability.writes import uncommitted_report

    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    file_a = co_env["repo"] / "dirty.py"

    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(sid, files=[str(file_a)]),
    )
    stub_git["alpha"] = " M dirty.py\n"

    report = uncommitted_report(days=30, repos_root=co_env["repos_root"])

    assert report["uncommitted_count"] == 1
    entry = report["uncommitted"][0]
    assert entry["session_id"] == sid
    assert entry["repo"] == "alpha"
    assert entry["files"] == ["dirty.py"]
    assert entry["status"] == ["M"]
    # Top-level summary fields are present.
    assert "clean_sessions" in report
    assert "scanned_sessions" in report


def test_uncommitted_report_drops_files_no_longer_dirty(
    co_env, stub_git,
) -> None:
    from work_buddy.conversation_observability.writes import uncommitted_report

    sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    file_a = co_env["repo"] / "a.py"

    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(sid, files=[str(file_a)]),
    )
    # First call: dirty.
    stub_git["alpha"] = " M a.py\n"
    first = uncommitted_report(days=30, repos_root=co_env["repos_root"])
    assert first["uncommitted_count"] == 1

    # Now the file is clean (e.g. user reverted) — report should drop it.
    stub_git["alpha"] = ""
    second = uncommitted_report(days=30, repos_root=co_env["repos_root"])
    assert second["uncommitted_count"] == 0


def test_uncommitted_report_keeps_most_recent_writer_only(
    co_env, stub_git,
) -> None:
    """When two sessions write the same file, only the latest is blamed."""
    from work_buddy.conversation_observability.writes import uncommitted_report

    file_x = co_env["repo"] / "shared.py"
    sid_old = "00000000-0000-0000-0000-000000000001"
    sid_new = "00000000-0000-0000-0000-000000000002"

    # Older session: writes at 09:00.
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid_old,
        entries=[
            user_turn("old", "2026-05-13T09:00:00Z"),
            assistant_write(
                "Write", str(file_x), "tu_o", "2026-05-13T09:00:01Z",
            ),
        ],
    )
    # Newer session: writes at 10:00.
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid_new,
        entries=[
            user_turn("new", "2026-05-13T10:00:00Z"),
            assistant_write(
                "Edit", str(file_x), "tu_n", "2026-05-13T10:00:01Z",
            ),
        ],
    )

    stub_git["alpha"] = " M shared.py\n"
    report = uncommitted_report(days=30, repos_root=co_env["repos_root"])

    assert report["uncommitted_count"] == 1
    assert report["uncommitted"][0]["session_id"] == sid_new


# ---------------------------------------------------------------------------
# Legacy compat wrapper continues to work
# ---------------------------------------------------------------------------


def test_inspector_session_uncommitted_wrapper_returns_same_shape(
    co_env, stub_git,
) -> None:
    from work_buddy.sessions.inspector import session_uncommitted

    sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    file_a = co_env["repo"] / "wrap.py"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(sid, files=[str(file_a)]),
    )
    stub_git["alpha"] = " M wrap.py\n"

    report = session_uncommitted(days=30)
    assert report["uncommitted_count"] == 1
    assert report["uncommitted"][0]["session_id"] == sid
    assert report["uncommitted"][0]["files"] == ["wrap.py"]
