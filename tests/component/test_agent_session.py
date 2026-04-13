"""Component tests for agent_session — directory creation, manifest, caching."""

import json

import pytest
from freezegun import freeze_time

from work_buddy.agent_session import (
    get_session_dir,
    get_session_context_dir,
    _get_session_id,
    list_sessions,
)


class TestGetSessionId:
    def test_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("WORK_BUDDY_SESSION_ID", raising=False)
        with pytest.raises(RuntimeError, match="WORK_BUDDY_SESSION_ID"):
            _get_session_id()

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "abc-123")
        assert _get_session_id() == "abc-123"


class TestGetSessionDir:
    def test_creates_directory_and_manifest(self, tmp_agents_dir, monkeypatch):
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "test-abcd1234-rest")
        session_dir = get_session_dir()
        assert session_dir.exists()
        assert session_dir.name.endswith("_test-abc")

        manifest_path = session_dir / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["session_id"] == "test-abcd1234-rest"
        assert manifest["short_id"] == "test-abc"

    def test_reuses_existing_directory(self, tmp_agents_dir, monkeypatch):
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "reuse-12345678")
        dir1 = get_session_dir()
        import work_buddy.agent_session as asmod
        monkeypatch.setattr(asmod, "_cached_session_dir", None)
        dir2 = get_session_dir()
        assert dir1 == dir2

    def test_explicit_session_id_bypasses_cache(self, tmp_agents_dir, monkeypatch):
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "default-00000000")
        default_dir = get_session_dir()

        other_dir = get_session_dir(session_id="other-99999999")
        assert other_dir != default_dir
        assert other_dir.name.endswith("_other-99")


class TestGetSessionContextDir:
    @freeze_time("2026-04-12T14:30:00")
    def test_creates_timestamped_context_dir(self, tmp_agents_dir, monkeypatch):
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "ctx-test-00000000")
        session_dir = get_session_dir()
        ctx_dir = get_session_context_dir(session_dir)
        assert ctx_dir.exists()
        assert "20260412-143000" in ctx_dir.name

    @freeze_time("2026-04-12T14:30:00")
    def test_multiple_calls_create_same_dir_when_frozen(self, tmp_agents_dir, monkeypatch):
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "ctx2-test-0000000")
        session_dir = get_session_dir()
        dir1 = get_session_context_dir(session_dir)
        dir2 = get_session_context_dir(session_dir)
        # Both created at same frozen time — mkdir exist_ok=True means same path
        assert dir1 == dir2


class TestListSessions:
    def test_lists_sessions(self, tmp_agents_dir, monkeypatch):
        # Use session IDs with different first-8 chars to avoid directory reuse
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "aaaaaaaa-list-test")
        get_session_dir()

        import work_buddy.agent_session as asmod
        monkeypatch.setattr(asmod, "_cached_session_dir", None)
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "bbbbbbbb-list-test")
        get_session_dir()

        sessions = list_sessions()
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert "aaaaaaaa-list-test" in ids
        assert "bbbbbbbb-list-test" in ids
