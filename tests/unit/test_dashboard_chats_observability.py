"""Dashboard ``get_chats_summary`` enriched with conversation_observability.

Pins the contract of ``_load_observability_for_sessions`` and the new
fields appended to each chat dict by ``get_chats_summary``.
"""

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
# Fixture: isolated env for both inspector + observability
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

    # Redirect the task store to a temp DB so the dashboard's
    # session→tasks aggregation (and these tests) stay hermetic — the
    # load_config stub below drops the ``tasks`` key, which would
    # otherwise fall through to the real tasks DB.
    from work_buddy.obsidian.tasks import store as _task_store

    monkeypatch.setattr(_task_store, "_db_path", lambda: tmp_path / "tasks.db")

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
    # Stub the GitHub PR-title/state enrichment so tests stay hermetic and
    # offline (no real `gh pr list` subprocess).
    monkeypatch.setattr(
        "work_buddy.dashboard.api._load_pr_meta_for_repos",
        lambda repos: {},
    )
    return {"projects": projects, "db": db_file, "repos_root": repos_root, "repo": repo}


# ---------------------------------------------------------------------------
# _load_observability_for_sessions — direct contract
# ---------------------------------------------------------------------------


def test_load_observability_returns_empty_for_empty_session_set(co_env) -> None:
    from work_buddy.dashboard.api import _load_observability_for_sessions

    assert _load_observability_for_sessions(set()) == {}


def test_load_observability_chat_only_session_marks_engages_git_false(
    co_env,
) -> None:
    """A session with no commits and no Write/Edit/NotebookEdit calls
    must have ``engages_git=False`` so the dashboard hides the badges.
    """
    from work_buddy.conversation_observability.sessions import (
        refresh_observed_sessions,
    )
    from work_buddy.dashboard.api import _load_observability_for_sessions

    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=[
            user_turn("hello", "2026-05-13T10:00:00Z"),
            assistant_text("hi back", "2026-05-13T10:00:01Z"),
        ],
    )
    refresh_observed_sessions(days=30)

    obs = _load_observability_for_sessions({sid})
    assert obs[sid]["engages_git"] is False
    assert obs[sid]["commit_count"] == 0
    assert obs[sid]["unfinished_count"] == 0


def test_load_observability_session_with_commits_marks_engages_git_true(
    co_env,
) -> None:
    from work_buddy.conversation_observability.commits import (
        refresh_session_commits,
    )
    from work_buddy.dashboard.api import _load_observability_for_sessions

    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="abc1234"),
    )
    refresh_session_commits(days=30)

    obs = _load_observability_for_sessions({sid})
    assert obs[sid]["engages_git"] is True
    assert obs[sid]["commit_count"] == 1


def test_load_observability_unfinished_count_uses_historical_signal(
    co_env, monkeypatch,
) -> None:
    """The badge counts files this session wrote that this session did
    not commit itself. Stable: doesn't depend on what other agents did
    later, doesn't depend on current ``git status``.
    """
    from work_buddy.conversation_observability.writes import (
        refresh_session_writes,
    )
    from work_buddy.dashboard.api import _load_observability_for_sessions

    # Stub _committed_files_for_sessions to keep the test hermetic
    # (no real git-show subprocesses against tmp repos).
    monkeypatch.setattr(
        "work_buddy.conversation_observability.writes._committed_files_for_sessions",
        lambda sids, repos_root: {sid: set() for sid in sids},
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.api._load_observability_for_sessions.__globals__"
        "['_committed_files_for_sessions']"
        if False else "work_buddy.conversation_observability.writes._committed_files_for_sessions",
        lambda sids, repos_root: {sid: set() for sid in sids},
    )

    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    file_a = co_env["repo"] / "leftover.py"
    file_b = co_env["repo"] / "another.py"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(sid, files=[str(file_a), str(file_b)]),
    )
    refresh_session_writes(days=30, repos_root=co_env["repos_root"])

    obs = _load_observability_for_sessions({sid})
    # Two files written, neither committed by this session.
    assert obs[sid]["unfinished_count"] == 2
    assert obs[sid]["engages_git"] is True


