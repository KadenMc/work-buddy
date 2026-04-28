"""Fix (a) regression tests: ConsentCache honors the originating session.

Background — `t-e2f1a8c4`: when the sidecar replays a PWU'd
operation, the user's consent grant lives in the user's session DB.
Before this fix, `_cache.is_granted` only looked at the current
process's session DB → sidecar replays consistently failed with
`ConsentRequired` even though the user had already authorized the
operation.

Fix: `is_granted` (and `get_mode`) check the current session first,
then fall back to the session whose ID is on the
`get_originating_session()` contextvar (set by `retry_sweep._replay`
before invoking the callable).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy import agent_session, consent
from work_buddy.consent import ConsentCache


# ---------------------------------------------------------------------------
# Test plumbing
# ---------------------------------------------------------------------------


def _write_grant(
    db_path: Path,
    operation: str,
    *,
    mode: str = "always",
    expires_at: str | None = None,
) -> None:
    """Hand-write a grant row into a session's consent.db so tests can
    set up cross-session state without going through the public API
    (which always writes to the current session)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS grants (
              operation TEXT PRIMARY KEY,
              mode TEXT NOT NULL,
              granted_at TEXT NOT NULL,
              expires_at TEXT
           )"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO grants (operation, mode, granted_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (
            operation,
            mode,
            datetime.now(timezone.utc).isoformat(),
            expires_at,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def two_sessions(tmp_path, monkeypatch):
    """Set up two pretend session directories (current + originating).

    Returns (current_session_id, originating_session_id, current_db, originating_db).
    """
    agents_dir = tmp_path / "agents"

    current_sid = "11111111-aaaa-bbbb-cccc-dddddddddddd"
    originating_sid = "22222222-eeee-ffff-0000-111111111111"

    current_dir = agents_dir / "11111111_current"
    originating_dir = agents_dir / "22222222_originat"
    current_dir.mkdir(parents=True)
    originating_dir.mkdir(parents=True)

    current_db = current_dir / "consent.db"
    originating_db = originating_dir / "consent.db"

    # Patch path resolvers
    def _fake_get_session_dir(session_id=None):
        if session_id is None or session_id == current_sid:
            return current_dir
        if session_id == originating_sid:
            return originating_dir
        # Unknown session — return tmp_path/unknown
        d = agents_dir / f"unknown_{session_id[:8]}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _fake_get_session_consent_db_path(session_dir=None):
        if session_dir is None:
            session_dir = current_dir
        return session_dir / "consent.db"

    def _fake_get_session_id():
        return current_sid

    monkeypatch.setattr(agent_session, "get_session_dir", _fake_get_session_dir)
    monkeypatch.setattr(
        agent_session, "get_session_consent_db_path",
        _fake_get_session_consent_db_path,
    )
    monkeypatch.setattr(agent_session, "_get_session_id", _fake_get_session_id)

    return current_sid, originating_sid, current_db, originating_db


@pytest.fixture
def fresh_cache():
    """Each test gets its own ConsentCache instance (default cache may
    have a path cached from earlier tests / app startup)."""
    return ConsentCache()


# ---------------------------------------------------------------------------
# Existing behavior preserved (no originating session)
# ---------------------------------------------------------------------------


def test_no_originating_session_uses_current_only(two_sessions, fresh_cache):
    """Without _originating_session set, behavior is identical to today."""
    current_sid, _, current_db, _ = two_sessions

    # Grant in current session.
    _write_grant(current_db, "test.op")

    # No originating session set.
    assert agent_session.get_originating_session() is None

    assert fresh_cache.is_granted("test.op") is True
    assert fresh_cache.get_mode("test.op") == "always"


def test_no_grant_anywhere_returns_false(two_sessions, fresh_cache):
    """When neither session has a grant, returns False."""
    assert fresh_cache.is_granted("test.op") is False
    assert fresh_cache.get_mode("test.op") is None


# ---------------------------------------------------------------------------
# Cross-session inheritance (the fix)
# ---------------------------------------------------------------------------


