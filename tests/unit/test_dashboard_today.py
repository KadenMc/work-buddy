"""Tests for the dashboard's ``GET /api/automation/today`` endpoint.

Smoke-test that the endpoint composes the engage view + the now-plan
+ recommendations + work-hour metadata, and forwards
``current_contexts`` from the query string.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from work_buddy.dashboard import service as dash_service
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore


@pytest.fixture
def _isolated_store(monkeypatch, tmp_path):
    from work_buddy.tasks import runtime
    from work_buddy.tasks import store as native_store

    db_file = tmp_path / "tasks.sqlite"
    latch_file = tmp_path / "task_authority_latch.json"
    monkeypatch.setattr(native_store, "default_task_db_path", lambda: db_file)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: db_file)
    monkeypatch.setattr(runtime, "_canonical_default_latch_path", lambda: latch_file)

    task_store = TaskStore(db_file)
    task_store.initialize()
    state = task_store.system_state()
    runtime.arm_native_authority_latch(
        db_file,
        cohort_id="dashboard-today-test",
        target_authority_epoch="native:dashboard-today-test",
        cutover_receipt_id="dashboard-today-test-cutover",
        armed_at="2026-08-27T12:00:00+00:00",
    )
    task_store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:dashboard-today-test",
        updated_at="2026-08-27T12:00:00+00:00",
        cutover_receipt_id="dashboard-today-test-cutover",
        process_generation=1,
    )
    return TaskApplicationService(task_store)


@pytest.fixture
def client(_isolated_store, monkeypatch):
    # Stub out the pure day-planner to keep the endpoint test deterministic.
    monkeypatch.setattr(
        "work_buddy.journal_capture.planner.generate_plan",
        lambda calendar_events, focused_tasks, cfg: [
            {"time_start": "10:00", "time_end": "11:00",
             "text": (focused_tasks[0]["description"]
                      if focused_tasks else "(no tasks)"),
             "checked": False}
        ],
    )
    # Stub the native task briefing; authority routing itself remains real.
    monkeypatch.setattr(
        "work_buddy.tasks.capabilities.daily_briefing",
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
        "timezone", "now", "work_hours", "journal_day", "current_contexts", "recommendations",
        "plan", "focused_count", "calendar_event_count",
        "active_contracts", "engage_count",
    ):
        assert key in body, f"missing: {key}"
    # now block has the time fields
    assert "iso" in body["now"]
    assert "local_hhmm" in body["now"]
    assert body["journal_day"]["timezone"] == body["timezone"]
    assert body["journal_day"]["day_boundary_start"]
    assert datetime.fromisoformat(body["journal_day"]["window_start"]).tzinfo is not None
    assert datetime.fromisoformat(body["journal_day"]["window_end"]).tzinfo is not None


def test_today_uses_configured_timezone_for_temporal_context(client, monkeypatch):
    """The dashboard must not inherit the server OS timezone by accident."""
    from work_buddy import config as wb_config

    configured = ZoneInfo("Pacific/Kiritimati")
    monkeypatch.setattr(wb_config, "_USER_TZ_CACHE", configured)

    body = client.get("/api/automation/today").get_json()
    instant = datetime.fromisoformat(body["now"]["iso"])
    local = instant.astimezone(configured)

    assert body["timezone"] == "Pacific/Kiritimati"
    assert body["now"]["local_hhmm"] == local.strftime("%H:%M")
    assert body["now"]["minutes_into_day"] == local.hour * 60 + local.minute


def test_today_forwards_current_contexts(client):
    body = client.get(
        "/api/automation/today?contexts=@filesystem,@vault",
    ).get_json()
    assert body["current_contexts"] == ["@filesystem", "@vault"]


def test_today_recommendations_pick_focused_task(client, _isolated_store):
    """A focused task with both sides satisfied lands as a recommendation."""
    _isolated_store.create(
        description="Edit code",
        client_mutation_id="today-create-001",
        actor="test:user",
        task_id="t-today-001",
        state="focused",
        summary_text="Edit the selected code now.",
        agent_required_contexts=["@filesystem"],
        user_required_contexts=["@user_workstation"],
        required_contexts_source="agent_inferred",
    )
    body = client.get(
        "/api/automation/today?contexts=@filesystem,@user_workstation",
    ).get_json()
    assert body["status"] in {"ok", "degraded"}
    rec_ids = [r.get("task_id") for r in body["recommendations"]]
    assert "t-today-001" in rec_ids


def test_today_blocked_task_drops_from_recommendations(
    client, _isolated_store, monkeypatch,
):
    """Tasks the agent can't satisfy and the user isn't currently in
    don't surface as a top recommendation."""
    # Force tool unavailable so the agent fails on @vault.
    monkeypatch.setattr(
        "work_buddy.automation.contexts.is_tool_available",
        lambda tid: False,
    )
    _isolated_store.create(
        description="Vault edit",
        client_mutation_id="today-create-002",
        actor="test:user",
        task_id="t-today-002",
        state="focused",
        summary_text="Edit the selected vault content now.",
        agent_required_contexts=["@vault"],
        user_required_contexts=["@vault"],
        required_contexts_source="agent_inferred",
    )
    # User declares phone-only contexts → can't satisfy @vault either.
    body = client.get(
        "/api/automation/today?contexts=@phone_voice",
    ).get_json()
    rec_ids = [r.get("task_id") for r in body["recommendations"]]
    assert "t-today-002" not in rec_ids
