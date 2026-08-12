from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from work_buddy.document_kernel.domain_service import DomainContentStoreManager
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.cowork_integration import apply_bound_direct_push
from work_buddy.document_kernel.protocol import sha256_bytes
from work_buddy.sources import ActorRef, SourceRef, SourceStore
from work_buddy.task_notes import (
    AuthorityState,
    ProjectionState,
    TaskNoteContentError,
    TaskNoteMigrationStore,
)
from work_buddy.task_notes.migration import (
    BoundTaskNoteReader,
    TaskNoteProjectionWorker,
    TaskNoteShadowImporter,
)
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth import documents, ydoc_store
from tests.unit.task_notes.support import current_journal_exit_evidence


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def _services(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "tasks" / "notes").mkdir(parents=True)
    migration = TaskNoteMigrationStore(tmp_path / "migration.db")
    sources = SourceStore.create(tmp_path / "sources")
    tenant = "vault-test-0001"
    principal = ActorRef(
        sources.authority_id,
        "task-note-migration",
        "service",
        tenant,
    )
    stores = DomainContentStoreManager(
        root=tmp_path / "domain-content",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    return vault, migration, sources, principal, stores


def test_exact_shadow_import_records_unknown_author_and_binds_without_cutover(
    tmp_path: Path,
) -> None:
    vault, migration, sources, principal, stores = _services(tmp_path)
    note_uuid = "stable-note-0001"
    body = "---\r\ntype: task-note\r\n---\r\n# Café 🧭\r\n\r\nBody.\r\n"
    path = vault / "tasks" / "notes" / f"{note_uuid}.md"
    path.write_bytes(body.encode("utf-8"))

    with TaskNoteShadowImporter(
        vault_root=vault,
        migration_store=migration,
        source_store=sources,
        principal=principal,
        stores=stores,
    ) as importer:
        result = importer.shadow_import(note_uuid)
        shadow = migration.get_task_note(note_uuid)
        assert shadow is not None and shadow.source_content_sha256 is not None
        migration.set_gate("task_note_cutover_gate", True)
        advanced, bound = importer.cutover(
            note_uuid,
            domain_revision=shadow.source_content_sha256,
            rollback_deadline="2099-01-01T00:00:00+00:00",
            journal_exit_evidence=current_journal_exit_evidence(),
        )
        assert advanced.epoch == bound.content_authority_epoch == 1
        rolled_back, legacy_binding = importer.rollback(
            note_uuid,
            domain_revision="legacy-2",
        )
        assert rolled_back.epoch == legacy_binding.content_authority_epoch == 2
        assert importer.rollback(
            note_uuid,
            domain_revision="legacy-2",
        ) == (rolled_back, legacy_binding)

    assert result.normalized_parity is True
    record = migration.get_task_note(note_uuid)
    assert record is not None
    assert record.comparison_state.value == "parity"
    authority = migration.get_authority("tasks", "task_note", note_uuid)
    assert authority is not None and authority.state is AuthorityState.LEGACY
    source_ref = SourceRef.parse(result.source_ref)
    item = sources.get_item(source_ref)
    assert item is not None
    assert item.source_role == "imported_file"
    assert item.origin_ref is not None
    assert item.origin_ref.provider_id == "work-buddy-file-import"
    conn = sources.connect()
    try:
        attributions = sources.current_attributions(conn, source_ref)
    finally:
        conn.close()
    assert any(
        assertion.role == "author" and assertion.state == "unknown"
        for assertion in attributions
    )


def test_projection_recovers_ambiguous_write_and_pauses_external_divergence(
    tmp_path: Path,
) -> None:
    vault, migration, sources, principal, stores = _services(tmp_path)
    note_uuid = "stable-note-0002"
    path = vault / "tasks" / "notes" / f"{note_uuid}.md"
    path.write_text("# Task\n\nOriginal.\n", encoding="utf-8")
    with TaskNoteShadowImporter(
        vault_root=vault,
        migration_store=migration,
        source_store=sources,
        principal=principal,
        stores=stores,
    ) as importer:
        importer.shadow_import(note_uuid)
        shadow = migration.get_task_note(note_uuid)
        assert shadow is not None and shadow.source_content_sha256 is not None
        migration.set_gate("task_note_cutover_gate", True)
        epoch, binding = importer.cutover(
            note_uuid,
            domain_revision=shadow.source_content_sha256,
            rollback_deadline="2099-01-01T00:00:00+00:00",
            journal_exit_evidence=current_journal_exit_evidence(),
        )
        assert epoch.epoch == binding.content_authority_epoch == 1

    reader = BoundTaskNoteReader(
        vault_root=vault,
        migration_store=migration,
        stores=stores,
    )
    worker = TaskNoteProjectionWorker(
        vault_root=vault,
        migrations=migration,
        source_store=sources,
        principal=principal,
        reader=reader,
    )
    projected = worker.project(note_uuid)
    assert projected.state is ProjectionState.CURRENT
    rendered = path.read_text(encoding="utf-8")
    assert "wb:cowork-task-note/v1" in rendered

    # Simulate a crash after the exact file replacement but before receipt by
    # rewinding only the migration cursor.  The deterministic marker/result is
    # recognized on retry and no second write is required.
    with migration.transaction() as conn:
        conn.execute(
            "UPDATE task_note_migrations SET projection_state='none',"
            "projection_result_sha256=NULL,projection_document_head=NULL,"
            "projection_generation=0 WHERE note_uuid=?",
            (note_uuid,),
        )
    recovered = worker.project(note_uuid)
    assert recovered.state is ProjectionState.CURRENT

    path.write_text(
        path.read_text(encoding="utf-8").replace("Original.", "External edit."),
        encoding="utf-8",
    )
    paused = worker.project(note_uuid)
    assert paused.state is ProjectionState.PAUSED_DIVERGED
    assert paused.divergence_source_ref is not None
    assert "External edit." in path.read_text(encoding="utf-8")
    item = sources.get_item(SourceRef.parse(paused.divergence_source_ref))
    assert item is not None and item.source_role == "imported_file"


def test_cutover_rejects_file_changed_after_parity(tmp_path: Path) -> None:
    vault, migration, sources, principal, stores = _services(tmp_path)
    note_uuid = "stable-note-stale"
    path = vault / "tasks" / "notes" / f"{note_uuid}.md"
    path.write_text("# Task\n\nReviewed body.\n", encoding="utf-8")
    with TaskNoteShadowImporter(
        vault_root=vault,
        migration_store=migration,
        source_store=sources,
        principal=principal,
        stores=stores,
    ) as importer:
        importer.shadow_import(note_uuid)
        record = migration.get_task_note(note_uuid)
        assert record is not None and record.source_content_sha256 is not None
        migration.set_gate("task_note_cutover_gate", True)
        path.write_text("# Task\n\nChanged after review.\n", encoding="utf-8")
        with pytest.raises(TaskNoteContentError, match="changed after parity"):
            importer.cutover(
                note_uuid,
                domain_revision=record.source_content_sha256,
                rollback_deadline="2099-01-01T00:00:00+00:00",
                journal_exit_evidence=current_journal_exit_evidence(),
            )

    authority = migration.get_authority("tasks", "task_note", note_uuid)
    assert authority is not None and authority.state is AuthorityState.SHADOW
    domain_store = stores.ensure(vault)
    binding = DocumentCausalityStore(domain_store.paths.sidecar).binding_for_document(
        record.store_id, record.document_id  # type: ignore[arg-type]
    )
    assert binding is not None and binding.content_authority == "domain"


def test_authority_mirror_recovers_crashes_after_cutover_and_rollback(
    tmp_path: Path, monkeypatch
) -> None:
    vault, migration, sources, principal, stores = _services(tmp_path)
    note_uuid = "stable-note-authority-recovery"
    path = vault / "tasks" / "notes" / f"{note_uuid}.md"
    path.write_text("# Task\n\nStable body.\n", encoding="utf-8")
    with TaskNoteShadowImporter(
        vault_root=vault,
        migration_store=migration,
        source_store=sources,
        principal=principal,
        stores=stores,
    ) as importer:
        importer.shadow_import(note_uuid)
        record = migration.get_task_note(note_uuid)
        assert record is not None and record.source_content_sha256 is not None
        migration.set_gate("task_note_cutover_gate", True)
        original_mirror = migration.mirror_authority
        monkeypatch.setattr(
            migration,
            "mirror_authority",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("stop after canonical cutover")
            ),
        )
        with pytest.raises(RuntimeError, match="canonical cutover"):
            importer.cutover(
                note_uuid,
                domain_revision=record.source_content_sha256,
                rollback_deadline="2099-01-01T00:00:00+00:00",
                journal_exit_evidence=current_journal_exit_evidence(),
            )
        assert migration.get_authority(
            "tasks", "task_note", note_uuid
        ).state is AuthorityState.SHADOW  # type: ignore[union-attr]
        assert migration.get_authority(
            "tasks", "task_note", note_uuid
        ).rollback_deadline == "2099-01-01T00:00:00+00:00"  # type: ignore[union-attr]
        monkeypatch.setattr(migration, "mirror_authority", original_mirror)
        repaired = importer.recover_authority(note_uuid)
        assert repaired is not None
        assert repaired.state is AuthorityState.COWORK and repaired.epoch == 1
        assert repaired.rollback_deadline == "2099-01-01T00:00:00+00:00"

        monkeypatch.setattr(
            migration,
            "mirror_authority",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("stop after canonical rollback")
            ),
        )
        with pytest.raises(RuntimeError, match="canonical rollback"):
            importer.rollback(
                note_uuid,
                domain_revision=record.source_content_sha256,
            )
        assert migration.get_authority(
            "tasks", "task_note", note_uuid
        ).state is AuthorityState.COWORK  # type: ignore[union-attr]
        monkeypatch.setattr(migration, "mirror_authority", original_mirror)
        repaired = importer.recover_authority(note_uuid)
        assert repaired is not None
        assert repaired.state is AuthorityState.LEGACY and repaired.epoch == 2
        assert "wb:cowork-task-note/v1" not in path.read_text(encoding="utf-8")
        assert migration.get_task_note(
            note_uuid
        ).projection_state is ProjectionState.NONE  # type: ignore[union-attr]


