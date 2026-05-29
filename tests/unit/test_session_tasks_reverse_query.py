"""Reverse session→tasks query + the session_tasks_get capability.

``store.get_sessions`` answers task→sessions; this pins the new reverse
reader ``store.get_tasks_for_session`` and the bridge-independent
``session_tasks_get`` op that enriches each row from the SQLite store.
"""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks import store


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


def test_reverse_query_returns_assigned_tasks_oldest_first(fresh_db) -> None:
    store.create(task_id="t-aaa")
    store.create(task_id="t-bbb")
    # t-aaa assigned first, then t-bbb — both to the same session.
    store.assign_session("t-aaa", "sess-1")
    store.assign_session("t-bbb", "sess-1")
    # A different session touched only t-bbb.
    store.assign_session("t-bbb", "sess-2")

    rows = store.get_tasks_for_session("sess-1")
    assert [r["task_id"] for r in rows] == ["t-aaa", "t-bbb"]
    assert all("assigned_at" in r for r in rows)

    rows2 = store.get_tasks_for_session("sess-2")
    assert [r["task_id"] for r in rows2] == ["t-bbb"]


def test_reverse_query_empty_for_unknown_session(fresh_db) -> None:
    assert store.get_tasks_for_session("nobody") == []


def test_round_trip_against_get_sessions(fresh_db) -> None:
    """get_tasks_for_session is the inverse of get_sessions."""
    store.create(task_id="t-xyz")
    store.assign_session("t-xyz", "sess-rt")

    fwd = store.get_sessions("t-xyz")
    assert [s["session_id"] for s in fwd] == ["sess-rt"]
    rev = store.get_tasks_for_session("sess-rt")
    assert [t["task_id"] for t in rev] == ["t-xyz"]


def test_session_tasks_get_enriches_text_and_state(fresh_db) -> None:
    from work_buddy.mcp_server.ops.tasks_ops import session_tasks_get

    store.create(task_id="t-enrich", description="Wire the linkage")
    # Move it out of the default 'inbox' state to prove state is read.
    store.update("t-enrich", state="focused")
    store.assign_session("t-enrich", "sess-e")

    result = session_tasks_get("sess-e")
    assert "tasks" in result
    assert len(result["tasks"]) == 1
    t = result["tasks"][0]
    assert t["task_id"] == "t-enrich"
    assert t["task_text"] == "Wire the linkage"
    assert t["state"] == "focused"
    assert "assigned_at" in t


def test_session_tasks_get_empty(fresh_db) -> None:
    from work_buddy.mcp_server.ops.tasks_ops import session_tasks_get

    assert session_tasks_get("ghost") == {"tasks": []}


def test_assign_records_originating_session_not_env(fresh_db, monkeypatch) -> None:
    """assign_task must record the agent's pinned session (the JSONL UUID
    the gateway sets on the originating-session ContextVar), not the MCP
    server process's bootstrap WORK_BUDDY_SESSION_ID. Recording the
    bootstrap id is what made the reverse linkage unjoinable against the
    JSONL-keyed commit/PR/chat rows.
    """
    from work_buddy.agent_session import (
        reset_originating_session,
        set_originating_session,
    )
    from work_buddy.obsidian.tasks import mutations

    # Bridge-free: stub the payload loader so no Obsidian read happens.
    monkeypatch.setattr(mutations, "_load_task_payload", lambda tid: {"success": True})
    # The process env stands in for the MCP server's bootstrap session.
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "sidecar-bootstrap")
    store.create(task_id="t-assign-pin")

    token = set_originating_session("real-jsonl-uuid")
    try:
        result = mutations.assign_task("t-assign-pin")
    finally:
        reset_originating_session(token)

    assert result["session_id"] == "real-jsonl-uuid"
    # Joinable under the real id; nothing recorded under the bootstrap id.
    assert [t["task_id"] for t in store.get_tasks_for_session("real-jsonl-uuid")] == [
        "t-assign-pin"
    ]
    assert store.get_tasks_for_session("sidecar-bootstrap") == []


def test_assign_falls_back_to_env_without_originating(fresh_db, monkeypatch) -> None:
    """When no originating session is pinned (direct, non-gateway call),
    assign_task falls back to the WORK_BUDDY_SESSION_ID env var.
    """
    from work_buddy.obsidian.tasks import mutations

    monkeypatch.setattr(mutations, "_load_task_payload", lambda tid: {"success": True})
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "env-session-only")
    store.create(task_id="t-assign-env")

    result = mutations.assign_task("t-assign-env")
    assert result["session_id"] == "env-session-only"
