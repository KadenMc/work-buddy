"""Refresh + query semantics for conversation_observability.commits.

The characterization tests in test_inspector_extraction_characterization
already pin the *legacy shape* the inspector wrappers must continue to
return. These tests add the DB-specific contract: stale-only refresh
skips unchanged files, query_session_commits reads the DB directly, and
re-running the refresh on a modified file replaces stale rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.conversation_observability_fixtures import (
    commit_scenario,
    write_session,
)


# ---------------------------------------------------------------------------
# Fixture: real DB at tmp path, real fake projects dir
# ---------------------------------------------------------------------------


@pytest.fixture
def co_env(tmp_path, monkeypatch):
    """Wire up an isolated projects dir + DB and clear caches."""
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


# ---------------------------------------------------------------------------
# refresh_session_commits — DB population, return shape, sort order
# ---------------------------------------------------------------------------


def test_refresh_populates_db_and_returns_legacy_shape(co_env) -> None:
    from work_buddy.conversation_observability.commits import (
        query_session_commits,
        refresh_session_commits,
    )

    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(
            sid, commit_hash="abc1234", message="Refresh test",
        ),
    )

    result = refresh_session_commits(days=30)
    assert result["commit_count"] == 1
    assert result["commits"][0]["hash"] == "abc1234"
    assert result["commits"][0]["session_id"] == sid

    # DB row was written.
    rows = query_session_commits(session_id=sid)
    assert len(rows) == 1
    assert rows[0]["hash"] == "abc1234"


def test_refresh_skips_unchanged_files(co_env) -> None:
    """A second refresh with no file mtime change re-uses cached rows."""
    from work_buddy.conversation_observability.commits import (
        refresh_session_commits,
    )

    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="cccccc1"),
    )

    refresh_session_commits(days=30)

    # Drop the row directly to detect whether the second refresh
    # parsed and re-inserted it. If staleness check works, the row
    # remains gone (file mtime unchanged → skipped → cached lookup
    # finds nothing in session_commits but no re-parse happens).
    import sqlite3
    conn = sqlite3.connect(str(co_env["db"]))
    conn.execute("DELETE FROM session_commits WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()

    second = refresh_session_commits(days=30)
    # No re-parse happened → no commits returned for this sid.
    sids = [c["session_id"] for c in second["commits"]]
    assert sid not in sids


def test_refresh_re_parses_when_file_mtime_changes(co_env) -> None:
    """Touching the file forces a fresh parse and row replacement."""
    import os
    import time

    from work_buddy.conversation_observability.commits import (
        refresh_session_commits,
    )

    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    path = write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="eeeeee1"),
    )

    refresh_session_commits(days=30)

    # Bump mtime by 10s — enough for the float comparison to register
    # as different.
    new_mtime = time.time() + 10
    os.utime(path, (new_mtime, new_mtime))

    # Delete the row, refresh, expect it back.
    import sqlite3
    conn = sqlite3.connect(str(co_env["db"]))
    conn.execute("DELETE FROM session_commits WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()

    second = refresh_session_commits(days=30)
    sids = [c["session_id"] for c in second["commits"]]
    assert sid in sids


def test_refresh_with_explicit_session_id_forces_rescan(co_env) -> None:
    """Passing session_id always re-parses, even when mtime is unchanged."""
    from work_buddy.conversation_observability.commits import (
        refresh_session_commits,
    )

    sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="ffffff1"),
    )

    refresh_session_commits(days=30)

    # Drop row; force rescan via explicit session_id.
    import sqlite3
    conn = sqlite3.connect(str(co_env["db"]))
    conn.execute("DELETE FROM session_commits WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()

    result = refresh_session_commits(session_id=sid)
    sids = [c["session_id"] for c in result["commits"]]
    assert sid in sids


# ---------------------------------------------------------------------------
# query_session_commits — read-only, no JSONL touch
# ---------------------------------------------------------------------------


def test_query_returns_only_cached_rows(co_env) -> None:
    """Query reads the DB directly without scanning JSONL."""
    from work_buddy.conversation_observability.commits import (
        query_session_commits,
        refresh_session_commits,
    )

    # No refresh yet → empty.
    assert query_session_commits() == []

    sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="1234567"),
    )
    refresh_session_commits(days=30)

    rows = query_session_commits()
    assert len(rows) == 1
    assert rows[0]["hash"] == "1234567"


# ---------------------------------------------------------------------------
# get_commit_session_map — 7-char short-sha → full session_id
# ---------------------------------------------------------------------------


def test_get_commit_session_map_returns_short_sha_mapping(co_env) -> None:
    from work_buddy.conversation_observability.commits import (
        get_commit_session_map,
    )

    sid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    write_session(
        co_env["projects"] / "alpha",
        session_id=sid,
        entries=commit_scenario(sid, commit_hash="deadbee"),
    )

    mapping = get_commit_session_map(days=30)
    assert mapping["deadbee"] == sid
