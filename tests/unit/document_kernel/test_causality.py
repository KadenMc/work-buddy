from __future__ import annotations

import copy
from pathlib import Path

import pytest

from work_buddy.document_kernel.causality import (
    BindingConflict,
    ChangeConflict,
    DocumentCausalityError,
    DocumentCausalityStore,
)


def _binding(store: DocumentCausalityStore, *, document_id: str = "2" * 32):
    return store.ensure_binding(
        domain_namespace="journal",
        domain_kind="running_note",
        domain_entity_id="1" * 32,
        domain_revision="revision-1",
        store_id="3" * 32,
        document_id=document_id,
        role="running_note",
        created_by="human:profile-1",
        projection_path="Journal/2026-08-09.md",
    )


def test_binding_uniqueness_supersession_retirement_and_orphan_query(
    tmp_path: Path,
) -> None:
    store = DocumentCausalityStore(tmp_path / "first")
    first = _binding(store)
    assert _binding(store) == first
    with pytest.raises(BindingConflict):
        _binding(store, document_id="4" * 32)
    assert store.orphaned_bindings(lambda _store, document: document != first.document_id) == (
        first,
    )

    successor = store.supersede_binding(
        first.binding_id,
        domain_revision="revision-2",
        store_id="3" * 32,
        document_id="4" * 32,
        created_by="human:profile-1",
        projection_path="Journal/2026-08-09.md",
    )
    assert successor.lifecycle == "current"
    assert successor.document_id == "4" * 32
    assert store.get_binding(first.binding_id).lifecycle == "superseded"  # type: ignore[union-attr]
    assert store.supersede_binding(
        first.binding_id,
        domain_revision="revision-2",
        store_id="3" * 32,
        document_id="4" * 32,
        created_by="human:profile-1",
    ) == successor
    retired = store.retire_binding(successor.binding_id)
    assert retired.lifecycle == "retired"
    assert store.list_bindings() == ()


def test_prepared_materialized_committed_change_is_idempotent_and_immutable(
    tmp_path: Path,
) -> None:
    store = DocumentCausalityStore(tmp_path / "causality")
    binding = _binding(store)
    intent = store.prepare_change(
        idempotency_key="capture-00000001",
        operation_kind="exact_source_copy",
        store_id=binding.store_id,
        document_id=binding.document_id,
        binding_id=binding.binding_id,
        source_ref="source://authority.example/item/source-00000001",
        source_representation_id="representation-00000001",
        source_content_sha256="a" * 64,
        exact_copied_text_sha256="a" * 64,
        base_snapshot_sha256="b" * 64,
        base_structured_head_sha256="c" * 64,
        base_generation_sha256="d" * 64,
        selector={"kind": "whole_document/v1"},
        actors={"selected_by": "human:profile-1"},
    )
    assert intent.state == "prepared"
    assert store.incomplete_changes() == (intent,)
    materialized = store.record_materialized(
        intent.change_id,
        result_snapshot_sha256="e" * 64,
        result_structured_head_sha256="f" * 64,
        result_projection_sha256="1" * 64,
        result_update_sha256="2" * 64,
        operation_manifest_sha256="3" * 64,
        protocol_version="cowork-document-kernel/v1",
        runtime_version="1.0.0",
        schema_version="cowork-yjs/v1",
    )
    assert materialized.state == "materialized"
    record = store.commit_change(
        intent.change_id,
        assurance={"exact_copied_text": "document_kernel_verified"},
    )
    assert record.result_snapshot_sha256 == "e" * 64
    assert store.commit_change(intent.change_id, assurance={"different": True}) == record
    assert store.incomplete_changes() == ()
    with store.transaction() as conn:
        with pytest.raises(Exception):
            conn.execute(
                "UPDATE document_change_records SET assurance_json='{}' WHERE change_id=?",
                (intent.change_id,),
            )


