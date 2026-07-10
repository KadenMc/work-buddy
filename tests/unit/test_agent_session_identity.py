"""Native harness environment fallback for local work-buddy code."""

from __future__ import annotations

import pytest

from work_buddy import agent_session


def test_work_buddy_session_id_has_priority(monkeypatch):
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "work-buddy-id")
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-id")
    assert agent_session._get_session_id() == "work-buddy-id"


def test_codex_thread_id_is_native_fallback(monkeypatch):
    monkeypatch.delenv("WORK_BUDDY_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-id")
    assert agent_session._get_session_id() == "codex-id"


def test_missing_native_identity_still_fails_closed(monkeypatch):
    monkeypatch.delenv("WORK_BUDDY_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(RuntimeError, match="No agent session identity"):
        agent_session._get_session_id()
