from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from work_buddy.conversations import store as conversation_store
from work_buddy.cowork import conversation_source_dependencies
from work_buddy.cowork.conversations import ensure_document_conversation
from work_buddy.document_kernel.domain_service import DomainContentStoreManager
from work_buddy.document_kernel.redaction_dispatch import (
    CoworkDocumentSourceDispatcher,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import (
    ActorRef,
    SourceOutbox,
    SourceRef,
    SourceStore,
    redact_source,
)
from work_buddy.task_notes import (
    ChangeOperationState,
    ProjectionState,
    SourceDependencyState,
    TaskNoteMigrationConflict,
    TaskNoteMigrationStore,
    TaskNoteSourceChangeService,
)
from work_buddy.task_notes.migration import (
    BoundTaskNoteReader,
    TaskNoteShadowImporter,
)
from work_buddy.truth.registry import TruthStoreRegistry
from tests.unit.task_notes.support import current_journal_exit_evidence


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def _cut_over(tmp_path: Path, note_uuid: str = "source-note-0001"):
    vault = tmp_path / "vault"
    path = vault / "tasks" / "notes" / f"{note_uuid}.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Task\n\nLegacy body.\n", encoding="utf-8")
    migrations = TaskNoteMigrationStore(tmp_path / "migration.db")
    sources = SourceStore.create(tmp_path / "sources")
    principal = ActorRef(
        sources.authority_id,
        "task-note-migration",
        "service",
        "vault-test-0001",
    )
    stores = DomainContentStoreManager(
        root=tmp_path / "domain-content",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    with TaskNoteShadowImporter(
        vault_root=vault,
        migration_store=migrations,
        source_store=sources,
        principal=principal,
        stores=stores,
    ) as importer:
        shadow = importer.shadow_import(note_uuid)
        migrations.set_gate("task_note_cutover_gate", True)
        importer.cutover(
            note_uuid,
            domain_revision=hashlib.sha256(path.read_bytes()).hexdigest(),
            rollback_deadline="2099-01-01T00:00:00+00:00",
            journal_exit_evidence=current_journal_exit_evidence(),
        )
    return vault, path, migrations, sources, principal, stores, shadow


def test_whole_document_replace_is_source_bound_idempotent_and_projected(
    tmp_path: Path,
) -> None:
    vault, path, migrations, sources, principal, stores, shadow = _cut_over(tmp_path)
    shadow_dependency = migrations.get_source_dependency_by_consumer(
        hashlib.sha256(b"task-note-shadow:source-note-0001").hexdigest()[:32]
    )
    assert shadow_dependency is not None
    assert shadow_dependency.state is SourceDependencyState.ACKNOWLEDGED

    with TaskNoteSourceChangeService(
        vault_root=vault,
        migrations=migrations,
        sources=sources,
        principal=principal,
        stores=stores,
    ) as service:
        first = service.replace(
            "source-note-0001",
            "# Task\n\nCo-work body.\n",
            idempotency_key="replace-0001",
        )
        retried = service.replace(
            "source-note-0001",
            "# Task\n\nCo-work body.\n",
            idempotency_key="replace-0001",
        )

    assert first.operation.state is ChangeOperationState.COMPLETED
    assert retried.change.change_id == first.change.change_id
    assert retried.projection.state is ProjectionState.CURRENT
    assert first.change.source_ref is not None
    assert first.change.source_content_sha256 == first.change.exact_copied_text_sha256
    assert "Co-work body." in BoundTaskNoteReader(
        vault_root=vault,
        migration_store=migrations,
        stores=stores,
    ).read("source-note-0001")
    rendered = path.read_text(encoding="utf-8")
    assert "Co-work body." in rendered
    assert "wb:cowork-task-note/v1" in rendered
    assert migrations.get_source_dependency(
        first.operation.source_usage_id  # type: ignore[arg-type]
    ).state is SourceDependencyState.ACKNOWLEDGED  # type: ignore[union-attr]
    assert migrations.get_source_dependency(
        shadow_dependency.usage_id
    ).state is SourceDependencyState.RELEASED  # type: ignore[union-attr]

    with TaskNoteSourceChangeService(
        vault_root=vault,
        migrations=migrations,
        sources=sources,
        principal=principal,
        stores=stores,
    ) as service:
        with pytest.raises(TaskNoteMigrationConflict):
            service.replace(
                "source-note-0001",
                "# Task\n\nDifferent reuse.\n",
                idempotency_key="replace-0001",
            )


def test_replace_recovers_after_document_commit_before_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, path, migrations, sources, principal, stores, _shadow = _cut_over(
        tmp_path, "source-note-recovery"
    )
    with TaskNoteSourceChangeService(
        vault_root=vault,
        migrations=migrations,
        sources=sources,
        principal=principal,
        stores=stores,
    ) as service:
        original_project = service.projector.project

        def interrupted(_note_uuid: str):
            raise RuntimeError("simulated_projection_crash")

        monkeypatch.setattr(service.projector, "project", interrupted)
        with pytest.raises(RuntimeError, match="simulated_projection_crash"):
            service.replace(
                "source-note-recovery",
                "# Task\n\nRecovered body.\n",
                idempotency_key="replace-recovery-1",
            )
        operation = migrations.change_operation_for_key(
            "task-note-replace:replace-recovery-1"
        )
        assert operation is not None
        assert operation.state is ChangeOperationState.RECOVERABLE
        assert operation.change_id is not None
        dependency = migrations.get_source_dependency(operation.source_usage_id)  # type: ignore[arg-type]
        assert dependency is not None
        assert dependency.state is SourceDependencyState.ACKNOWLEDGED

        monkeypatch.setattr(service.projector, "project", original_project)
        recovered = service.recover(operation.operation_id)

    assert recovered.operation.state is ChangeOperationState.COMPLETED
    assert "Recovered body." in path.read_text(encoding="utf-8")
    target = migrations.resolve_source_redaction_target(
        recovered.operation.operation_id,
        current_document_head_sha256=recovered.change.result_structured_head_sha256,
        has_direct_changes=False,
    )
    assert target is not None and target["disposition"] == "scrub"
    mixed = migrations.resolve_source_redaction_target(
        recovered.operation.operation_id,
        current_document_head_sha256="f" * 64,
        has_direct_changes=True,
    )
    assert mixed is not None and mixed["disposition"] == "review"


def test_source_redaction_routes_task_note_head_mismatch_to_durable_review(
    tmp_path: Path,
) -> None:
    _vault, _path, migrations, sources, principal, stores, shadow = _cut_over(
        tmp_path, "source-note-redaction"
    )
    consumer_id = hashlib.sha256(
        b"task-note-shadow:source-note-redaction"
    ).hexdigest()[:32]
    dependency = migrations.get_source_dependency_by_consumer(consumer_id)
    assert dependency is not None
    migrations.update_source_dependency(
        dependency.usage_id,
        result_document_head_sha256="f" * 64,
    )
    source_ref = SourceRef.parse(shadow.source_ref)
    sources.grant_access(
        source_ref=source_ref,
        principal=principal,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint="c" * 64,
    )
    redaction = redact_source(
        sources,
        source_ref=source_ref,
        actor=principal,
        authorization_fingerprint="c" * 64,
        reason_code="user_requested",
    )
    assert len(redaction.pending_effect_ids) == 1

    summary = CoworkDocumentSourceDispatcher(
        sources,
        JournalCaptureStore(tmp_path / "journal.db"),
        service_principal=principal,
        registry=stores.registry,
        task_notes=migrations,
        vault_root=_vault,
    ).drain()
    assert summary.deferred == 1
    updated = migrations.get_source_dependency(dependency.usage_id)
    assert updated is not None
    assert updated.state is SourceDependencyState.REVIEW_REQUIRED
    assert updated.review_reason == "document_head_changed_after_source_copy"
    effect = SourceOutbox(sources).get(redaction.pending_effect_ids[0])
    assert effect is not None
    assert effect.status == "retryable"
    assert effect.error_code == "cowork_document_redaction_review_required"


def test_exact_task_note_redaction_scrubs_history_before_releasing_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, _path, migrations, sources, principal, stores, shadow = _cut_over(
        tmp_path, "source-note-exact-redaction"
    )
    consumer_id = hashlib.sha256(
        b"task-note-shadow:source-note-exact-redaction"
    ).hexdigest()[:32]
    dependency = migrations.get_source_dependency_by_consumer(consumer_id)
    assert dependency is not None
    source_ref = SourceRef.parse(shadow.source_ref)
    sources.grant_access(
        source_ref=source_ref,
        principal=principal,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint="d" * 64,
    )
    redaction = redact_source(
        sources,
        source_ref=source_ref,
        actor=principal,
        authorization_fingerprint="d" * 64,
        reason_code="user_requested",
    )
    assert len(redaction.pending_effect_ids) == 1

    original_release = sources.release_usage

    def _release_only_after_history_cleanup(usage_id: str):
        truth_store = stores.registry.open_store(dependency.store_id)
        with truth_store._read_connection() as conn:
            status = conn.execute(
                "SELECT s.status FROM document_content_redaction_status_events AS s "
                "JOIN document_content_redactions AS r ON r.id = s.redaction_id "
                "JOIN ledger_records AS l "
                "ON l.record_type = 'document_content_redaction_status' "
                "AND l.record_key = s.id WHERE r.source_usage_id = ? "
                "ORDER BY l.seq DESC LIMIT 1",
                (usage_id,),
            ).fetchone()
        assert status is not None and status["status"] == "cleanup_complete"
        return original_release(usage_id)

    monkeypatch.setattr(sources, "release_usage", _release_only_after_history_cleanup)
    summary = CoworkDocumentSourceDispatcher(
        sources,
        JournalCaptureStore(tmp_path / "journal.db"),
        service_principal=principal,
        registry=stores.registry,
        task_notes=migrations,
        vault_root=_vault,
    ).drain()

    assert summary.completed == 1
    rendered = _path.read_text(encoding="utf-8")
    assert "Legacy body." not in rendered
    assert "wb:cowork-task-note-redacted/v1" in rendered
    authority = migrations.get_authority(
        "tasks", "task_note", dependency.note_uuid
    )
    assert authority is not None and authority.state.value == "retired"
    updated = migrations.get_source_dependency(dependency.usage_id)
    assert updated is not None and updated.state is SourceDependencyState.RELEASED
    effect = SourceOutbox(sources).get(redaction.pending_effect_ids[0])
    assert effect is not None and effect.status == "succeeded"
    truth_store = stores.registry.open_store(dependency.store_id)
    with truth_store._read_connection() as conn:
        receipt = conn.execute(
            "SELECT r.*, s.status FROM document_content_redactions AS r "
            "JOIN document_content_redaction_status_events AS s "
            "ON s.redaction_id = r.id JOIN ledger_records AS l "
            "ON l.record_type = 'document_content_redaction_status' "
            "AND l.record_key = s.id WHERE r.source_usage_id = ? "
            "ORDER BY l.seq DESC LIMIT 1",
            (dependency.usage_id,),
        ).fetchone()
    assert receipt is not None
    assert receipt["document_id"] == dependency.document_id
    assert receipt["status"] == "cleanup_complete"


def test_task_note_redaction_keeps_usage_when_conversation_derivative_needs_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, _path, migrations, sources, principal, stores, shadow = _cut_over(
        tmp_path, "source-note-conversation-derivative"
    )
    consumer_id = hashlib.sha256(
        b"task-note-shadow:source-note-conversation-derivative"
    ).hexdigest()[:32]
    dependency = migrations.get_source_dependency_by_consumer(consumer_id)
    assert dependency is not None

    monkeypatch.setattr(
        conversation_store,
        "_DB_PATH",
        tmp_path / "conversations.db",
    )
    with conversation_store.get_connection() as conn:
        conversation_store._ensure_schema(conn)
    monkeypatch.setattr(
        conversation_source_dependencies,
        "_DB_PATH",
        tmp_path / "conversation-source-dependencies.db",
    )
    binding = ensure_document_conversation(
        document_id=shadow.document_id,
        store_id=shadow.store_id,
    )
    message = conversation_store.post_user_message(
        binding.conversation_id,
        "Preserve the argument from this source, but restate it more clearly.",
        message_id="source-note-semantic-message",
    )
    assert message is not None

    source_ref = SourceRef.parse(shadow.source_ref)
    sources.grant_access(
        source_ref=source_ref,
        principal=principal,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint="e" * 64,
    )
    redaction = redact_source(
        sources,
        source_ref=source_ref,
        actor=principal,
        authorization_fingerprint="e" * 64,
        reason_code="user_requested",
    )
    assert len(redaction.pending_effect_ids) == 1

    def _unexpected_release(_usage_id: str):
        raise AssertionError("source usage released before conversation review")

    monkeypatch.setattr(sources, "release_usage", _unexpected_release)
    summary = CoworkDocumentSourceDispatcher(
        sources,
        JournalCaptureStore(tmp_path / "journal.db"),
        service_principal=principal,
        registry=stores.registry,
        task_notes=migrations,
        vault_root=_vault,
    ).drain()

    assert summary.deferred == 1
    updated = migrations.get_source_dependency(dependency.usage_id)
    assert updated is not None
    assert updated.state is SourceDependencyState.REVIEW_REQUIRED
    assert updated.review_reason == "conversation_semantic_derivatives_require_review"
    effect = SourceOutbox(sources).get(redaction.pending_effect_ids[0])
    assert effect is not None
    assert effect.status == "retryable"
    assert effect.error_code == "cowork_document_redaction_review_required"
    conversation_dependency = (
        conversation_source_dependencies.conversation_dependencies_for_document(
            shadow.store_id,
            shadow.document_id,
        )
    )
    assert len(conversation_dependency) == 1
    assert conversation_dependency[0].message_id == message.message_id
    assert conversation_dependency[0].state == "review_required"
    persisted = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assert persisted is not None
    assert persisted["messages"][0]["content"] == message.content