def test_load_observability_unfinished_drops_files_session_committed(
    co_env, monkeypatch,
) -> None:
    """A file the session itself committed should NOT count toward the
    unfinished badge.
    """
    from work_buddy.conversation_observability.writes import (
        refresh_session_writes,
    )
    from work_buddy.dashboard.api import _load_observability_for_sessions

    sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    file_committed = co_env["repo"] / "shipped.py"
    file_uncommitted = co_env["repo"] / "still-dirty.py"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=write_scenario(
            sid, files=[str(file_committed), str(file_uncommitted)],
        ),
    )
    refresh_session_writes(days=30, repos_root=co_env["repos_root"])

    # Stub the per-session committed-files map: this session committed
    # ``shipped.py`` (its absolute path) but not the other.
    committed_path = file_committed.resolve().as_posix()
    monkeypatch.setattr(
        "work_buddy.conversation_observability.writes._committed_files_for_sessions",
        lambda sids, repos_root: {sid: {committed_path}},
    )

    obs = _load_observability_for_sessions({sid})
    assert obs[sid]["unfinished_count"] == 1


def test_load_observability_commits_by_repo_aggregates(co_env) -> None:
    """Multi-repo commits should be broken down per repo."""
    from work_buddy.conversation_observability.commits import (
        refresh_session_commits,
    )
    from work_buddy.conversation_observability.db import get_connection
    from work_buddy.dashboard.api import _load_observability_for_sessions

    sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="aaaaaaa"),
    )
    refresh_session_commits(days=30)

    # Manually stamp repo_name on the commit row so the aggregation
    # has something to group by (the parser doesn't infer repo).
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE session_commits SET repo_name = ? WHERE session_id = ?",
            ("electricrag", sid),
        )
        # Add a second commit attributed to a different repo.
        conn.execute(
            "INSERT INTO session_commits "
            "(sha, short_sha, session_id, repo_name, branch, message, "
            " files_changed, committed_at, message_index, observed_at) "
            "VALUES ('bbbbbbb', 'bbbbbbb', ?, 'work-buddy', 'main', "
            "'docs change', 1, '2026-05-14T10:00:00Z', 5, "
            "'2026-05-14T10:00:01Z')",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()

    obs = _load_observability_for_sessions({sid})
    assert obs[sid]["commit_count"] == 2
    assert obs[sid]["commits_by_repo"] == {
        "electricrag": 1,
        "work-buddy": 1,
    }
    assert obs[sid]["latest_committed_at"] == "2026-05-14T10:00:00Z"


def test_load_observability_tldr_only_when_status_ok(co_env, tmp_path, monkeypatch) -> None:
    """Errored summaries must not surface their (empty) tldr.

    Seeds the summarization framework store (`summary_items` +
    `summary_nodes` in `summarization.db`) directly — the legacy
    `session_summaries` table was dropped after the 2026-05-28 migration.
    """
    from work_buddy.conversation_observability.commits import (
        refresh_session_commits,
    )
    from work_buddy.dashboard.api import _load_observability_for_sessions

    # Redirect the summarization framework DB to a tmp file so we don't
    # touch the user's real `<data_root>/summarization/summarization.db`.
    summ_db = tmp_path / "summarization.db"
    monkeypatch.setattr(
        "work_buddy.summarization.db._default_db_path",
        lambda: summ_db,
    )
    monkeypatch.setattr(
        "work_buddy.summarization.db.db_path",
        lambda cfg=None: summ_db,
    )

    sid_ok = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    sid_err = "00000000-0000-0000-0000-000000000001"
    for sid in (sid_ok, sid_err):
        write_session(
            co_env["projects"] / "alpha",
            session_id=sid,
            entries=commit_scenario(sid, commit_hash=sid[:7]),
        )
    refresh_session_commits(days=30)

    # Seed framework rows directly. `_load_tldrs_from_framework` joins
    # status='ok' items against their level=0 root node — only the ok
    # session's tldr should surface.
    from work_buddy.summarization.db import get_connection as get_summ_conn

    sconn = get_summ_conn()
    try:
        for sid, tldr, status, error in (
            (sid_ok, "Built the thing.", "ok", None),
            (sid_err, "", "error", "llm timeout"),
        ):
            sconn.execute(
                "INSERT INTO summary_items "
                "(namespace, item_id, freshness_token, generated_at, "
                " prompt_version, summary_schema_version, selection_version, "
                " cache_version, status, error) "
                "VALUES ('conversation_session', ?, 'tok', '2026-05-14', "
                "        1, 1, 1, 1, ?, ?)",
                (sid, status, error),
            )
            sconn.execute(
                "INSERT INTO summary_nodes "
                "(id, namespace, item_id, parent_id, ordinal, level, summary) "
                "VALUES (?, 'conversation_session', ?, NULL, 0, 0, ?)",
                (f"conversation_session:{sid}:0", sid, tldr),
            )
        sconn.commit()
    finally:
        sconn.close()

    obs = _load_observability_for_sessions({sid_ok, sid_err})
    assert obs[sid_ok]["tldr"] == "Built the thing."
    assert obs[sid_err]["tldr"] is None


# ---------------------------------------------------------------------------
# get_chats_summary — end-to-end shape with new fields
# ---------------------------------------------------------------------------


def test_get_chats_summary_appends_observability_fields(co_env, monkeypatch) -> None:
    """Each chat dict gains the new fields; legacy fields preserved."""
    from work_buddy.conversation_observability.commits import (
        refresh_session_commits,
    )
    from work_buddy.dashboard.api import get_chats_summary

    sid = "11111111-2222-3333-4444-555555555555"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="abc1234"),
    )
    refresh_session_commits(days=30)

    # The chat_collector reads from a different shape — stub it so the
    # dashboard wrapper has something to enrich without depending on
    # the user's real ~/.claude/projects/ tree.
    monkeypatch.setattr(
        "work_buddy.collectors.chat_collector._get_claude_code_conversations",
        lambda days: [
            {
                "full_session_id": sid,
                "session_id": sid,
                "first_user_message": "first message",
                "user_msg_count": 2,
                "assistant_text_count": 2,
                "tool_use_count": 5,
                "tool_names": {"Bash": 3, "Edit": 2},
                "start_time": "2026-05-13T10:00:00Z",
                "end_time": "2026-05-13T10:30:00Z",
                "project_slug": "alpha",
                "project_name": "alpha",
            }
        ],
    )

    out = get_chats_summary(days=7)
    assert out["total"] == 1
    chat = out["chats"][0]

    # New fields populated.
    assert chat["commit_count"] == 1
    assert chat["unfinished_count"] == 0  # commit_scenario has no writes
    assert chat["engages_git"] is True
    assert "commits_by_repo" in chat
    assert "latest_committed_at" in chat

    # Legacy fields preserved.
    assert chat["session_id"] == sid
    assert chat["first_message"] == "first message"
    assert chat["message_count"] == 4
    assert chat["top_tools"] == ["Bash", "Edit"]


