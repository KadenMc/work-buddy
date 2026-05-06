"""Tests for the dashboard's ``GET /api/automation/today`` endpoint.

Smoke-test that the endpoint composes the engage view + the now-plan
+ recommendations + work-hour metadata, and forwards
``current_contexts`` from the query string.
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
def client(_isolated_store, monkeypatch):
    # Stub out the day-planner so the test doesn't depend on Obsidian.
    monkeypatch.setattr(
        "work_buddy.obsidian.day_planner.planner.generate_plan",
        lambda calendar_events, focused_tasks, cfg: [
            {"time_start": "10:00", "time_end": "11:00",
             "text": (focused_tasks[0]["description"]
                      if focused_tasks else "(no tasks)"),
             "checked": False}
        ],
    )
    # Stub task_briefing so the load-context step doesn't need Obsidian.
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.manager.daily_briefing",
        lambda: {"focused": [], "mit": [], "overdue": [], "stale": [],
                 "inbox_count": 0},
    )
    dash_service.app.config["TESTING"] = True
    with dash_service.app.test_client() as c:
        yield c


def test_today_payload_shape(client):
    """Endpoint returns the documented top-level keys."""
    body = client.get("/api/automation/today").get_json()
    # Status may degrade if calendar/contracts unavailable in the test env.
    assert body["status"] in {"ok", "degraded"}
    for key in (
        "now", "work_hours", "current_contexts", "recommendations",
        "plan", "focused_count", "calendar_event_count",
        "active_contracts", "engage_count",
    ):
        assert key in body, f"missing: {key}"
    # now block has the time fields
    assert "iso" in body["now"]
    assert "local_hhmm" in body["now"]


def test_today_forwards_current_contexts(client):
    body = client.get(
        "/api/automation/today?contexts=@filesystem,@vault",
    ).get_json()
    assert body["current_contexts"] == ["@filesystem", "@vault"]


def test_today_recommendations_pick_focused_task(client):
    """A focused task with both sides satisfied lands as a recommendation."""
    store.create(
        task_id="t-today-001",
        state="focused",
        description="Edit code",
        agent_required_contexts=json.dumps(["@filesystem"]),
        user_required_contexts=json.dumps(["@user_workstation"]),
        required_contexts_source="agent_inferred",
    )
    body = client.get(
        "/api/automation/today?contexts=@filesystem,@user_workstation",
    ).get_json()
    assert body["status"] in {"ok", "degraded"}
    rec_ids = [r.get("task_id") for r in body["recommendations"]]
    assert "t-today-001" in rec_ids


def test_today_blocked_task_drops_from_recommendations(client, monkeypatch):
    """Tasks the agent can't satisfy and the user isn't currently in
    don't surface as a top recommendation."""
    # Force tool unavailable so the agent fails on @vault.
    monkeypatch.setattr(
        "work_buddy.automation.contexts.is_tool_available",
        lambda tid: False,
    )
    store.create(
        task_id="t-today-002",
        state="focused",
        description="Vault edit",
        agent_required_contexts=json.dumps(["@vault"]),
        user_required_contexts=json.dumps(["@vault"]),
        required_contexts_source="agent_inferred",
    )
    # User declares phone-only contexts → can't satisfy @vault either.
    body = client.get(
        "/api/automation/today?contexts=@phone_voice",
    ).get_json()
    rec_ids = [r.get("task_id") for r in body["recommendations"]]
    assert "t-today-002" not in rec_ids