def test_direct_editor_push_projects_and_rollback_preserves_latest_head(
    tmp_path: Path,
) -> None:
    vault, migration, sources, principal, stores = _services(tmp_path)
    note_uuid = "stable-note-direct-edit"
    path = vault / "tasks" / "notes" / f"{note_uuid}.md"
    path.write_text("# Task\n\nOriginal body.\n", encoding="utf-8")
    with TaskNoteShadowImporter(
        vault_root=vault,
        migration_store=migration,
        source_store=sources,
        principal=principal,
        stores=stores,
    ) as importer:
        importer.shadow_import(note_uuid)
        record = migration.get_task_note(note_uuid)
        assert record is not None and record.source_content_sha256 is not None
        migration.set_gate("task_note_cutover_gate", True)
        importer.cutover(
            note_uuid,
            domain_revision=record.source_content_sha256,
            rollback_deadline="2099-01-01T00:00:00+00:00",
            journal_exit_evidence=current_journal_exit_evidence(),
        )

    store = stores.ensure(vault)
    document = documents.get_document(store, record.document_id or "")
    assert document.ydoc_snapshot_sha256 is not None
    snapshot = ydoc_store.read_snapshot(
        store, snapshot_sha256=document.ydoc_snapshot_sha256
    )
    updates, _ = ydoc_store.read_updates(store, document_id=document.id)
    base_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    kernel = DocumentKernelClient()
    try:
        candidate = kernel.request(
            {
                "kind": "replace_text",
                "snapshotBase64": snapshot,
                "updatesBase64": updates,
                "expectedBaseStructuredHeadSha256": base_head,
                "selector": {
                    "kind": "prosemirror_text/v1",
                    "from": 1,
                    "to": 5,
                    "expectedText": "Task",
                },
                "copiedText": "Updated task",
                "copiedTextSha256": sha256_bytes(b"Updated task"),
            },
            request_id="task_note_direct_candidate_01",
        )
    finally:
        kernel.close()
    assert candidate.update is not None
    pushed = apply_bound_direct_push(
        store,
        document,
        update=candidate.update,
        expected_head=base_head,
        expected_generation=documents.current_ydoc_generation(store, document.id),
        actors={"input_by": "human:test"},
        input_assurance="direct_human_input",
        source_store=sources,
        source_principal=principal,
        task_migration_store=migration,
        vault_root=vault,
    )
    assert pushed is not None and pushed.projection is not None
    assert pushed.projection.status == "committed"
    assert "Updated task" in path.read_text(encoding="utf-8")
    assert "wb:cowork-task-note/v1" in path.read_text(encoding="utf-8")

    # A new importer models process restart. Rollback must use the durable
    # document head, not the compatibility file hash supplied by old callers.
    with TaskNoteShadowImporter(
        vault_root=vault,
        migration_store=migration,
        source_store=sources,
        principal=principal,
        stores=stores,
    ) as importer:
        rolled_back, binding = importer.rollback(
            note_uuid,
            domain_revision="obsolete-file-digest",
        )
    assert rolled_back.state is AuthorityState.LEGACY
    assert binding.domain_revision == pushed.change.result_structured_head_sha256
    rendered = path.read_text(encoding="utf-8")
    assert "Updated task" in rendered
    assert "wb:cowork-task-note/v1" not in rendered
