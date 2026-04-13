"""Shared fixtures for work-buddy test suite.

Key isolation concerns:
- WORK_BUDDY_SESSION_ID must be set before importing most work_buddy modules
- agent_session._cached_session_dir persists across tests — must be reset
- paths.data_dir resolves to the real data/ — must be redirected for isolation
- config.py computes USER_TZ at import time — generally fine, but tests that
  need a different timezone should monkeypatch work_buddy.config.USER_TZ
- messaging models resolve DB path from config — override via cfg param
"""

import os

# CRITICAL: Must be set before ANY work_buddy imports happen during collection.
# Many modules trigger get_logger() -> get_session_dir() at import time.
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-session-00000000")

import pytest


@pytest.fixture(autouse=True)
def _set_session_env(monkeypatch):
    """Ensure WORK_BUDDY_SESSION_ID is always set for imports."""
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "test-session-00000000")


@pytest.fixture
def tmp_agents_dir(tmp_path, monkeypatch):
    """Redirect agent_session to write into a temp directory.

    Monkeypatches ``paths.data_dir`` so that ``data_dir("agents")`` returns
    ``tmp_path`` while other categories are unaffected.  Also clears the
    cached session dir so each test starts fresh.

    Returns the temp agents/ directory.
    """
    import work_buddy.agent_session as asmod
    import work_buddy.paths as pmod

    _original_data_dir = pmod.data_dir

    def _patched_data_dir(category: str = "") -> "Path":
        if category == "agents":
            tmp_path.mkdir(parents=True, exist_ok=True)
            return tmp_path
        return _original_data_dir(category)

    monkeypatch.setattr(pmod, "data_dir", _patched_data_dir)
    # Also patch the direct import reference in agent_session
    monkeypatch.setattr(asmod, "data_dir", _patched_data_dir)
    monkeypatch.setattr(asmod, "_cached_session_dir", None)
    return tmp_path


@pytest.fixture
def tmp_messaging_db(tmp_path):
    """Create a fresh in-memory-like SQLite messaging DB in a temp dir.

    Returns (connection, db_path).
    """
    from work_buddy.messaging.models import get_connection

    db_path = tmp_path / "test_messages.db"
    import work_buddy.messaging.models as mmod

    original = mmod._db_path

    def _patched_db_path(c=None):
        return db_path

    mmod._db_path = _patched_db_path
    try:
        conn = get_connection()
        yield conn, db_path
    finally:
        conn.close()
        mmod._db_path = original
