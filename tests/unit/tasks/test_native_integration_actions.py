from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from work_buddy.collectors.chrome_thread_actions import (
    chrome_route_to_tasks,
    chrome_route_to_umbrella_task,
)
from work_buddy.email.thread_actions import (
    email_create_tasks,
    email_create_umbrella_task,
    email_record_into_task,
)
from work_buddy.journal_backlog.thread_actions import journal_route_to_tasks
from work_buddy.journal_backlog.route import _create_task_impl
from work_buddy.tasks import capabilities, integration_results, runtime
from work_buddy.tasks import store as task_store_module
from work_buddy.tasks.documents import TaskKnowledgeDocument
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore
from work_buddy.threads import models, store as thread_store


@pytest.fixture()
def thread_db(tmp_path, monkeypatch):
    monkeypatch.setattr(thread_store, "_db_path", lambda: tmp_path / "threads.db")


@pytest.fixture()
def native_result(monkeypatch):
    link = SimpleNamespace(
        task_id="t-native-1",
        note_uuid="note-native-1",
        store_id="store-native-1",
        document_id="document-native-1",
        binding_id="binding-native-1",
        lifecycle="active",
        to_dict=lambda: {
            "task_id": "t-native-1",
            "note_uuid": "note-native-1",
            "store_id": "store-native-1",
            "document_id": "document-native-1",
            "binding_id": "binding-native-1",
            "lifecycle": "active",
        },
    )
    monkeypatch.setattr(
        integration_results,
        "TaskStore",
        lambda: SimpleNamespace(get_task_document_link=lambda _task_id: link),
    )
    monkeypatch.setattr(runtime, "native_authority_active", lambda _path=None: True)
    return {
        "success": True,
        "task_id": "t-native-1",
        "task": {"task_id": "t-native-1", "revision": 3},
        "revision": 3,
        "collection_revision": 17,
        "receipt": {"receipt_id": "receipt-native-1", "status": "committed"},
        "replayed": False,
    }


def _thread_with_item(*, source: str, item_id: str, label: str, payload: dict):
    thread = models.Thread(
        context_items=(
            models.ContextItem(
                id=item_id,
                source=source,
                type="item",
                label=label,
                payload=payload,
            ),
        ),
        inciting_event_summary={"title": f"Umbrella {label}"},
    )
    thread_store.insert_thread(thread)
    return thread


def _assert_native_creation(entry: dict) -> None:
    assert entry["task_id"] == "t-native-1"
    assert entry["revision"] == 3
    assert entry["collection_revision"] == 17
    assert entry["receipt"]["receipt_id"] == "receipt-native-1"
    assert entry["knowledge_document"]["document_id"] == "document-native-1"
    assert "task_line" not in entry
    assert "file" not in entry
    assert "note_path" not in entry


def test_chrome_native_task_actions_return_receipts_and_document_metadata(
    thread_db,
    native_result,
):
    thread = _thread_with_item(
        source="chrome_tab",
        item_id="tab-1",
        label="Useful reference",
        payload={"title": "Useful reference", "url": "https://example.test"},
    )
    with patch.object(models.Task, "create", return_value=native_result):
        per_item = chrome_route_to_tasks(thread.thread_id)
        umbrella = chrome_route_to_umbrella_task(thread.thread_id)

    _assert_native_creation(per_item["created"][0])
    _assert_native_creation(umbrella["created"])
    assert umbrella["created"]["tab_count"] == 1


def test_email_native_task_actions_return_receipts_and_document_metadata(
    thread_db,
    native_result,
):
    thread = _thread_with_item(
        source="email_message",
        item_id="email-1",
        label="Please review",
        payload={"subject": "Please review", "sender": "sender@example.test"},
    )
    with patch.object(models.Task, "create", return_value=native_result):
        per_item = email_create_tasks(thread.thread_id)
        umbrella = email_create_umbrella_task(thread.thread_id)

    _assert_native_creation(per_item["created"][0])
    _assert_native_creation(umbrella["created"])
    assert umbrella["created"]["email_count"] == 1