def test_grant_in_originating_session_visible_when_contextvar_set(
    two_sessions, fresh_cache,
):
    """The fix: when current session has no grant but the originating
    session does, is_granted returns True."""
    _, originating_sid, _, originating_db = two_sessions

    # Grant lives in the user's session (originating).
    _write_grant(originating_db, "test.op", mode="temporary")

    # Set the contextvar as retry_sweep._replay would.
    token = agent_session.set_originating_session(originating_sid)
    try:
        assert fresh_cache.is_granted("test.op") is True
        assert fresh_cache.get_mode("test.op") == "temporary"
    finally:
        agent_session.reset_originating_session(token)


def test_originating_session_grant_invisible_when_contextvar_unset(
    two_sessions, fresh_cache,
):
    """Sanity: without the contextvar set, the originating-session
    grant is NOT visible (preserves session isolation when no
    explicit replay context exists)."""
    _, _, _, originating_db = two_sessions

    _write_grant(originating_db, "test.op")

    # Don't set the contextvar.
    assert agent_session.get_originating_session() is None
    assert fresh_cache.is_granted("test.op") is False


def test_revoked_in_originating_returns_false(two_sessions, fresh_cache):
    """Escape hatch: if user revokes the grant in their session, the
    originating-session lookup also finds nothing → ConsentRequired.

    "Revoked" here means the row is deleted (consent.revoke_consent
    behavior). We simulate by simply not writing the row."""
    _, originating_sid, _, originating_db = two_sessions

    # Grant THEN revoke (delete the row).
    _write_grant(originating_db, "test.op")
    conn = sqlite3.connect(str(originating_db))
    conn.execute("DELETE FROM grants WHERE operation = ?", ("test.op",))
    conn.commit()
    conn.close()

    token = agent_session.set_originating_session(originating_sid)
    try:
        assert fresh_cache.is_granted("test.op") is False
        assert fresh_cache.get_mode("test.op") is None
    finally:
        agent_session.reset_originating_session(token)


def test_expired_in_originating_returns_false(two_sessions, fresh_cache):
    """Expired grants in the originating session are NOT honored."""
    _, originating_sid, _, originating_db = two_sessions

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _write_grant(originating_db, "test.op", expires_at=past)

    token = agent_session.set_originating_session(originating_sid)
    try:
        assert fresh_cache.is_granted("test.op") is False
    finally:
        agent_session.reset_originating_session(token)


def test_current_session_grant_takes_precedence(two_sessions, fresh_cache):
    """If both sessions have grants, the current session is checked
    first (no extra DB hit needed)."""
    _, originating_sid, current_db, originating_db = two_sessions

    _write_grant(current_db, "test.op", mode="always")
    _write_grant(originating_db, "test.op", mode="once")

    token = agent_session.set_originating_session(originating_sid)
    try:
        # Current's "always" wins.
        assert fresh_cache.get_mode("test.op") == "always"
    finally:
        agent_session.reset_originating_session(token)


def test_originating_equals_current_skips_double_lookup(
    two_sessions, fresh_cache,
):
    """If the contextvar is set to the current session ID, we don't
    re-check the same DB. No grant → False."""
    current_sid, _, _, _ = two_sessions

    token = agent_session.set_originating_session(current_sid)
    try:
        assert fresh_cache.is_granted("test.op") is False
    finally:
        agent_session.reset_originating_session(token)


def test_workflow_blanket_grant_in_originating_session(
    two_sessions, fresh_cache,
):
    """The workflow blanket grant pattern (which is checked by both
    is_granted and get_mode) ALSO works across the
    current/originating boundary."""
    _, originating_sid, _, originating_db = two_sessions

    _write_grant(
        originating_db,
        ConsentCache.WORKFLOW_CONSENT_OP,
        mode="always",
    )

    token = agent_session.set_originating_session(originating_sid)
    try:
        # Any operation under the workflow blanket → granted.
        assert fresh_cache.is_granted("any.operation") is True
    finally:
        agent_session.reset_originating_session(token)
