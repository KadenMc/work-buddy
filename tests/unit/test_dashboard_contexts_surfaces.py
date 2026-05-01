"""Slice 5a dashboard surfaces: Engage view + blocked-by-context.

Three endpoints:

* ``GET /api/automation/engage`` — every open task with the Slice-4
  operating-tier decision + Slice-5a who-can-act decision + per-row
  user-current satisfaction.
* ``GET /api/automation/blocked-by-context`` — aggregate of open
  tasks blocked on missing agent contexts, grouped by token, with
  setup-wizard deep-links per group.
* The existing ``GET /api/automation/review-queue`` (Slice 4) gains
  ``who_can_act`` on each entry.
"""

from __future__ import annotations

import json

import pytest

from work_buddy.dashboard import service as dash_service
from work_buddy.obsidian.tasks import store


@pytest.fixture
def _isolated_store(monkeypatch, tmp_path):
    db_file = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_file)
    return db_file


@pytest.fixture
def client(_isolated_store):
    dash_service.app.config["TESTING"] = True
    with dash_service.app.test_client() as c:
        yield c


@pytest.fixture
def fake_tool_status(monkeypatch):
    """Replace work_buddy.tools.is_tool_available with a controllable map."""
    state: dict[str, bool] = {}

    def _is_avail(tid: str) -> bool:
        return state.get(tid, False)

    monkeypatch.setattr(
        "work_buddy.automation.contexts.is_tool_available", _is_avail,
    )
    return state


def _seed(
    task_id: str,
    *,
    description: str = "test",
    agent: list[str] | None = None,
    user: list[str] | None = None,
    state: str = "inbox",
):
    store.create(
        task_id=task_id,
        state=state,
        description=description,
        agent_required_contexts=json.dumps(agent) if agent is not None else None,
        user_required_contexts=json.dumps(user) if user is not None else None,
        required_contexts_source="agent_inferred" if (agent or user) else None,
    )


# ---------------------------------------------------------------------------
# Engage view
# ---------------------------------------------------------------------------


def test_engage_empty(client):
    resp = client.get("/api/automation/engage")
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["count"] == 0
    assert body["items"] == []
    # Known tokens always present so the frontend can render presets.
    assert "@filesystem" in body["known_tokens"]


def test_engage_returns_who_can_act(client, fake_tool_status):
    fake_tool_status["thunderbird"] = False
    _seed("t-eng-001",
          description="Send dean an email",
          agent=["@email_send"],
          user=["@email_send"])

    body = client.get("/api/automation/engage").get_json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["task_id"] == "t-eng-001"
    who = item["who_can_act"]
    assert who["agent"] is False
    assert who["agent_unmet"] == ["@email_send"]
    # User declared the context so the user-side trusts at this layer.
    assert who["user"] is True
    assert who["agent_handoff_eligible"] is True
    assert item["auto"]["operating"] == 1
    assert item["auto"]["pipeline_blocker"]["kind"] == "agent_context_unmet"


def test_engage_user_now_filter(client, fake_tool_status):
    fake_tool_status["obsidian"] = True
    _seed("t-eng-002",
          description="Vault edit",
          agent=["@vault"],
          user=["@user_workstation"])

    # Default: no current contexts → user_now.satisfied = False
    body = client.get("/api/automation/engage").get_json()
    item = body["items"][0]
    assert item["user_now"]["satisfied"] is False
    assert "@user_workstation" in item["user_now"]["unmet"]

    # Provide @user_workstation → user_now.satisfied = True
    body = client.get(
        "/api/automation/engage?contexts=@user_workstation,@filesystem",
    ).get_json()
    item = body["items"][0]
    assert item["user_now"]["satisfied"] is True


def test_engage_skips_done_and_archived(client, fake_tool_status):
    _seed("t-eng-done",
          description="done task",
          agent=["@filesystem"],
          state="done")
    body = client.get("/api/automation/engage").get_json()
    assert body["count"] == 0


# ---------------------------------------------------------------------------
# Blocked-by-context aggregation
# ---------------------------------------------------------------------------


def test_blocked_by_context_groups_and_counts(client, fake_tool_status):
    fake_tool_status["thunderbird"] = False
    fake_tool_status["obsidian"] = False

    _seed("t-blk-1", agent=["@email_send"], user=["@email_send"])
    _seed("t-blk-2", agent=["@email_send"], user=[])
    _seed("t-blk-3", agent=["@vault"], user=[])

    body = client.get("/api/automation/blocked-by-context").get_json()
    assert body["status"] == "ok"
    assert body["blocked_count"] == 3
    by_ctx = {it["context"]: it for it in body["items"]}
    assert by_ctx["@email_send"]["count"] == 2
    assert by_ctx["@email_send"]["tool_ids"] == ["thunderbird"]
    assert by_ctx["@email_send"]["setup_link"].startswith("/setup?requirement_id=thunderbird")
    assert by_ctx["@vault"]["count"] == 1


def test_blocked_by_context_skips_satisfied_tasks(client, fake_tool_status):
    fake_tool_status["thunderbird"] = True
    _seed("t-blk-ok", agent=["@email_send"], user=[])

    body = client.get("/api/automation/blocked-by-context").get_json()
    assert body["blocked_count"] == 0
    assert body["items"] == []


def test_blocked_by_context_ignores_tasks_without_contexts(client):
    _seed("t-no-ctx", description="legacy task")
    body = client.get("/api/automation/blocked-by-context").get_json()
    assert body["examined_tasks"] == 0
    assert body["blocked_count"] == 0


# ---------------------------------------------------------------------------
# Review-queue who_can_act enrichment
# ---------------------------------------------------------------------------


def test_review_queue_includes_who_can_act_when_present(client, fake_tool_status):
    """Review queue picks up context-blocked tasks at tier 1."""
    fake_tool_status["thunderbird"] = False
    _seed("t-rq-ctx", description="Email task",
          agent=["@email_send"], user=["@email_send"])

    body = client.get("/api/automation/review-queue").get_json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["task_id"] == "t-rq-ctx"
    assert item["operating"] == 1
    assert item["pipeline_blocker"]["kind"] == "agent_context_unmet"
    assert item["who_can_act"]["agent_handoff_eligible"] is True
    assert "@email_send" in item["who_can_act"]["agent_unmet"]
