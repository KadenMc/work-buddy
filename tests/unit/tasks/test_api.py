from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from flask import Flask

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.dashboard.tasks_api import create_tasks_blueprint
from work_buddy.tasks.documents import TaskDocumentService, TaskDocumentStoreManager
from work_buddy.tasks.models import TaskDocumentLink
from work_buddy.tasks.runtime import (
    activation_authority_latch_path,
    arm_native_authority_latch,
)
from work_buddy.tasks.store import TaskStore
from work_buddy.truth import documents as truth_documents
from work_buddy.truth.contracts import Actor
from work_buddy.truth.registry import TruthStoreRegistry


def _app(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.initialize()
    state = store.system_state()
    arm_native_authority_latch(
        store.path,
        cohort_id="dashboard-api-test",
        target_authority_epoch="native:test",
        cutover_receipt_id="dashboard-api-cutover",
        armed_at="2026-08-23T11:59:59+00:00",
    )
    store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:test",
        updated_at="2026-08-23T12:00:00+00:00",
        process_generation=1,
    )
    manager = TaskDocumentStoreManager(
        root=tmp_path / "task-knowledge",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    documents = TaskDocumentService(stores=manager)
    authorized: list[tuple[str, str, str, str]] = []

    def authorize(operation, subject, method, path, body):
        authorized.append((operation, subject, method, path))
        assert body["client_mutation_id"]
        return "human:local-test"

    app = Flask(__name__)
    app.testing = True
    app.config["TEST_TASK_DOCUMENT_SERVICE"] = documents
    app.register_blueprint(
        create_tasks_blueprint(
            store_factory=lambda: store,
            document_factory=lambda: documents,
            authorizer=authorize,
        )
    )
    return app, store, authorized


def _create(client, *, mutation_id: str = "create-1", title: str = "Native task"):
    return client.post(
        "/api/tasks",
        json={
            "client_mutation_id": mutation_id,
            "title": title,
            "attention_state": "inbox",
            "urgency": "medium",
            "summary": "Useful context",
            "project": "work-buddy",
            "namespaces": ["engineering"],
            "dependencies": ["Review design"],
        },
    )


def test_native_task_api_create_view_update_lifecycle_and_conflict(tmp_path: Path) -> None:
    app, _store, authorized = _app(tmp_path)
    client = app.test_client()

    created = _create(client)
    assert created.status_code == 200
    first = created.get_json()["result"]["task"]
    assert first["title"] == "Native task"
    assert first["summary"] == "Useful context"
    assert first["dependencies"] == ["Review design"]
    assert first["project"] == "work-buddy"
    task_id = first["task_id"]

    view = client.get(f"/api/tasks/view?lens=inbox&task={task_id}")
    assert view.status_code == 200
    payload = view.get_json()
    assert payload["access"] == {"mode": "read_write"}
    assert [item["task_id"] for item in payload["tasks"]] == [task_id]
    assert payload["selected_task"]["task_id"] == task_id

    updated = client.patch(
        f"/api/tasks/{task_id}",
        json={
            "client_mutation_id": "update-1",
            "expected_revision": first["revision"],
            "title": "Native task edited",
            "attention_state": "active",
            "urgency": "high",
            "summary": "Edited context",
            "project": "work-buddy",
            "namespaces": ["engineering", "dashboard"],
            "dependencies": [],
        },
    )
    assert updated.status_code == 200
    second = updated.get_json()["result"]["task"]
    assert second["title"] == "Native task edited"
    assert second["attention_state"] == "active"
    assert second["summary"] == "Edited context"
    assert second["dependencies"] == []

    stale = client.post(
        f"/api/tasks/{task_id}/complete",
        json={
            "client_mutation_id": "stale-complete",
            "expected_revision": first["revision"],
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "task_revision_conflict"

    completed = client.post(
        f"/api/tasks/{task_id}/complete",
        json={
            "client_mutation_id": "complete-1",
            "expected_revision": second["revision"],
        },
    )
    assert completed.status_code == 200
    assert completed.get_json()["result"]["task"]["completed_at"] is not None
    assert {item[0] for item in authorized} >= {"create", "update", "complete"}


def test_native_task_api_preserves_mit_and_allows_project_removal(tmp_path: Path) -> None:
    app, _store, _authorized = _app(tmp_path)
    client = app.test_client()
    created = client.post(
        "/api/tasks",
        json={
            "client_mutation_id": "create-mit",
            "title": "Protect MIT semantics",
            "attention_state": "mit",
            "urgency": "high",
            "project": "work-buddy",
        },
    ).get_json()["result"]["task"]

    assert created["attention_state"] == "mit"
    updated = client.patch(
        f"/api/tasks/{created['task_id']}",
        json={
            "client_mutation_id": "edit-mit",
            "expected_revision": created["revision"],
            "title": "MIT remains an MIT",
            "attention_state": "mit",
            "project": None,
        },
    )

    assert updated.status_code == 200
    task = updated.get_json()["result"]["task"]
    assert task["attention_state"] == "mit"
    assert task["project"] is None


def test_native_task_api_batch_is_atomic_and_replay_safe(tmp_path: Path) -> None:
    app, _store, _authorized = _app(tmp_path)
    client = app.test_client()
    preview_body = {
        "client_mutation_id": "batch-1",
        "items": [
            {"title": "First", "child_mutation_id": "batch-1:1"},
            {"title": "Second", "child_mutation_id": "batch-1:2"},
        ],
    }
    preview_response = client.post("/api/tasks/batch/preview", json=preview_body)
    assert preview_response.status_code == 200
    preview = preview_response.get_json()["preview"]
    body = {
        **preview_body,
        "preview_confirmed": True,
        "preview_token": preview["preview_token"],
        "accepted_indices": preview["accepted_indices"],
    }

    first = client.post("/api/tasks/batch", json=body)
    replay = client.post("/api/tasks/batch", json=body)

    assert first.status_code == replay.status_code == 200
    assert [item["task_id"] for item in first.get_json()["result"]["tasks"]] == [
        item["task_id"] for item in replay.get_json()["result"]["tasks"]
    ]
    assert replay.get_json()["result"]["replayed"] is True
    assert len(client.get("/api/tasks/view?lens=inbox").get_json()["tasks"]) == 2


def test_native_task_api_batch_preview_reports_existing_duplicates_and_row_errors(
    tmp_path: Path,
) -> None:
    app, _store, authorized = _app(tmp_path)
    client = app.test_client()
    assert _create(
        client,
        mutation_id="existing-create",
        title="Already tracked",
    ).status_code == 200
    authorized.clear()

    response = client.post(
        "/api/tasks/batch/preview",
        json={
            "client_mutation_id": "batch-preview-1",
            "items": [
                {"title": "Already tracked"},
                {"title": "New task"},
                {"title": "new task"},
                {"title": "", "dependencies": "not-a-list"},
            ],
        },
    )

    assert response.status_code == 200
    preview = response.get_json()["preview"]
    assert preview["accepted_indices"] == [1]
    assert preview["accepted_count"] == 1
    assert preview["rows"][0]["duplicate_reason"] == "existing_title"
    assert preview["rows"][2]["duplicate_reason"] == "batch"
    assert preview["rows"][3]["valid"] is False
    assert preview["rows"][3]["field_errors"]
    assert authorized == []


def test_native_task_api_rejects_stale_or_unpreviewed_batch_commit(tmp_path: Path) -> None:
    app, _store, _authorized = _app(tmp_path)
    client = app.test_client()
    body = {
        "client_mutation_id": "batch-stale-1",
        "items": [{"title": "Preview me"}],
    }
    preview = client.post("/api/tasks/batch/preview", json=body).get_json()["preview"]
    assert _create(client, mutation_id="intervening-create").status_code == 200

    response = client.post(
        "/api/tasks/batch",
        json={
            **body,
            "preview_confirmed": True,
            "preview_token": preview["preview_token"],
            "accepted_indices": preview["accepted_indices"],
        },
    )

    assert response.status_code == 422
    assert "preview_token" in response.get_json()["error"]["field_errors"]


def test_native_task_api_action_item_authoring_contract(tmp_path: Path) -> None:
    app, _store, authorized = _app(tmp_path)
    client = app.test_client()
    task = _create(client, mutation_id="action-task-create").get_json()["result"]["task"]

    created = client.post(
        f"/api/tasks/{task['task_id']}/action-items",
        json={
            "client_mutation_id": "action-create-1",
            "expected_revision": task["revision"],
            "text": "Draft the acceptance test",
        },
    )
    assert created.status_code == 200
    after_create = created.get_json()["result"]["task"]
    item = after_create["action_items"][0]
    assert item["text"] == "Draft the acceptance test"

    updated = client.patch(
        f"/api/tasks/{task['task_id']}/action-items/{item['action_item_id']}",
        json={
            "client_mutation_id": "action-update-1",
            "expected_revision": after_create["revision"],
            "text": "Run the acceptance test",
            "completed": True,
        },
    )
    assert updated.status_code == 200
    after_update = updated.get_json()["result"]["task"]
    assert after_update["action_items"][0]["text"] == "Run the acceptance test"
    assert after_update["action_items"][0]["completed"] is True

    deleted = client.delete(
        f"/api/tasks/{task['task_id']}/action-items/{item['action_item_id']}",
        json={
            "client_mutation_id": "action-delete-1",
            "expected_revision": after_update["revision"],
        },
    )
    assert deleted.status_code == 200
    after_delete = deleted.get_json()["result"]["task"]
    assert after_delete["action_items"][0]["deleted_at"] is not None

    restored = client.post(
        f"/api/tasks/{task['task_id']}/action-items/{item['action_item_id']}/restore",
        json={
            "client_mutation_id": "action-restore-1",
            "expected_revision": after_delete["revision"],
        },
    )
    assert restored.status_code == 200
    assert restored.get_json()["result"]["task"]["action_items"][0]["deleted_at"] is None
    assert {item[0] for item in authorized} >= {
        "action_item_create",
        "action_item_update",
        "action_item_delete",
        "action_item_restore",
    }


def test_native_task_api_rejects_mutations_while_cutover_fence_is_armed(
    tmp_path: Path,
) -> None:
    app, store, authorized = _app(tmp_path)
    state = store.system_state()
    store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch=state.authority_epoch,
        updated_at="2026-08-23T12:01:00+00:00",
        rollback_fence=True,
        process_generation=state.process_generation,
    )

    response = _create(app.test_client(), mutation_id="fenced-api-create")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "task_mutation_fenced"
    assert authorized == []


def test_native_task_api_fails_closed_when_external_authority_latch_is_missing(
    tmp_path: Path,
) -> None:
    app, store, authorized = _app(tmp_path)
    activation_authority_latch_path(store.path).unlink()
    client = app.test_client()

    view = client.get("/api/tasks/view?lens=inbox")
    created = _create(client, mutation_id="missing-latch-create")

    assert view.status_code == created.status_code == 503
    assert view.get_json()["error"]["code"] == "task_authority_unavailable"
    assert created.get_json()["error"]["code"] == "task_authority_unavailable"
    assert authorized == []


def test_native_task_api_surfaces_link_registry_failure_in_task_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.cowork.local_files import LocalFileLinkRegistry

    app, store, _authorized = _app(tmp_path)
    client = app.test_client()
    task = _create(
        client,
        mutation_id="linked-file-task-create",
        title="Task with linked metadata",
    ).get_json()["result"]["task"]
    store.upsert_task_document_link(
        TaskDocumentLink(
            task_id=task["task_id"],
            note_uuid="note-linked-file-test",
            store_id="store-linked-file-test",
            document_id="document-linked-file-test",
            binding_id="binding-linked-file-test",
            lifecycle="active",
            created_at="2026-08-23T12:00:00+00:00",
            updated_at="2026-08-23T12:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        LocalFileLinkRegistry,
        "default",
        classmethod(lambda _cls: (_ for _ in ()).throw(OSError("catalog offline"))),
    )

    response = client.get(
        f"/api/tasks/view?lens=inbox&task={task['task_id']}"
    )

    assert response.status_code == 200
    detail = response.get_json()["selected_task"]
    assert detail["local_files"] == []
    assert "unavailable" in detail["local_files_error"].casefold()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_native_task_api_provisions_projection_free_cowork_document(tmp_path: Path) -> None:
    app, store, _authorized = _app(tmp_path)
    client = app.test_client()
    created = _create(client, mutation_id="doc-task-create", title="Documented task")
    task = created.get_json()["result"]["task"]

    response = client.post(
        f"/api/tasks/{task['task_id']}/document",
        json={
            "client_mutation_id": "document-create-1",
            "expected_revision": task["revision"],
        },
    )

    assert response.status_code == 200
    detail = response.get_json()["result"]["task"]
    assert detail["document"]["state"] == "available"
    assert detail["document"]["href"].startswith("/app/cowork?")
    assert store.get_task_document_link(task["task_id"]) is not None
    replay = client.post(
        f"/api/tasks/{task['task_id']}/document",
        json={
            "client_mutation_id": "document-create-1",
            "expected_revision": task["revision"],
        },
    )
    assert replay.status_code == 200
    assert replay.get_json()["result"]["replayed"] is True
    opened = client.get(f"/api/tasks/{task['task_id']}/document")
    assert opened.status_code == 200
    assert opened.get_json()["document"]["document_id"]
    assert list((tmp_path / "task-knowledge").rglob("*.md")) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_restore_deleted_import_reactivates_document_with_new_cowork_binding(
    tmp_path: Path,
) -> None:
    app, store, _authorized = _app(tmp_path)
    client = app.test_client()
    task = _create(
        client,
        mutation_id="restore-doc-task-create",
        title="Restore retained knowledge",
    ).get_json()["result"]["task"]
    attached = client.post(
        f"/api/tasks/{task['task_id']}/document",
        json={
            "client_mutation_id": "restore-doc-create",
            "expected_revision": task["revision"],
        },
    ).get_json()["result"]["task"]
    document_service = app.config["TEST_TASK_DOCUMENT_SERVICE"]
    document_service.append_markdown(
        task_id=task["task_id"],
        markdown="## Retained context\n\nThis must survive restore.",
        actor_ref="human:test",
        idempotency_key="restore-doc-seed-content",
    )
    deleted_response = client.delete(
        f"/api/tasks/{task['task_id']}",
        json={
            "client_mutation_id": "restore-doc-delete",
            "expected_revision": attached["revision"],
        },
    )
    assert deleted_response.status_code == 200
    deleted = deleted_response.get_json()["result"]["task"]

    link = store.get_task_document_link(task["task_id"])
    assert link is not None
    cowork_store = document_service.stores.open_existing()
    causality = DocumentCausalityStore(cowork_store.paths.sidecar)
    old_binding = causality.retire_binding(link.binding_id)
    truth_documents.retire_document(
        cowork_store,
        document_id=link.document_id,
        actor=Actor(kind="system", ref="test:legacy-import"),
    )
    retired_link = TaskDocumentLink(
        task_id=link.task_id,
        note_uuid=link.note_uuid,
        store_id=link.store_id,
        document_id=link.document_id,
        binding_id=link.binding_id,
        lifecycle="retired",
        created_at=link.created_at,
        updated_at="2026-08-23T12:02:00+00:00",
        retired_at="2026-08-23T12:02:00+00:00",
    )
    store.upsert_task_document_link(retired_link)

    body = {
        "client_mutation_id": "restore-doc-restore",
        "expected_revision": deleted["revision"],
    }
    restored = client.post(f"/api/tasks/{task['task_id']}/restore", json=body)
    replay = client.post(f"/api/tasks/{task['task_id']}/restore", json=body)

    assert restored.status_code == replay.status_code == 200, replay.get_json()
    assert replay.get_json()["result"]["replayed"] is True
    current_link = store.get_task_document_link(task["task_id"])
    assert current_link is not None
    assert current_link.lifecycle == "active"
    assert current_link.binding_id != old_binding.binding_id
    assert current_link.document_id != link.document_id
    assert causality.get_binding(old_binding.binding_id).lifecycle == "retired"
    successor = causality.get_binding(current_link.binding_id)
    assert successor is not None
    assert successor.lifecycle == "current"
    assert successor.content_authority == "co_work"
    assert "This must survive restore." in document_service.read_markdown(task["task_id"])
    assert list((tmp_path / "task-knowledge").rglob("*.md")) == []
