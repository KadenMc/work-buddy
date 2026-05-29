"""Extraction + persistence semantics for session→PR linkage.

Exercises ``sessions.inspector._extract_prs_single_pass`` (structural
``gh pr`` detection) and ``conversation_observability.prs`` (stale-only
refresh + read-only query) against synthesized JSONL fixtures — no
dependence on the user's real ~/.claude/projects tree.
"""

from __future__ import annotations

import pytest

from tests.unit.conversation_observability_fixtures import (
    assistant_bash,
    assistant_text,
    tool_result,
    user_turn,
    write_session,
)


@pytest.fixture
def co_env(tmp_path, monkeypatch):
    """Isolated projects dir + DB, mirroring the commits-test fixture."""
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
    return {"projects": projects, "db": db_file}


def _pr_turns(command: str, stdout: str, *, tu: str = "tu_pr") -> list:
    """A 'do something, then run a gh pr command' two-call session."""
    return [
        user_turn("Please open/merge the PR.", "2026-05-08T23:00:00Z"),
        assistant_text("On it.", "2026-05-08T23:00:01Z"),
        assistant_bash(
            command=command,
            tool_use_id=tu,
            timestamp="2026-05-08T23:00:02Z",
            accompanying_text="Running gh.",
        ),
        tool_result(tu, stdout=stdout, timestamp="2026-05-08T23:00:03Z"),
    ]


def _extract(co_env, sid, entries):
    from work_buddy.sessions.inspector import _extract_prs_single_pass

    path = write_session(co_env["projects"] / "alpha", sid, entries)
    return _extract_prs_single_pass(path, sid)


# ---------------------------------------------------------------------------
# Extraction — one assertion per verb / shape
# ---------------------------------------------------------------------------


def test_create_from_output_url(co_env) -> None:
    """gh pr create prints the URL on stdout → one 'created' row."""
    prs = _extract(
        co_env,
        "s-create",
        _pr_turns(
            "gh pr create --title x --body y",
            "https://github.com/KadenMc/work-buddy/pull/92",
        ),
    )
    assert len(prs) == 1
    p = prs[0]
    assert p["action"] == "created"
    assert p["pr_number"] == 92
    assert p["repo"] == "KadenMc/work-buddy"
    assert p["pr_url"] == "https://github.com/KadenMc/work-buddy/pull/92"


@pytest.mark.parametrize(
    "verb,action,num",
    [
        ("merge", "merged", 92),
        ("close", "closed", 93),
        ("review --approve", "reviewed", 94),
    ],
)
def test_merge_close_review_from_command_url(co_env, verb, action, num) -> None:
    """merge/close/review reference the PR by URL in the command."""
    url = f"https://github.com/KadenMc/work-buddy/pull/{num}"
    prs = _extract(
        co_env,
        f"s-{action}",
        _pr_turns(f"gh pr {verb} {url}", f"Done with #{num}"),
    )
    assert len(prs) == 1
    assert prs[0]["action"] == action
    assert prs[0]["pr_number"] == num


def test_fork_pr_captures_upstream_repo(co_env) -> None:
    """A fork's gh pr create still prints the UPSTREAM owner/repo URL."""
    prs = _extract(
        co_env,
        "s-fork",
        _pr_turns(
            "gh pr create --fill",
            "https://github.com/upstream-org/work-buddy/pull/5",
        ),
    )
    assert prs[0]["repo"] == "upstream-org/work-buddy"
    assert prs[0]["pr_number"] == 5


def test_chained_command_matches(co_env) -> None:
    """`git push && gh pr create` is detected (not start-anchored)."""
    prs = _extract(
        co_env,
        "s-chain",
        _pr_turns(
            "git push -u origin feat && gh pr create --fill",
            "https://github.com/KadenMc/work-buddy/pull/99",
        ),
    )
    assert len(prs) == 1
    assert prs[0]["pr_number"] == 99


def test_view_and_list_are_excluded(co_env) -> None:
    """gh pr view/list print URLs but are NOT activity → no rows."""
    entries = (
        _pr_turns(
            "gh pr view 92",
            "https://github.com/KadenMc/work-buddy/pull/92\ntitle: ...",
            tu="tu_view",
        )
        + _pr_turns(
            "gh pr list",
            "#92  feat  https://github.com/KadenMc/work-buddy/pull/92",
            tu="tu_list",
        )
    )
    prs = _extract(co_env, "s-readonly", entries)
    assert prs == []


def test_truncated_stream_no_result_is_safe(co_env) -> None:
    """A gh pr create with no following tool_result → no row, no crash."""
    entries = [
        user_turn("Open the PR.", "2026-05-08T23:00:00Z"),
        assistant_bash(
            "gh pr create --fill",
            tool_use_id="tu_orphan",
            timestamp="2026-05-08T23:00:01Z",
        ),
        # JSONL ends here mid-stream — no tool_result.
    ]
    prs = _extract(co_env, "s-trunc", entries)
    assert prs == []


def test_bare_number_merge_without_url_is_skipped(co_env) -> None:
    """`gh pr merge 92` with no URL anywhere is skipped (needs a URL)."""
    prs = _extract(
        co_env,
        "s-bare",
        _pr_turns("gh pr merge 92 --squash", "Merged pull request #92"),
    )
    assert prs == []


# ---------------------------------------------------------------------------
# Persistence — refresh populates, is idempotent, query reads
# ---------------------------------------------------------------------------


def test_refresh_persists_query_and_is_idempotent(co_env) -> None:
    from work_buddy.conversation_observability.prs import (
        query_session_prs,
        refresh_session_prs,
    )

    sid = "11111111-1111-1111-1111-111111111111"
    write_session(
        co_env["projects"] / "alpha",
        sid,
        _pr_turns(
            "gh pr create --fill",
            "https://github.com/KadenMc/work-buddy/pull/143",
        ),
    )

    first = refresh_session_prs(days=30)
    assert first["pr_count"] == 1
    assert first["prs"][0]["pr_number"] == 143

    rows = query_session_prs(session_id=sid)
    assert len(rows) == 1
    assert rows[0]["action"] == "created"

    # Re-running with the same file (forced rescan) does not duplicate.
    refresh_session_prs(session_id=sid)
    assert len(query_session_prs(session_id=sid)) == 1
