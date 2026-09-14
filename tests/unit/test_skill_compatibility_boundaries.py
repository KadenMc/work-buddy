"""Focused compatibility tests for durable capability-to-skill boundaries."""

from __future__ import annotations

import json


def test_completing_legacy_operation_rewrites_canonical_type(
    tmp_path, monkeypatch,
):
    from work_buddy.mcp_server.tools import gateway

    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", tmp_path)
    operation_id = "op_legacy_complete"
    path = tmp_path / f"{operation_id}.json"
    path.write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "type": "capability",
                "name": "task_read",
                "status": "running",
            }
        ),
        encoding="utf-8",
    )

    gateway._complete_operation(operation_id, result={"ok": True})

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["type"] == "skill"


def test_recent_operations_project_legacy_type_as_skill(tmp_path, monkeypatch):
    from work_buddy.mcp_server.tools import gateway

    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", tmp_path)
    operation_id = "op_legacy_status"
    (tmp_path / f"{operation_id}.json").write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "type": "capability",
                "name": "task_read",
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )

    assert gateway._list_recent_operations() == [
        {
            "operation_id": operation_id,
            "name": "task_read",
            "type": "skill",
            "status": "completed",
            "retry_policy": None,
            "attempt": None,
            "created_at": None,
            "completed_at": None,
        }
    ]


def test_legacy_notification_callback_is_canonical_on_read_and_rewrite(
    tmp_path, monkeypatch,
):
    from work_buddy.notifications import store
    from work_buddy.notifications.models import Notification, ResponseType

    monkeypatch.setattr(store, "_get_store_dir", lambda: tmp_path)
    notification = Notification(
        notification_id="req_legacy",
        title="Legacy callback",
        response_type=ResponseType.CHOICE.value,
        choices=[{"key": "yes", "label": "Yes"}],
    )
    legacy = notification.to_dict()
    legacy["callback"] = {
        "capability": "consent_grant",
        "params": {"operation": "task.write"},
    }
    path = tmp_path / "req_legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = store.get_notification("req_legacy")
    assert loaded is not None
    assert loaded.callback == {
        "skill": "consent_grant",
        "params": {"operation": "task.write"},
    }
    assert "capability" not in loaded.to_dict()["callback"]

    store.mark_delivered("req_legacy", "dashboard")
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["callback"]["skill"] == "consent_grant"
    assert "capability" not in rewritten["callback"]


def test_new_notification_write_canonicalizes_deprecated_callback_input(
    tmp_path, monkeypatch,
):
    from work_buddy.notifications import store
    from work_buddy.notifications.models import Notification, ResponseType

    monkeypatch.setattr(store, "_get_store_dir", lambda: tmp_path)
    notification = Notification(
        notification_id="req_new_from_cached_client",
        title="Cached client callback",
        response_type=ResponseType.CHOICE.value,
        choices=[{"key": "yes", "label": "Yes"}],
        callback={"capability": "consent_grant", "params": {}},
    )

    created = store.create_notification(notification)

    assert created.to_dict()["callback"] == {
        "skill": "consent_grant",
        "params": {},
    }
    stored = json.loads(
        (tmp_path / "req_new_from_cached_client.json").read_text(encoding="utf-8")
    )
    assert stored["callback"] == {"skill": "consent_grant", "params": {}}


def test_notification_callback_canonical_key_wins_even_when_empty():
    from work_buddy.notifications.models import Notification

    notification = Notification(
        callback={
            "skill": "",
            "capability": "legacy_should_not_override",
            "params": {},
        }
    )

    assert notification.to_dict()["callback"] == {"skill": "", "params": {}}