def test_projection_cursor_and_export_round_trip(tmp_path: Path) -> None:
    source = DocumentCausalityStore(tmp_path / "source")
    binding = _binding(source)
    authoritative = source.cutover_to_cowork(
        binding.binding_id,
        domain_revision="revision-2",
    )
    cursor = source.initialize_projection_base(
        binding.binding_id,
        content_authority_epoch=authoritative.content_authority_epoch,
        section_sha256="4" * 64,
        file_sha256="5" * 64,
    )
    assert cursor.status == "pending"
    projection_id = source.prepare_projection(
        binding_id=binding.binding_id,
        content_authority_epoch=authoritative.content_authority_epoch,
        document_head_sha256="6" * 64,
        expected_section_sha256="4" * 64,
        result_section_sha256="7" * 64,
        result_projection_sha256="8" * 64,
    )
    committed = source.commit_projection(
        projection_id,
        base_file_sha256="5" * 64,
        result_file_sha256="9" * 64,
        result_section_sha256="7" * 64,
    )
    assert committed.status == "committed"
    assert committed.document_head_sha256 == "6" * 64

    restored = DocumentCausalityStore(tmp_path / "restored")
    restored.import_bundle(source.export_bundle())
    assert restored.get_binding(binding.binding_id) == authoritative
    assert restored.projection_cursor(binding.binding_id) == committed


def test_schema_one_export_remains_restorable_with_legacy_projection_policy(
    tmp_path: Path,
) -> None:
    source = DocumentCausalityStore(tmp_path / "source-v1")
    binding = _binding(source)
    legacy = copy.deepcopy(source.export_bundle())
    legacy["schema_version"] = 1
    for row in legacy["tables"]["domain_document_bindings"]:
        row.pop("projection_mode")

    restored = DocumentCausalityStore(tmp_path / "restored-v1")
    restored.import_bundle(legacy)

    recovered = restored.get_binding(binding.binding_id)
    assert recovered is not None
    assert recovered.projection_mode == "managed_section"


def test_no_projection_binding_is_settled_without_external_cursor(
    tmp_path: Path,
) -> None:
    source = DocumentCausalityStore(tmp_path / "source-no-projection")
    binding = source.ensure_binding(
        domain_namespace="tasks",
        domain_kind="task_knowledge",
        domain_entity_id="1" * 32,
        domain_revision="revision-1",
        store_id="3" * 32,
        document_id="2" * 32,
        role="task_knowledge",
        created_by="service:tasks",
        projection_mode="none",
    )

    assert binding.projection_mode == "none"
    assert binding.projection_path is None
    authoritative = source.cutover_to_cowork(
        binding.binding_id,
        domain_revision="revision-2",
    )
    assert authoritative.content_authority == "co_work"
    assert source.projection_cursor(binding.binding_id) is None
    with pytest.raises(ChangeConflict):
        source.prepare_projection(
            binding_id=binding.binding_id,
            content_authority_epoch=authoritative.content_authority_epoch,
            document_head_sha256="a" * 64,
            expected_section_sha256=None,
            result_section_sha256="b" * 64,
            result_projection_sha256="c" * 64,
        )

    restored = DocumentCausalityStore(tmp_path / "restored-no-projection")
    restored.import_bundle(source.export_bundle())
    assert restored.get_binding(binding.binding_id) == authoritative
    assert restored.projection_cursor(binding.binding_id) is None


def test_projection_mode_can_only_be_changed_before_cutover(tmp_path: Path) -> None:
    store = DocumentCausalityStore(tmp_path / "projection-policy")
    binding = _binding(store)
    no_projection = store.configure_projection_mode(
        binding.binding_id,
        projection_mode="none",
        projection_path=None,
    )
    assert no_projection.projection_mode == "none"
    assert no_projection.projection_path is None

    store.cutover_to_cowork(binding.binding_id, domain_revision="revision-2")
    with pytest.raises(BindingConflict):
        store.configure_projection_mode(
            binding.binding_id,
            projection_mode="managed_file",
            projection_path="tasks/notes/example.md",
        )


def test_identity_bound_recovery_bundle_requires_clean_matching_store(
    tmp_path: Path,
) -> None:
    source = DocumentCausalityStore(tmp_path / "source-recovery")
    binding = _binding(source)
    envelope = source.export_recovery_bundle(store_id=binding.store_id)

    restored = DocumentCausalityStore(tmp_path / "restored-recovery")
    restored.import_recovery_bundle(
        envelope,
        expected_store_id=binding.store_id,
        expected_document_ids={binding.document_id},
    )
    assert restored.get_binding(binding.binding_id) == binding

    with pytest.raises(DocumentCausalityError, match="target_not_empty"):
        restored.import_recovery_bundle(
            envelope,
            expected_store_id=binding.store_id,
            expected_document_ids={binding.document_id},
        )


