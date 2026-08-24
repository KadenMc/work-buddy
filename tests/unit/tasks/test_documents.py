from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.protocol import sha256_bytes, structured_head_sha256
from work_buddy.document_kernel.cowork_integration import project_bound_document
from work_buddy.tasks.documents import TaskDocumentService, TaskDocumentStoreManager
from work_buddy.tasks.store import TaskStore
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.registry import TruthStoreRegistry


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def _service(tmp_path: Path) -> tuple[TaskDocumentService, TaskDocumentStoreManager]:
    manager = TaskDocumentStoreManager(
        root=tmp_path / "task-knowledge",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    return TaskDocumentService(stores=manager), manager


def test_task_document_is_idempotent_registered_and_has_no_projection_path(
    tmp_path: Path,
) -> None:
    service, manager = _service(tmp_path)

    first = service.create(
        task_id="t-native-doc",
        title="Build the native Tasks view",
        domain_revision="1",
        created_by="service:tasks",
    )
    second = service.create(
        task_id="t-native-doc",
        title="Build the native Tasks view",
        domain_revision="1",
        created_by="service:tasks",
    )

    assert second == first
    assert service.get("t-native-doc") == first
    assert first.href == (
        f"/app/cowork?store_id={first.store_id}&document_id={first.document_id}"
    )
    store = manager.open_existing()
    binding = DocumentCausalityStore(store.paths.sidecar).get_binding(first.binding_id)
    assert binding is not None
    assert binding.content_authority == "co_work"
    assert binding.projection_mode == "none"
    assert binding.projection_path is None
    assert project_bound_document(
        store,
        binding=binding,
        change=None,
        source_store=object(),  # type: ignore[arg-type]
        source_principal=object(),  # type: ignore[arg-type]
    ) is None
    assert list((tmp_path / "task-knowledge").rglob("*.md")) == []


def test_task_document_store_does_not_need_a_vault(tmp_path: Path) -> None:
    service, manager = _service(tmp_path)
    created = service.create(
        task_id="t-no-vault",
        title="No vault required",
        domain_revision="1",
        created_by="human:local",
    )

    registered = manager.registry.open_store(created.store_id)
    assert registered.store_id == created.store_id
    assert registered.paths.root == (tmp_path / "task-knowledge").resolve()


def test_default_manager_follows_cutover_pinned_store_after_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    relocated = TaskDocumentStoreManager(
        root=tmp_path / "relocated-task-knowledge",
        registry=registry,
    )
    store = relocated.ensure()
    task_db = tmp_path / "tasks.db"
    task_store = TaskStore(task_db)
    task_store.initialize()
    state = task_store.system_state()
    task_store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:1",
        cowork_task_store_id=store.store_id,
        cutover_receipt_id="cutover-test",
        updated_at="2026-08-23T12:00:00+00:00",
    )

    monkeypatch.setattr(
        "work_buddy.tasks.documents.default_task_db_path",
        lambda: task_db,
    )
    monkeypatch.setattr(
        "work_buddy.tasks.documents.resolve",
        lambda _name: tmp_path / "unused-default-task-knowledge",
    )
    reopened = TaskDocumentStoreManager(registry=registry).open_existing()

    assert reopened.store_id == store.store_id
    assert reopened.paths.root == (tmp_path / "relocated-task-knowledge").resolve()


def test_task_document_can_bootstrap_user_authored_content_without_a_markdown_file(
    tmp_path: Path,
) -> None:
    service, manager = _service(tmp_path)
    created = service.create(
        task_id="t-authored-note",
        title="Authored knowledge",
        domain_revision="1",
        created_by="human:local",
        initial_markdown="# Authored knowledge\n\nKeep this context.\n",
    )

    store = manager.open_existing()
    record = documents.get_document(store, created.document_id)
    projection = store.resolve_blob_path(f"blobs/{record.content_sha256}").read_text(
        encoding="utf-8"
    )
    assert "Keep this context." in projection
    assert list((tmp_path / "task-knowledge").rglob("*.md")) == []


def test_task_document_append_is_projection_free_and_response_retry_safe(
    tmp_path: Path,
) -> None:
    service, manager = _service(tmp_path)
    created = service.create(
        task_id="t-append-context",
        title="Append context",
        domain_revision="1",
        created_by="human:local",
        initial_markdown="# Append context\n",
    )

    first = service.append_markdown(
        task_id="t-append-context",
        markdown="## Recorded email\n\n- A durable detail",
        actor_ref="service:email",
        idempotency_key="email-thread:one",
    )
    replay = service.append_markdown(
        task_id="t-append-context",
        markdown="## Recorded email\n\n- A durable detail",
        actor_ref="service:email",
        idempotency_key="email-thread:one",
    )

    assert first.document_id == replay.document_id == created.document_id
    store = manager.open_existing()
    record = documents.get_document(store, created.document_id)
    projection = store.resolve_blob_path(f"blobs/{record.content_sha256}").read_text(
        encoding="utf-8"
    )
    assert projection.count("## Recorded email") == 1
    assert "A durable detail" in projection
    assert list((tmp_path / "task-knowledge").rglob("*.md")) == []


def test_task_document_read_projects_uncompacted_editor_tail(tmp_path: Path) -> None:
    service, manager = _service(tmp_path)
    created = service.create(
        task_id="t-live-head",
        title="Live head",
        domain_revision="1",
        created_by="human:local",
        initial_markdown="# Live head\n\nOld body.\n",
    )
    store = manager.open_existing()
    record = documents.get_document(store, created.document_id)
    assert record.ydoc_snapshot_sha256 is not None
    snapshot = ydoc_store.read_snapshot(
        store,
        snapshot_sha256=record.ydoc_snapshot_sha256,
    )
    updates, _cursor = ydoc_store.read_updates(store, document_id=record.id)
    base_head = structured_head_sha256(snapshot, updates)
    source = b"# Live head\n\nEdited in Co-work.\n"
    edit = service.kernel.request(
        {
            "kind": "apply_source_markdown",
            "snapshotBase64": snapshot,
            "updatesBase64": updates,
            "expectedBaseStructuredHeadSha256": base_head,
            "sourceBase64": source,
            "sourceSha256": sha256_bytes(source),
            "newlineStyle": "lf",
            "utf8Bom": False,
            "trailingNewlineCount": 1,
        },
        request_id="task_live_head_edit",
    )
    assert edit.update is not None
    ydoc_store.append_update_cas(
        store,
        document_id=record.id,
        snapshot_sha256=record.ydoc_snapshot_sha256,
        update=edit.update,
        expected_structured_head_sha256=base_head,
    )

    # The materialized pointer intentionally remains stale until Save, while
    # native readers must still observe the live authoritative update tail.
    assert documents.get_document(store, record.id).content_sha256 == record.content_sha256
    assert service.read_markdown("t-live-head") == source.decode("utf-8")
