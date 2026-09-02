"""Sources-outbox consumer for Co-work document managed-copy redaction."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.journal_projection import inspect_managed_section
from work_buddy.document_kernel.redaction import BoundDocumentRedactionService
from work_buddy.journal_capture.content_adapter import (
    JournalContentAdapter,
    redacted_marker_for,
)
from work_buddy.journal_capture.authority import existing_authority_mode
from work_buddy.journal_capture.models import (
    JournalMigrationRecord,
    JournalMigrationState,
    JournalProjectionDiverged,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.paths import resolve
from work_buddy.sources import ActorRef, SourceOutbox, SourceRef, SourceStore
from work_buddy.sources.models import OutboxEffect
from work_buddy.task_notes.models import SourceDependencyState
from work_buddy.task_notes.migration import (
    BoundTaskNoteReader,
    TaskNoteProjectionDiverged,
    TaskNoteProjectionWorker,
)
from work_buddy.task_notes.store import TaskNoteMigrationStore
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.document_redaction import (
    EXACT_COPY_CONTENT_CLASS,
    SCRUB_REDACTION_POLICY,
    scrub_exact_managed_document_content,
)
from work_buddy.truth.registry import TruthStoreRegistry


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentRedactionDispatchSummary:
    completed: int = 0
    deferred: int = 0
    failed: int = 0


class CoworkDocumentSourceDispatcher:
    def __init__(
        self,
        sources: SourceStore,
        journal: JournalCaptureStore,
        *,
        service_principal: ActorRef,
        registry: TruthStoreRegistry | None = None,
        task_notes: TaskNoteMigrationStore | None = None,
        vault_root=None,
        worker_id: str = "cowork-document-source-dispatch",
    ) -> None:
        self.sources = sources
        self.journal = journal
        self.service_principal = service_principal
        self.registry = registry if registry is not None else TruthStoreRegistry()
        self.task_notes = task_notes
        self.journal_adapter = JournalContentAdapter(vault_root)
        self.worker_id = worker_id
        self.outbox = SourceOutbox(sources)

    def drain(self, *, limit: int = 25) -> DocumentRedactionDispatchSummary:
        effects = self.outbox.lease(
            self.worker_id,
            limit=limit,
            lease_seconds=60,
            target_domain="cowork_document",
            effect_type="source.redaction",
        )
        completed = deferred = failed = 0
        for effect in effects:
            try:
                result_ref = self._deliver(effect)
                self.outbox.complete(
                    effect.effect_id,
                    self.worker_id,
                    result_ref=result_ref,
                    result_sha256=hashlib.sha256(result_ref.encode()).hexdigest(),
                )
                completed += 1
            except (
                _RedactionTargetPending,
                _RedactionReviewRequired,
                _RedactionHistoryIncomplete,
            ) as exc:
                self.outbox.fail(
                    effect.effect_id,
                    self.worker_id,
                    error_code=exc.code,
                    retryable=True,
                )
                deferred += 1
            except (ValueError, KeyError) as exc:
                self.outbox.fail(
                    effect.effect_id,
                    self.worker_id,
                    error_code=getattr(exc, "code", "cowork_document_redaction_invalid"),
                    retryable=False,
                )
                failed += 1
            except Exception as exc:
                logger.warning(
                    "Deferred Co-work document source redaction (%s)",
                    getattr(exc, "code", type(exc).__name__),
                )
                self.outbox.fail(
                    effect.effect_id,
                    self.worker_id,
                    error_code=getattr(exc, "code", "cowork_document_redaction_failed"),
                    retryable=True,
                )
                deferred += 1
        return DocumentRedactionDispatchSummary(completed, deferred, failed)

    def _deliver(self, effect: OutboxEffect) -> str:
        payload = _mapping(effect.payload)
        if (
            effect.target_domain != "cowork_document"
            or effect.effect_type != "source.redaction"
            or payload.get("schema") != "wb.source-redaction-effect/v1"
            or payload.get("consumer_domain") != "cowork_document"
            or payload.get("redaction_policy") not in {"scrub", "review"}
        ):
            raise ValueError("cowork_document_redaction_invalid")
        source_ref = SourceRef.from_dict(_mapping(payload.get("source_ref")))
        consumer_id = _text(payload, "consumer_id")
        usage_id = _text(payload, "usage_id")
        event_id = _text(payload, "redaction_event_id")
        mirror = self.journal.get_document_binding_by_source_consumer(consumer_id)
        if mirror is None:
            migration = self._journal_migration_target(
                consumer_id=consumer_id,
                source_ref=source_ref,
            )
            if migration is not None:
                return self._deliver_journal_migration(
                    effect,
                    migration=migration,
                    source_ref=source_ref,
                    usage_id=usage_id,
                    event_id=event_id,
                )
            return self._deliver_task_note(
                effect,
                source_ref=source_ref,
                consumer_id=consumer_id,
                usage_id=usage_id,
                event_id=event_id,
            )
        store = self.registry.open_store(mirror.store_id)
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.get_binding(mirror.binding_id)
        if binding is None:
            raise KeyError("binding_not_found")
        direct_changes = tuple(
            item
            for item in causality.changes_for_binding(binding.binding_id, limit=1000)
            if item.operation_kind == "direct_editor_update"
        )
        redaction_policy = _text(payload, "redaction_policy")
        if direct_changes or redaction_policy == "review":
            first_direct = min(
                direct_changes,
                key=lambda item: (item.committed_at, item.change_id),
                default=None,
            )
            self.journal.mark_document_source_review_required(
                mirror.entry_id,
                details={
                    "schema": "wb.source-maintenance-attention/v1",
                    "kind": "source_redaction_review_required",
                    "reason": (
                        "document_contains_direct_edits"
                        if direct_changes
                        else "semantic_derivative_requires_review"
                    ),
                    "bindingId": binding.binding_id,
                    "documentId": binding.document_id,
                    "sourceRef": source_ref.uri,
                    "redactionEventId": event_id,
                    "sourceEffectId": effect.effect_id,
                    "effectUsageId": usage_id,
                    "activeUsageId": mirror.source_usage_id,
                    "activeRedactionPolicy": mirror.source_redaction_policy,
                    "firstDirectChangeId": (
                        None if first_direct is None else first_direct.change_id
                    ),
                },
            )
            raise _RedactionReviewRequired()
        if mirror.source_usage_id != usage_id:
            raise ValueError("cowork_document_redaction_invalid")
        journal_authority = existing_authority_mode(self.journal.path)
        if journal_authority in {"cutover_paused", "recovery_fenced"}:
            # Do not partly advance a document redaction while Journal authority
            # is changing or recovery-fenced.  The durable Sources effect will
            # retry after the authority transition resolves.
            raise _RedactionTargetPending()
        actor = json.dumps(
            self.service_principal.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        document_redaction = BoundDocumentRedactionService().scrub(
            store,
            binding=binding,
            source_ref=source_ref.uri,
            redaction_event_id=event_id,
            actors={"applied_by": actor},
        )
        entry = self.journal.get_entry(mirror.entry_id)
        if entry is None:
            self.journal.mark_document_source_review_required(
                mirror.entry_id,
                details={
                    "schema": "wb.source-maintenance-attention/v1",
                    "kind": "source_redaction_review_required",
                    "reason": "compatibility_projection_unverifiable",
                    "bindingId": binding.binding_id,
                    "sourceRef": source_ref.uri,
                    "redactionEventId": event_id,
                },
            )
            raise _RedactionReviewRequired()
        if journal_authority == "legacy_compatibility":
            cursor = causality.projection_cursor(binding.binding_id)
            if cursor is None or cursor.section_sha256 is None:
                self.journal.mark_document_source_review_required(
                    mirror.entry_id,
                    details={
                        "schema": "wb.source-maintenance-attention/v1",
                        "kind": "source_redaction_review_required",
                        "reason": "compatibility_projection_unverifiable",
                        "bindingId": binding.binding_id,
                        "sourceRef": source_ref.uri,
                        "redactionEventId": event_id,
                    },
                )
                raise _RedactionReviewRequired()
            journal_path = self.journal_adapter.journal_path(entry.day_id)
            sibling_tombstone = redacted_marker_for(entry.entry_id, event_id)
            try:
                sibling_already_scrubbed = sibling_tombstone in journal_path.read_text(
                    encoding="utf-8-sig"
                )
            except OSError:
                sibling_already_scrubbed = False
            if not sibling_already_scrubbed:
                observed = inspect_managed_section(journal_path, entry.entry_id)
                if observed.body_sha256 != cursor.section_sha256:
                    self.journal.mark_document_source_review_required(
                        mirror.entry_id,
                        details={
                            "schema": "wb.source-maintenance-attention/v1",
                            "kind": "source_redaction_review_required",
                            "reason": "compatibility_projection_diverged",
                            "bindingId": binding.binding_id,
                            "sourceRef": source_ref.uri,
                            "redactionEventId": event_id,
                        },
                    )
                    raise _RedactionReviewRequired()
            try:
                self.journal_adapter.redact(entry, redaction_event_id=event_id)
            except JournalProjectionDiverged:
                self.journal.mark_document_source_review_required(
                    mirror.entry_id,
                    details={
                        "schema": "wb.source-maintenance-attention/v1",
                        "kind": "source_redaction_review_required",
                        "reason": "compatibility_projection_diverged",
                        "bindingId": binding.binding_id,
                        "sourceRef": source_ref.uri,
                        "redactionEventId": event_id,
                    },
                )
                raise _RedactionReviewRequired()
        elif journal_authority != "database_only":
            raise ValueError("cowork_document_redaction_invalid")
        coverage = scrub_exact_managed_document_content(
            store,
            document_id=binding.document_id,
            replacement_document_version_id=(
                document_redaction.replacement_document_version_id
            ),
            source_usage_id=usage_id,
            source_ref=source_ref.to_dict(),
            source_redaction_event_id=event_id,
            actor_ref=self.service_principal.to_dict(),
            content_class=EXACT_COPY_CONTENT_CLASS,
            redaction_policy=SCRUB_REDACTION_POLICY,
        )
        if not coverage.complete:
            if coverage.review_target_refs:
                self.journal.mark_document_source_review_required(
                    mirror.entry_id,
                    details={
                        "schema": "wb.source-maintenance-attention/v1",
                        "kind": "source_redaction_review_required",
                        "reason": "document_semantic_derivatives_require_review",
                        "bindingId": binding.binding_id,
                        "documentId": binding.document_id,
                        "sourceRef": source_ref.uri,
                        "redactionEventId": event_id,
                        "sourceEffectId": effect.effect_id,
                        "effectUsageId": usage_id,
                        "reviewTargetRefs": list(coverage.review_target_refs),
                    },
                )
                raise _RedactionReviewRequired()
            raise _RedactionHistoryIncomplete()
        from work_buddy.cowork.conversation_source_dependencies import (
            redact_document_conversation_dependencies,
        )

        conversation_cleanup = redact_document_conversation_dependencies(
            store_id=store.store_id,
            document_id=binding.document_id,
        )
        if not conversation_cleanup["complete"]:
            self.journal.mark_document_source_review_required(
                mirror.entry_id,
                details={
                    "schema": "wb.source-maintenance-attention/v1",
                    "kind": "source_redaction_review_required",
                    "reason": "conversation_semantic_derivatives_require_review",
                    "bindingId": binding.binding_id,
                    "documentId": binding.document_id,
                    "sourceRef": source_ref.uri,
                    "redactionEventId": event_id,
                    "sourceEffectId": effect.effect_id,
                    "effectUsageId": usage_id,
                    "reviewMessageIds": list(
                        conversation_cleanup["review_required_message_ids"]
                    ),
                    "scrubbedMessageIds": list(
                        conversation_cleanup["scrubbed_message_ids"]
                    ),
                },
            )
            raise _RedactionReviewRequired()
        self.journal.retire_document_binding(mirror.entry_id)
        self.sources.release_usage(usage_id)
        return f"cowork-document-source-redaction:{event_id}"

    def _journal_migration_target(
        self,
        *,
        consumer_id: str,
        source_ref: SourceRef,
    ) -> tuple[JournalMigrationRecord, Any, Any, Any] | None:
        records = {
            item.binding_id: item
            for item in self.journal.list_migrations()
            if item.binding_id is not None and item.store_id is not None
        }
        matches: list[tuple[JournalMigrationRecord, Any, Any, Any]] = []
        for store_id in sorted({item.store_id for item in records.values()}):
            if store_id is None:
                continue
            store = self.registry.open_store(store_id)
            causality = DocumentCausalityStore(store.paths.sidecar)
            change = causality.exact_source_change_for_consumer(consumer_id)
            if change is None or change.source_ref != source_ref.uri:
                continue
            binding = (
                None
                if change.binding_id is None
                else causality.get_binding(change.binding_id)
            )
            record = None if binding is None else records.get(binding.binding_id)
            if (
                record is not None
                and binding is not None
                and binding.domain_namespace == "journal"
                and binding.domain_kind == record.entity_kind
                and binding.domain_entity_id == record.marker_id
            ):
                matches.append((record, store, binding, change))
        if len(matches) > 1:
            raise ValueError("cowork_document_redaction_invalid")
        return None if not matches else matches[0]

    def _deliver_journal_migration(
        self,
        effect: OutboxEffect,
        *,
        migration: tuple[JournalMigrationRecord, Any, Any, Any],
        source_ref: SourceRef,
        usage_id: str,
        event_id: str,
    ) -> str:
        record, store, binding, change = migration
        causality = DocumentCausalityStore(store.paths.sidecar)
        document = documents.get_document(store, binding.document_id)
        if document.ydoc_snapshot_sha256 is None:
            raise _RedactionTargetPending()
        current_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        later_direct = any(
            item.operation_kind == "direct_editor_update"
            and item.committed_at >= change.committed_at
            for item in causality.changes_for_binding(binding.binding_id, limit=1000)
        )
        if later_direct or current_head != change.result_structured_head_sha256:
            self.journal.mirror_migration_authority(
                entity_kind=record.entity_kind,
                entity_id=record.entity_id,
                state=JournalMigrationState.PAUSED_DIVERGED,
                authority_epoch=binding.content_authority_epoch,
                rollback_deadline=record.rollback_deadline,
                projection_state="paused_diverged",
                error_code="source_redaction_review_required",
            )
            raise _RedactionReviewRequired()
        cursor = causality.projection_cursor(binding.binding_id)
        if binding.content_authority == "co_work":
            if cursor is None or cursor.section_sha256 is None:
                raise _RedactionReviewRequired()
            try:
                self.journal_adapter.redact_managed_selection(
                    day_id=record.day_id,
                    marker_id=record.marker_id,
                    redaction_event_id=event_id,
                    expected_body_sha256=cursor.section_sha256,
                )
            except JournalProjectionDiverged:
                self.journal.mirror_migration_authority(
                    entity_kind=record.entity_kind,
                    entity_id=record.entity_id,
                    state=JournalMigrationState.PAUSED_DIVERGED,
                    authority_epoch=binding.content_authority_epoch,
                    rollback_deadline=record.rollback_deadline,
                    projection_state="paused_diverged",
                    error_code="source_redaction_review_required",
                )
                raise _RedactionReviewRequired()
        actor = json.dumps(
            self.service_principal.to_dict(), sort_keys=True, separators=(",", ":")
        )
        document_redaction = BoundDocumentRedactionService().scrub(
            store,
            binding=binding,
            source_ref=source_ref.uri,
            redaction_event_id=event_id,
            actors={"applied_by": actor},
        )
        coverage = scrub_exact_managed_document_content(
            store,
            document_id=binding.document_id,
            replacement_document_version_id=document_redaction.replacement_document_version_id,
            source_usage_id=usage_id,
            source_ref=source_ref.to_dict(),
            source_redaction_event_id=event_id,
            actor_ref=self.service_principal.to_dict(),
            content_class=EXACT_COPY_CONTENT_CLASS,
            redaction_policy=SCRUB_REDACTION_POLICY,
        )
        if not coverage.complete:
            raise _RedactionHistoryIncomplete()
        from work_buddy.cowork.conversation_source_dependencies import (
            redact_document_conversation_dependencies,
        )

        conversation_cleanup = redact_document_conversation_dependencies(
            store_id=store.store_id,
            document_id=binding.document_id,
        )
        if not conversation_cleanup["complete"]:
            self.journal.mirror_migration_authority(
                entity_kind=record.entity_kind,
                entity_id=record.entity_id,
                state=JournalMigrationState.PAUSED_DIVERGED,
                authority_epoch=binding.content_authority_epoch,
                rollback_deadline=record.rollback_deadline,
                projection_state="paused_diverged",
                error_code="conversation_redaction_review_required",
            )
            raise _RedactionReviewRequired()
        self.journal.mirror_migration_authority(
            entity_kind=record.entity_kind,
            entity_id=record.entity_id,
            state=JournalMigrationState.RETIRED,
            authority_epoch=binding.content_authority_epoch,
            rollback_deadline=None,
            projection_state="none",
            error_code="source_redacted",
        )
        self.sources.release_usage(usage_id)
        return f"journal-migration-source-redaction:{event_id}"

    def _deliver_task_note(
        self,
        effect: OutboxEffect,
        *,
        source_ref: SourceRef,
        consumer_id: str,
        usage_id: str,
        event_id: str,
    ) -> str:
        task_notes = self.task_notes or TaskNoteMigrationStore(
            resolve("db/task-note-migration")
        )
        dependency = task_notes.get_source_dependency_by_consumer(consumer_id)
        if dependency is None:
            # The reservation precedes the readable domain commit. Until a
            # Journal or task-note reverse mirror appears, fail closed.
            raise _RedactionTargetPending()
        if dependency.usage_id != usage_id or dependency.source_ref != source_ref.uri:
            raise ValueError("cowork_document_redaction_invalid")
        store = self.registry.open_store(dependency.store_id)
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.get_binding(dependency.binding_id)
        if binding is None or binding.document_id != dependency.document_id:
            raise KeyError("binding_not_found")
        document = documents.get_document(store, dependency.document_id)
        if document.ydoc_snapshot_sha256 is None:
            raise _RedactionTargetPending()
        current_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        direct_changes = tuple(
            item
            for item in causality.changes_for_binding(binding.binding_id, limit=1000)
            if item.operation_kind == "direct_editor_update"
        )
        target = task_notes.resolve_source_redaction_target(
            consumer_id,
            current_document_head_sha256=current_head,
            has_direct_changes=bool(direct_changes),
        )
        if target is None:
            raise _RedactionTargetPending()
        disposition = target.get("disposition")
        if disposition == "review":
            reason = target.get("reason")
            if not isinstance(reason, str) or not reason:
                raise ValueError("cowork_document_redaction_invalid")
            task_notes.update_source_dependency(
                usage_id,
                state=SourceDependencyState.REVIEW_REQUIRED,
                review_reason=reason,
            )
            raise _RedactionReviewRequired()
        if disposition == "released":
            self.sources.release_usage(usage_id)
            return f"task-note-source-redaction:{event_id}:superseded"
        if disposition == "pending":
            raise _RedactionTargetPending()
        if disposition != "scrub":
            raise ValueError("cowork_document_redaction_invalid")
        actor = json.dumps(
            self.service_principal.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        document_redaction = BoundDocumentRedactionService().scrub(
            store,
            binding=binding,
            source_ref=source_ref.uri,
            redaction_event_id=event_id,
            actors={"applied_by": actor},
        )
        from work_buddy.document_kernel.runtime_service import shared_document_kernel

        task_reader = BoundTaskNoteReader(
            vault_root=self.journal_adapter.vault_root,
            migration_store=task_notes,
            bound_store=store,
        )
        task_projection = TaskNoteProjectionWorker(
            vault_root=self.journal_adapter.vault_root,
            migrations=task_notes,
            source_store=self.sources,
            principal=self.service_principal,
            reader=task_reader,
            kernel=shared_document_kernel(),
        )
        try:
            task_projection.redact_compatibility_copy(
                dependency.note_uuid,
                redaction_event_id=event_id,
            )
        except TaskNoteProjectionDiverged:
            task_notes.update_source_dependency(
                usage_id,
                state=SourceDependencyState.REVIEW_REQUIRED,
                review_reason="compatibility_projection_diverged",
            )
            raise _RedactionReviewRequired()
        coverage = scrub_exact_managed_document_content(
            store,
            document_id=binding.document_id,
            replacement_document_version_id=(
                document_redaction.replacement_document_version_id
            ),
            source_usage_id=usage_id,
            source_ref=source_ref.to_dict(),
            source_redaction_event_id=event_id,
            actor_ref=self.service_principal.to_dict(),
            content_class=EXACT_COPY_CONTENT_CLASS,
            redaction_policy=SCRUB_REDACTION_POLICY,
        )
        if not coverage.complete:
            if coverage.review_target_refs:
                task_notes.update_source_dependency(
                    usage_id,
                    state=SourceDependencyState.REVIEW_REQUIRED,
                    review_reason="document_semantic_derivatives_require_review",
                )
                raise _RedactionReviewRequired()
            raise _RedactionHistoryIncomplete()
        from work_buddy.cowork.conversation_source_dependencies import (
            redact_document_conversation_dependencies,
        )

        conversation_cleanup = redact_document_conversation_dependencies(
            store_id=store.store_id,
            document_id=binding.document_id,
        )
        if not conversation_cleanup["complete"]:
            task_notes.update_source_dependency(
                usage_id,
                state=SourceDependencyState.REVIEW_REQUIRED,
                review_reason="conversation_semantic_derivatives_require_review",
            )
            raise _RedactionReviewRequired()
        task_notes.update_source_dependency(
            usage_id,
            state=SourceDependencyState.RELEASED,
        )
        task_notes.mirror_retired_authority(
            "tasks",
            "task_note",
            dependency.note_uuid,
            epoch=binding.content_authority_epoch,
        )
        self.sources.release_usage(usage_id)
        return f"task-note-source-redaction:{event_id}"


class _RedactionTargetPending(RuntimeError):
    code = "cowork_document_redaction_target_pending"


class _RedactionReviewRequired(RuntimeError):
    code = "cowork_document_redaction_review_required"


class _RedactionHistoryIncomplete(RuntimeError):
    code = "cowork_document_redaction_history_incomplete"


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("cowork_document_redaction_invalid")
    return value


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > 512:
        raise ValueError("cowork_document_redaction_invalid")
    return item


__all__ = [
    "CoworkDocumentSourceDispatcher",
    "DocumentRedactionDispatchSummary",
]