def test_recovery_bundle_rejects_store_document_and_digest_mismatch(
    tmp_path: Path,
) -> None:
    source = DocumentCausalityStore(tmp_path / "source-mismatch")
    binding = _binding(source)
    envelope = source.export_recovery_bundle(store_id=binding.store_id)

    with pytest.raises(DocumentCausalityError, match="store_identity_mismatch"):
        DocumentCausalityStore.validate_recovery_bundle(
            envelope,
            expected_store_id="f" * 32,
        )
    with pytest.raises(DocumentCausalityError, match="document_identity_mismatch"):
        DocumentCausalityStore.validate_recovery_bundle(
            envelope,
            expected_store_id=binding.store_id,
            expected_document_ids={"e" * 32},
        )
    corrupted = dict(envelope)
    corrupted["payload_sha256"] = "0" * 64
    with pytest.raises(DocumentCausalityError, match="digest_mismatch"):
        DocumentCausalityStore.validate_recovery_bundle(
            corrupted,
            expected_store_id=binding.store_id,
        )


def test_recovery_bundle_failure_rolls_clean_target_back_to_empty(
    tmp_path: Path,
) -> None:
    source = DocumentCausalityStore(tmp_path / "source-duplicate")
    binding = _binding(source)
    envelope = copy.deepcopy(
        source.export_recovery_bundle(store_id=binding.store_id)
    )
    rows = envelope["payload"]["tables"]["domain_document_bindings"]
    rows.append(dict(rows[0]))
    envelope["payload_sha256"] = DocumentCausalityStore._bundle_sha256(
        envelope["payload"]
    )
    restored = DocumentCausalityStore(tmp_path / "restored-duplicate")

    with pytest.raises(DocumentCausalityError):
        restored.import_recovery_bundle(
            envelope,
            expected_store_id=binding.store_id,
            expected_document_ids={binding.document_id},
        )

    assert restored.export_bundle()["tables"] == {
        table: [] for table in envelope["payload"]["tables"]
    }


def test_duplicate_projection_head_cannot_change_materialized_result(tmp_path: Path) -> None:
    store = DocumentCausalityStore(tmp_path / "causality")
    binding = store.cutover_to_cowork(_binding(store).binding_id, domain_revision="r2")
    first = store.prepare_projection(
        binding_id=binding.binding_id,
        content_authority_epoch=binding.content_authority_epoch,
        document_head_sha256="a" * 64,
        expected_section_sha256=None,
        result_section_sha256="b" * 64,
        result_projection_sha256="c" * 64,
    )
    assert store.prepare_projection(
        binding_id=binding.binding_id,
        content_authority_epoch=binding.content_authority_epoch,
        document_head_sha256="a" * 64,
        expected_section_sha256=None,
        result_section_sha256="b" * 64,
        result_projection_sha256="c" * 64,
    ) == first
    with pytest.raises(ChangeConflict):
        store.prepare_projection(
            binding_id=binding.binding_id,
            content_authority_epoch=binding.content_authority_epoch,
            document_head_sha256="a" * 64,
            expected_section_sha256=None,
            result_section_sha256="d" * 64,
            result_projection_sha256="c" * 64,
        )


def test_rollback_to_domain_fences_prepared_projection_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = DocumentCausalityStore(tmp_path / "causality")
    binding = store.cutover_to_cowork(
        _binding(store).binding_id,
        domain_revision="cowork-revision",
    )
    projection_id = store.prepare_projection(
        binding_id=binding.binding_id,
        content_authority_epoch=binding.content_authority_epoch,
        document_head_sha256="a" * 64,
        expected_section_sha256=None,
        result_section_sha256="b" * 64,
        result_projection_sha256="c" * 64,
    )

    rolled_back = store.rollback_to_domain(
        binding.binding_id,
        domain_revision="legacy-revision-2",
        expected_epoch=binding.content_authority_epoch,
    )
    assert rolled_back.content_authority == "domain"
    assert rolled_back.content_authority_epoch == binding.content_authority_epoch + 1
    cursor = store.projection_cursor(binding.binding_id)
    assert cursor is not None
    assert cursor.status == "failed"
    assert cursor.document_head_sha256 is None
    with pytest.raises(ChangeConflict):
        store.commit_projection(
            projection_id,
            base_file_sha256="d" * 64,
            result_file_sha256="e" * 64,
            result_section_sha256="b" * 64,
        )

    assert store.rollback_to_domain(
        binding.binding_id,
        domain_revision="legacy-revision-2",
        expected_epoch=binding.content_authority_epoch,
    ) == rolled_back
    with pytest.raises(BindingConflict):
        store.rollback_to_domain(
            binding.binding_id,
            domain_revision="different-revision",
            expected_epoch=binding.content_authority_epoch,
        )
