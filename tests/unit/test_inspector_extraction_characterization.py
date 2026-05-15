"""Characterization tests for session-derived extraction in sessions/inspector.

Pins the observable shapes of ``session_commits``, ``build_session_map``,
``_extract_writes_from_jsonl``, and ``session_uncommitted`` so the
upcoming migration into ``work_buddy.conversation_observability`` can
preserve callers (``GitSource``, journal directions, the ``session_*``
gateway capabilities) without behavior drift.

These tests describe the *current* contract. They are not specifications
of what those APIs should ideally look like — they are a snapshot of
what other code in the repo today depends on. When the migration moves
the logic, the compatibility wrappers must continue to satisfy these
tests; the wrappers can be retired only when no caller relies on the
old surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.conversation_observability_fixtures import (
    assistant_bash,
    assistant_text,
    assistant_write,
    commit_scenario,
    tool_result,
    user_turn,
    write_scenario,
    write_session,
)


# ---------------------------------------------------------------------------
# Fixtures: redirect ~/.claude/projects/ to a temp dir
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_projects(tmp_path, monkeypatch):
    """Point inspector._CLAUDE_PROJECTS + conversation_observability DB at
    temp paths, and clear in-process caches.

    The DB redirection is needed because the inspector now delegates
    to ``conversation_observability.commits`` which reads/writes a
    durable SQLite store. Without isolation, fixtures from one test
    persist into the next.
    """
    projects = tmp_path / "projects"
    projects.mkdir()
    co_db = tmp_path / "co.db"

    from work_buddy.sessions import inspector

    monkeypatch.setattr(inspector, "_CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db._default_db_path",
        lambda: co_db,
    )
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db.db_path",
        lambda cfg=None: co_db,
    )
    # Bust the process-local commit cache between tests so prior runs
    # don't bleed in.
    inspector._commit_cache.clear()
    return projects


# ---------------------------------------------------------------------------
# session_commits — shape, sort order, message_index, multi-commit
# ---------------------------------------------------------------------------


def test_session_commits_extracts_single_commit_with_expected_fields(
    fake_projects,
) -> None:
    from work_buddy.sessions.inspector import session_commits

    project = fake_projects / "project_a"
    write_session(
        project,
        session_id="11111111-1111-1111-1111-111111111111",
        entries=commit_scenario(
            session_id="11111111-1111-1111-1111-111111111111",
            commit_hash="abc1234",
            branch="main",
            message="Add feature X",
            files_changed=3,
        ),
    )

    result = session_commits(days=30)

    assert result["commit_count"] == 1
    commit = result["commits"][0]
    assert commit["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert commit["hash"] == "abc1234"
    assert commit["branch"] == "main"
    assert commit["message"] == "Add feature X"
    assert commit["files_changed"] == 3
    # message_index is the turn index (in iter_session_turns order) of
    # the assistant message that issued the commit. Scenario turns:
    # 0=user, 1=assistant_text, 2=user, 3=assistant_bash.
    assert commit["message_index"] == 3
    assert commit["timestamp"]  # iso string present


def test_session_commits_sorts_newest_first(fake_projects) -> None:
    from work_buddy.sessions.inspector import session_commits

    project = fake_projects / "project_a"
    # Two sessions; the older session's commit timestamp is earlier.
    write_session(
        project,
        session_id="22222222-2222-2222-2222-222222222222",
        entries=[
            user_turn("old work", "2026-05-01T09:00:00Z"),
            assistant_bash(
                "git commit -m 'old'", "tu_o", "2026-05-01T09:00:01Z"
            ),
            tool_result(
                "tu_o", "[main 1111111] old\n 1 file changed",
                "2026-05-01T09:00:02Z",
            ),
        ],
    )
    write_session(
        project,
        session_id="33333333-3333-3333-3333-333333333333",
        entries=[
            user_turn("recent work", "2026-05-12T15:00:00Z"),
            assistant_bash(
                "git commit -m 'recent'", "tu_r", "2026-05-12T15:00:01Z"
            ),
            tool_result(
                "tu_r", "[main 2222222] recent\n 2 files changed",
                "2026-05-12T15:00:02Z",
            ),
        ],
    )

    result = session_commits(days=365)
    hashes = [c["hash"] for c in result["commits"]]
    assert hashes == ["2222222", "1111111"]


def test_session_commits_skips_failed_tool_result(fake_projects) -> None:
    """A tool_result with is_error=True must not produce a commit row."""
    from work_buddy.sessions.inspector import session_commits

    project = fake_projects / "project_a"
    write_session(
        project,
        session_id="44444444-4444-4444-4444-444444444444",
        entries=[
            user_turn("commit it", "2026-05-13T10:00:00Z"),
            assistant_bash(
                "git commit -m 'broken'", "tu_e", "2026-05-13T10:00:01Z"
            ),
            tool_result(
                "tu_e", "", "2026-05-13T10:00:02Z", is_error=True
            ),
        ],
    )

    result = session_commits(days=30)
    assert result["commit_count"] == 0


def test_session_commits_project_filter(fake_projects, monkeypatch) -> None:
    """Passing ``project=`` should only scan the matching project dir."""
    from work_buddy.sessions.inspector import session_commits

    # Use real project_name_from_slug — sanitize the slug to match
    monkeypatch.setattr(
        "work_buddy.collectors.chat_collector.project_name_from_slug",
        lambda name: name.removeprefix("project_"),
    )

    write_session(
        fake_projects / "project_alpha",
        session_id="55555555-5555-5555-5555-555555555555",
        entries=commit_scenario(
            "55555555-5555-5555-5555-555555555555",
            commit_hash="aaaaaaa",
        ),
    )
    write_session(
        fake_projects / "project_beta",
        session_id="66666666-6666-6666-6666-666666666666",
        entries=commit_scenario(
            "66666666-6666-6666-6666-666666666666",
            commit_hash="bbbbbbb",
        ),
    )

    result = session_commits(days=30, project="alpha")
    hashes = {c["hash"] for c in result["commits"]}
    assert hashes == {"aaaaaaa"}


# ---------------------------------------------------------------------------
# build_session_map — 7-char hash keys, full session_id values
# ---------------------------------------------------------------------------


def test_build_session_map_returns_7char_short_hash_to_full_sid(
    fake_projects,
) -> None:
    from work_buddy.sessions.inspector import build_session_map

    sid = "77777777-7777-7777-7777-777777777777"
    write_session(
        fake_projects / "project_x",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="deadbeef1234"),
    )

    mapping = build_session_map(days=30)
    # Keys are the 7-char short hash GitSource consumes. Values are the
    # full session UUID; consumers truncate at display time.
    assert "deadbee" in mapping
    assert mapping["deadbee"] == sid


# ---------------------------------------------------------------------------
# _extract_writes_from_jsonl — tool support and latest-timestamp dedup
# ---------------------------------------------------------------------------


def test_extract_writes_picks_up_write_edit_notebookedit(
    fake_projects,
) -> None:
    from work_buddy.sessions.inspector import _extract_writes_from_jsonl

    session_path = write_session(
        fake_projects / "project_w",
        session_id="88888888-8888-8888-8888-888888888888",
        entries=[
            user_turn("do writes", "2026-05-13T12:00:00Z"),
            assistant_write("Write", "/repo/a.py", "tu_w", "2026-05-13T12:00:01Z"),
            assistant_write("Edit",  "/repo/b.py", "tu_e", "2026-05-13T12:00:02Z"),
            assistant_write(
                "NotebookEdit", "/repo/c.ipynb", "tu_n",
                "2026-05-13T12:00:03Z",
            ),
        ],
    )

    writes = _extract_writes_from_jsonl(session_path)
    resolved_paths = {p.as_posix() for p in writes.keys()}
    # Paths are resolved via Path.resolve() — on Windows that may
    # canonicalize separators or case. Use endswith to stay portable.
    assert any(p.endswith("/a.py") for p in resolved_paths)
    assert any(p.endswith("/b.py") for p in resolved_paths)
    assert any(p.endswith("/c.ipynb") for p in resolved_paths)


def test_extract_writes_keeps_latest_timestamp_per_file(
    fake_projects,
) -> None:
    from work_buddy.sessions.inspector import _extract_writes_from_jsonl

    session_path = write_session(
        fake_projects / "project_w",
        session_id="99999999-9999-9999-9999-999999999999",
        entries=[
            user_turn("write", "2026-05-13T13:00:00Z"),
            assistant_write("Write", "/repo/dup.py", "tu1", "2026-05-13T13:00:01Z"),
            assistant_write("Edit",  "/repo/dup.py", "tu2", "2026-05-13T13:00:05Z"),
            assistant_write("Edit",  "/repo/dup.py", "tu3", "2026-05-13T13:00:03Z"),
        ],
    )

    writes = _extract_writes_from_jsonl(session_path)
    # Single file. The kept timestamp is the latest, regardless of
    # encounter order.
    assert len(writes) == 1
    ts = next(iter(writes.values()))
    assert ts == "2026-05-13T13:00:05Z"


# ---------------------------------------------------------------------------
# Empty-state behavior
# ---------------------------------------------------------------------------


def test_session_commits_no_sessions_returns_zero(fake_projects) -> None:
    from work_buddy.sessions.inspector import session_commits

    result = session_commits(days=7)
    assert result == {"commit_count": 0, "commits": []}


def test_build_session_map_no_sessions_returns_empty(fake_projects) -> None:
    from work_buddy.sessions.inspector import build_session_map

    assert build_session_map(days=7) == {}