def test_email_native_document_append_returns_task_revision_and_document(
    thread_db,
    native_result,
):
    thread = _thread_with_item(
        source="email_message",
        item_id="email-1",
        label="Please review",
        payload={"subject": "Please review", "sender": "sender@example.test"},
    )
    document = integration_results.native_creation_result(native_result)[
        "knowledge_document"
    ]
    appended = TaskKnowledgeDocument(
        task_id="t-native-1",
        store_id="store-native-1",
        document_id="document-native-1",
        binding_id="binding-native-1",
        title="Task knowledge",
        lifecycle="active",
    )
    with (
        patch(
            "work_buddy.tasks.capabilities.task_read",
            return_value={
                "success": True,
                "task_id": "t-native-1",
                "metadata": {"revision": 3},
                "knowledge_document": document,
            },
        ),
        patch(
            "work_buddy.tasks.documents.TaskDocumentService.append_markdown",
            return_value=appended,
        ),
    ):
        result = email_record_into_task(
            thread.thread_id,
            target_task_id="t-native-1",
        )

    assert result["appended"] is True
    assert result["task_id"] == "t-native-1"
    assert result["revision"] == 3
    assert result["document"]["document_id"] == "document-native-1"
    assert result["document"]["note_uuid"] == "note-native-1"
    assert "note_path" not in result


def test_email_native_empty_append_result_never_claims_a_note_path(
    thread_db,
    native_result,
):
    thread = models.Thread(context_items=())
    thread_store.insert_thread(thread)

    result = email_record_into_task(
        thread.thread_id,
        target_task_id="t-native-1",
    )

    assert result["task_id"] == "t-native-1"
    assert result["document"] is None
    assert "note_path" not in result


def test_journal_native_route_needs_no_vault_and_returns_native_envelope(
    thread_db,
    native_result,
):
    thread = _thread_with_item(
        source="journal_segment",
        item_id="journal-1",
        label="Follow up on research",
        payload={"raw_text": "Follow up on research"},
    )
    with patch.object(models.Task, "create", return_value=native_result):
        result = journal_route_to_tasks(thread.thread_id)

    _assert_native_creation(result["created"][0])


def test_journal_native_create_wording_has_no_master_list_claim(native_result):
    with patch.object(models.Task, "create", return_value=native_result):
        result = _create_task_impl("Follow up", None)

    _assert_native_creation(result)
    assert result["message"] == "Task created"
    assert "master-task-list" not in result["message"]


def test_legacy_integration_result_keeps_task_line(thread_db, monkeypatch):
    monkeypatch.setattr(runtime, "native_authority_active", lambda _path=None: False)
    thread = _thread_with_item(
        source="chrome_tab",
        item_id="tab-legacy",
        label="Legacy tab",
        payload={"title": "Legacy tab", "url": "https://example.test"},
    )
    with patch.object(
        models.Task,
        "create",
        return_value={"success": True, "task_line": "- [ ] Legacy tab"},
    ):
        result = chrome_route_to_tasks(thread.thread_id)

    assert result["created"] == [
        {"item_id": "tab-legacy", "task_line": "- [ ] Legacy tab"}
    ]


def test_has_deadline_round_trips_through_native_model_facade_and_capability(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "tasks.db"
    monkeypatch.setattr(task_store_module, "default_task_db_path", lambda: path)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: path)
    monkeypatch.setattr(
        runtime,
        "_canonical_default_latch_path",
        lambda: tmp_path / "task_authority_latch.json",
    )
    store = TaskStore(path)
    store.initialize()
    state = store.system_state()
    runtime.arm_native_authority_latch(
        path,
        cohort_id="integration-actions-test",
        target_authority_epoch="native:test",
        cutover_receipt_id="cutover-test",
        armed_at=datetime.now(timezone.utc).isoformat(),
    )
    store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:test",
        updated_at=datetime.now(timezone.utc).isoformat(),
        cutover_receipt_id="cutover-test",
        process_generation=1,
    )
    task = TaskApplicationService(store).create(
        description="Honor the hard deadline",
        deadline_date="2026-09-15",
        client_mutation_id="deadline-create",
        actor="agent:test",
    ).task

    assert task.has_deadline is True
    assert task.to_dict()["has_deadline"] is True
    facade = models.Task.load(task.task_id)
    assert facade is not None
    assert facade.has_deadline is True
    payload = capabilities.task_read(task.task_id)
    assert payload["has_deadline"] is True
    assert payload["metadata"]["has_deadline"] is True
