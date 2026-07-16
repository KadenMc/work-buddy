from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from work_buddy import config as wb_config
from work_buddy.dashboard import service as dash_service
from work_buddy.settings import broker, store


SETTING_ID = "wb.journal.day-boundary"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "settings.db")
    monkeypatch.setattr(wb_config, "_USER_TZ_CACHE", ZoneInfo("America/New_York"))
    monkeypatch.setitem(dash_service._cfg, "dashboard", {"read_only": False})
    monkeypatch.setattr(broker, "publish_change", lambda event: None)
    dash_service.app.config["TESTING"] = True
    with dash_service.app.test_client() as test_client:
        yield test_client


def test_registry_and_context_value_snapshot(client) -> None:
    registry = client.get("/api/settings/registry")
    assert registry.status_code == 200
    assert registry.headers["Cache-Control"] == "no-store"
    body = registry.get_json()
    assert body["definitions"][0]["setting_id"] == SETTING_ID
    assert len(body["placements"]) == 1
    assert body["pages"][0]["navigation_category"] == "built-in"

    values = client.get(
        "/api/settings/values?context_id=wb.settings.app.journal"
    )
    assert values.status_code == 200
    snapshot = values.get_json()
    assert snapshot["timezone"] == "America/New_York"
    assert snapshot["read_only"] is False
    assert snapshot["values"][0]["effective_value"] == "05:00"


def test_patch_conflict_validation_and_reset_contract(client) -> None:
    response = client.patch(
        f"/api/settings/values/{SETTING_ID}",
        json={
            "scope": "profile",
            "value": "04:00",
            "expected_revision": "value:0",
        },
    )
    assert response.status_code == 200
    value = response.get_json()["value"]
    assert value["effective_value"] == "05:00"
    assert value["pending_value"] == "04:00"

    conflict = client.patch(
        f"/api/settings/values/{SETTING_ID}",
        json={
            "scope": "profile",
            "value": "03:00",
            "expected_revision": "value:0",
        },
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "revision_conflict"
    assert conflict.get_json()["value"]["revision"] == "value:1"

    invalid = client.patch(
        f"/api/settings/values/{SETTING_ID}",
        json={
            "scope": "profile",
            "value": "25:00",
            "expected_revision": "value:1",
        },
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["field"] == "value"

    reset = client.delete(
        f"/api/settings/values/{SETTING_ID}",
        json={"scope": "profile", "expected_revision": "value:1"},
    )
    assert reset.status_code == 200
    assert reset.get_json()["value"]["pending_value"] is None


def test_post_reset_alias_and_read_only_enforcement(client, monkeypatch) -> None:
    initial = client.get("/api/settings/values").get_json()["values"][0]
    alias = client.post(
        "/api/settings/reset",
        json={
            "setting_id": SETTING_ID,
            "scope": "profile",
            "expected_revision": initial["revision"],
        },
    )
    assert alias.status_code == 200

    monkeypatch.setitem(dash_service._cfg, "dashboard", {"read_only": True})
    blocked = client.patch(
        f"/api/settings/values/{SETTING_ID}",
        json={
            "scope": "profile",
            "value": "04:00",
            "expected_revision": alias.get_json()["value"]["revision"],
        },
    )
    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "read_only"
    snapshot = client.get("/api/settings/values").get_json()
    assert snapshot["read_only"] is True


def test_unknown_context_and_setting_are_structured_errors(client) -> None:
    context = client.get("/api/settings/values?context_id=nope")
    assert context.status_code == 404
    assert context.get_json()["error"] == "unknown_context"

    setting = client.patch(
        "/api/settings/values/nope",
        json={
            "scope": "profile",
            "value": "05:00",
            "expected_revision": "value:0",
        },
    )
    assert setting.status_code == 404
    assert setting.get_json()["error"] == "unknown_setting"


def test_proposed_value_preview_is_no_store_and_side_effect_free(
    client,
    monkeypatch,
) -> None:
    initial = client.get("/api/settings/values").get_json()["values"][0]
    published: list[dict] = []
    monkeypatch.setattr(
        broker,
        "publish_change",
        lambda event: published.append(event) if event else None,
    )
    response = client.post(
        f"/api/settings/values/{SETTING_ID}/preview",
        json={
            "scope": "profile",
            "value": "04:00",
            "expected_revision": initial["revision"],
        },
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["value_revision"] == initial["revision"]
    assert payload["preview"]["setting_id"] == SETTING_ID
    assert payload["preview"]["value"] == "04:00"
    assert payload["preview"]["apply_status"] == "pending"
    assert payload["preview"]["impact_preview"]["pending_day"] is not None
    assert published == []

    unchanged = client.get("/api/settings/values").get_json()["values"][0]
    assert unchanged["revision"] == initial["revision"]
    assert unchanged["pending_value"] is None


def test_preview_uses_existing_validation_conflict_and_read_only_contract(
    client,
    monkeypatch,
) -> None:
    invalid = client.post(
        f"/api/settings/values/{SETTING_ID}/preview",
        json={
            "scope": "profile",
            "value": "25:00",
            "expected_revision": "value:0",
        },
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["field"] == "value"

    conflict = client.post(
        f"/api/settings/values/{SETTING_ID}/preview",
        json={
            "scope": "profile",
            "value": "04:00",
            "expected_revision": "value:99",
        },
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["value"]["revision"] == "value:0"

    monkeypatch.setitem(dash_service._cfg, "dashboard", {"read_only": True})
    blocked = client.post(
        f"/api/settings/values/{SETTING_ID}/preview",
        json={
            "scope": "profile",
            "value": "04:00",
            "expected_revision": "value:0",
        },
    )
    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "read_only"


def test_successful_write_publishes_normalized_settings_event(
    client, monkeypatch
) -> None:
    published: list[dict] = []
    monkeypatch.setattr(broker, "publish_change", published.append)
    response = client.patch(
        f"/api/settings/values/{SETTING_ID}",
        json={
            "scope": "profile",
            "value": "04:00",
            "expected_revision": "value:0",
        },
    )
    assert response.status_code == 200
    assert published == [response.get_json()["event"]]
    assert published[0]["type"] == "settings.changed"
    assert published[0]["setting_ids"] == [SETTING_ID]
    assert "view:wb.journal.main" in published[0]["affected_contexts"]