def test_load_observability_aggregates_prs_and_tasks(co_env) -> None:
    """PR-activity counts and task-assignment counts surface per session."""
    from tests.unit.conversation_observability_fixtures import (
        assistant_bash,
        tool_result,
        user_turn,
    )
    from work_buddy.conversation_observability.prs import refresh_session_prs
    from work_buddy.dashboard.api import _load_observability_for_sessions
    from work_buddy.obsidian.tasks import store

    sid = "abababab-abab-abab-abab-abababababab"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=[
            user_turn("open then merge", "2026-05-08T23:00:00Z"),
            assistant_bash(
                "gh pr create --fill", "tu_c", "2026-05-08T23:00:01Z",
            ),
            tool_result(
                "tu_c",
                "https://github.com/KadenMc/work-buddy/pull/200",
                "2026-05-08T23:00:02Z",
            ),
            assistant_bash(
                "gh pr merge https://github.com/KadenMc/work-buddy/pull/200",
                "tu_m", "2026-05-08T23:05:00Z",
            ),
            tool_result("tu_m", "Merged #200", "2026-05-08T23:05:01Z"),
        ],
    )
    refresh_session_prs(days=30)

    # Two task assignments for the same session.
    store.create(task_id="t-link1", description="First linked task")
    store.create(task_id="t-link2", description="Second linked task")
    store.assign_session("t-link1", sid)
    store.assign_session("t-link2", sid)

    obs = _load_observability_for_sessions({sid})
    assert obs[sid]["pr_authored_count"] == 1
    assert obs[sid]["pr_merged_count"] == 1
    assert {p["action"] for p in obs[sid]["prs_detail"]} == {"created", "merged"}
    assert obs[sid]["task_count"] == 2
    assert {t["task_id"] for t in obs[sid]["tasks_detail"]} == {
        "t-link1", "t-link2",
    }
