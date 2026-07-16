"""Contract checks for the legacy read model used by the React Journal adapter.

These tests intentionally stop at the existing Flask boundary. The React adapter may
project genuine Today fields, but the endpoint must not be mistaken for a native
Journal API or acquire write behavior as part of the UI-first scaffold.
"""

from __future__ import annotations

import pytest

from work_buddy.dashboard import service as dash_service


@pytest.fixture
def client():
    dash_service.app.config["TESTING"] = True
    with dash_service.app.test_client() as test_client:
        yield test_client


def _today_payload(*, current_contexts: list[str] | None = None) -> dict:
    return {
        "status": "degraded",
        "timezone": "America/Toronto",
        "now": {
            "iso": "2026-07-11T16:18:00+00:00",
            "local_hhmm": "12:18",
            "minutes_into_day": 738,
        },
        "work_hours": [9, 17],
        "journal_day": {
            "local_date": "2026-07-11",
            "timezone": "America/Toronto",
            "day_boundary_start": "05:00",
            "window_start": "2026-07-11T05:00:00-04:00",
            "window_end": "2026-07-12T05:00:00-04:00",
            "boundary_setting_revision": "value:0",
            "pending_day_boundary_start": None,
            "boundary_effective_at": None,
        },
        "current_contexts": list(current_contexts or []),
        "recommendations": [
            {"task_id": "t-1", "text": "Untimed recommendation", "state": "focused"}
        ],
        "plan": [
            {
                "time_start": "12:20",
                "time_end": "13:30",
                "text": "Prototype mobile timeline",
                "checked": False,
            },
            {
                "time_start": "14:00",
                "time_end": "14:45",
                "text": "[Cal] Northwind project review",
                "checked": False,
            },
        ],
        "plan_status": "partial",
        "focused_count": 1,
        "calendar_event_count": 1,
        "active_contracts": [],
        "contract_constraints": [],
        "engage_count": 2,
        "errors": ["calendar source unavailable"],
    }


def test_legacy_today_endpoint_exposes_only_read_projection_fields(
    client, monkeypatch
):
    monkeypatch.setattr(dash_service, "_build_today_payload", _today_payload)

    response = client.get("/api/automation/today")
    body = response.get_json()

    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["timezone"] == "America/Toronto"
    assert body["now"] == {
        "iso": "2026-07-11T16:18:00+00:00",
        "local_hhmm": "12:18",
        "minutes_into_day": 738,
    }
    assert body["work_hours"] == [9, 17]
    assert body["journal_day"]["day_boundary_start"] == "05:00"
    assert body["journal_day"]["window_end"] == "2026-07-12T05:00:00-04:00"
    assert body["plan"][0] == {
        "time_start": "12:20",
        "time_end": "13:30",
        "text": "Prototype mobile timeline",
        "checked": False,
    }
    assert body["recommendations"][0]["text"] == "Untimed recommendation"
    assert body["calendar_event_count"] == 1
    assert body["errors"] == ["calendar source unavailable"]

    # Aggregate Today data is not a native Journal model. The adapter must keep these
    # capabilities unavailable rather than fabricating them from nearby-looking fields.
    forbidden_native_fields = {
        "capture",
        "capture_persistence",
        "running_notes",
        "observed_records",
        "calendar_events",
        "smart_processing",
        "revision",
    }
    assert forbidden_native_fields.isdisjoint(body)


def test_legacy_today_endpoint_remains_get_only_and_forwards_contexts(
    client, monkeypatch
):
    seen: list[list[str]] = []

    def build(*, current_contexts: list[str] | None = None) -> dict:
        seen.append(list(current_contexts or []))
        return _today_payload(current_contexts=current_contexts)

    monkeypatch.setattr(dash_service, "_build_today_payload", build)

    response = client.get(
        "/api/automation/today?contexts=@filesystem,%20@vault,,@user_workstation"
    )
    assert response.status_code == 200
    assert response.get_json()["current_contexts"] == [
        "@filesystem",
        "@vault",
        "@user_workstation",
    ]
    assert seen == [["@filesystem", "@vault", "@user_workstation"]]

    # No mutation route is introduced for the compatibility provider.
    assert client.post("/api/automation/today", json={"text": "write"}).status_code == 405
