from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from work_buddy.document_kernel.protocol import sha256_bytes, structured_head_sha256
from work_buddy.ir.sources.task_notes import TaskNoteSource
from work_buddy.tasks import capabilities, runtime
from work_buddy.tasks.documents import TaskDocumentService, TaskDocumentStoreManager
from work_buddy.tasks.models import TaskDocumentLink
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.registry import TruthStoreRegistry


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def test_native_ir_and_task_read_follow_live_cowork_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_path = tmp_path / "tasks.db"
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: task_path)
    monkeypatch.setattr(
        runtime,
        "_canonical_default_latch_path",
        lambda: tmp_path / "task_authority_latch.json",
    )
    monkeypatch.setattr(
        "work_buddy.tasks.store.default_task_db_path",
        lambda: task_path,
    )
    task_store = TaskStore(task_path)
    task_store.initialize()
    state = task_store.system_state()
    runtime.arm_native_authority_latch(
        task_store.path,
        cohort_id="ir-live-head-test",
        target_authority_epoch="native:1",
        cutover_receipt_id="ir-live-head-cutover",
        armed_at="2026-08-23T11:59:59+00:00",
    )
    task_store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:1",
        updated_at="2026-08-23T12:00:00+00:00",
        cutover_receipt_id="ir-live-head-cutover",
        process_generation=1,
    )
    task_service = TaskApplicationService(
        task_store,
        clock=lambda: datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
    )
    task = task_service.create(
        description="Index native knowledge",
        task_id="t-ir-live-head",
        client_mutation_id="create-ir-live-head",
        actor="human:test",
    ).task
    manager = TaskDocumentStoreManager(
        root=tmp_path / "task-knowledge",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    document_service = TaskDocumentService(stores=manager)
    created = document_service.create(
        task_id=task.task_id,
        title=task.description,
        domain_revision=str(task.revision),
        created_by="human:test",
        initial_markdown="# Native knowledge\n\nOld indexed body.\n",
    )
    note_uuid = str(uuid.UUID(hex=created.document_id))
    now = "2026-08-23T12:00:00+00:00"
    task_service.attach_document(
        task.task_id,
        TaskDocumentLink(
            task_id=task.task_id,
            note_uuid=note_uuid,
            store_id=created.store_id,
            document_id=created.document_id,
            binding_id=created.binding_id,
            lifecycle="active",
            created_at=now,
            updated_at=now,
        ),
        expected_revision=task.revision,
        client_mutation_id="attach-ir-live-head",
        actor="human:test",
    )

    monkeypatch.setattr(runtime, "default_task_db_path", lambda: task_store.path)
    monkeypatch.setattr(
        "work_buddy.tasks.store.default_task_db_path",
        lambda: task_store.path,
    )
    monkeypatch.setattr(
        "work_buddy.tasks.documents.TaskDocumentStoreManager",
        lambda: manager,
    )

    source = TaskNoteSource()
    first_discovery = source.discover()
    assert first_discovery == [(f"task_note:{note_uuid}", first_discovery[0][1])]
    first_document = source.parse(f"task_note:{note_uuid}")[0]
    assert "Old indexed body." in first_document.fields["body"]
    assert "file_path" not in first_document.metadata

    cowork_store = manager.open_existing()
    record = documents.get_document(cowork_store, created.document_id)
    assert record.ydoc_snapshot_sha256 is not None
    snapshot = ydoc_store.read_snapshot(
        cowork_store,
        snapshot_sha256=record.ydoc_snapshot_sha256,
    )
    updates, _cursor = ydoc_store.read_updates(
        cowork_store,
        document_id=record.id,
    )
    base_head = structured_head_sha256(snapshot, updates)
    edited = b"# Native knowledge\n\nFresh uncompacted body.\n"
    outcome = document_service.kernel.request(
        {
            "kind": "apply_source_markdown",
            "snapshotBase64": snapshot,
            "updatesBase64": updates,
            "expectedBaseStructuredHeadSha256": base_head,
            "sourceBase64": edited,
            "sourceSha256": sha256_bytes(edited),
            "newlineStyle": "lf",
            "utf8Bom": False,
            "trailingNewlineCount": 1,
        },
        request_id="ir_live_head_edit",
    )
    assert outcome.update is not None
    ydoc_store.append_update_cas(
        cowork_store,
        document_id=record.id,
        snapshot_sha256=record.ydoc_snapshot_sha256,
        update=outcome.update,
        expected_structured_head_sha256=base_head,
    )

    second_discovery = source.discover()
    assert second_discovery[0][1] != first_discovery[0][1]
    refreshed = source.parse(f"task_note:{note_uuid}")[0]
    assert "Fresh uncompacted body." in refreshed.fields["body"]
    assert "file_path" not in refreshed.metadata
    read = capabilities.task_read(task.task_id)
    assert "Fresh uncompacted body." in read["note_content"]
