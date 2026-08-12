"""Thin integration of domain-bound documents with ordinary Co-work edits."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlencode

from work_buddy.document_kernel.causality import (
    DocumentCausalityStore,
    DocumentChangeRecord,
    DomainDocumentBinding,
    ProjectionCursor,
)
from work_buddy.document_kernel.direct_edit import DirectDocumentEditService
from work_buddy.document_kernel.journal_projection import (
    FileDivergenceCapture,
    JournalProjectionAdapter,
    JournalProjectionWorker,
)
from work_buddy.document_kernel.runtime_service import shared_document_kernel
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.models import JournalDocumentBinding
from work_buddy.journal_capture.models import JournalMigrationState
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import (
    ActorRef,
    SourceRedacted,
    SourceRef,
    SourceStore,
    SourceUsageConflict,
)
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.store import DocumentRecord, TruthStore
from work_buddy.truth.registry import TruthStoreRegistry


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from work_buddy.task_notes.models import ProjectionOutcome
    from work_buddy.task_notes.store import TaskNoteMigrationStore


@dataclass(frozen=True, slots=True)
class BoundDirectPush:
    binding: DomainDocumentBinding
    change: DocumentChangeRecord
    next_offset: str
    projection: ProjectionCursor | ProjectionOutcome | None


def current_domain_binding(
    store: TruthStore, document_id: str
) -> DomainDocumentBinding | None:
    return DocumentCausalityStore(store.paths.sidecar).binding_for_document(
        store.store_id, document_id
    )


def apply_bound_direct_push(
    store: TruthStore,
    document: DocumentRecord,
    *,
    update: bytes,
    expected_head: str,
    expected_generation: str,
    actors: Mapping[str, str | None],
    input_assurance: str,
    source_store: SourceStore,
    source_principal: ActorRef,
    journal_store: JournalCaptureStore | None = None,
    task_migration_store: TaskNoteMigrationStore | None = None,
    vault_root=None,
    lock_guard=None,
) -> BoundDirectPush | None:
    """Apply a non-compacted browser update when the doc has domain authority."""

    binding = current_domain_binding(store, document.id)
    if binding is None or binding.content_authority != "co_work":
        return None
    update_sha = hashlib.sha256(update).hexdigest()
    idempotency = hashlib.sha256(
        (
            "cowork-bound-direct-edit/v1\0"
            + store.store_id
            + "\0"
            + document.id
            + "\0"
            + expected_head
            + "\0"
            + expected_generation
            + "\0"
            + update_sha
        ).encode("utf-8")
    ).hexdigest()
    result = DirectDocumentEditService(kernel=shared_document_kernel()).apply(
        store,
        document_id=document.id,
        update=update,
        expected_base_structured_head_sha256=expected_head,
        expected_base_generation_sha256=expected_generation,
        actors=actors,
        idempotency_key=idempotency,
        binding=binding,
        input_assurance=input_assurance,
        lock_guard=lock_guard,
    )
    reconcile_document_source_dependency(
        store,
        binding=binding,
        source_store=source_store,
        source_principal=source_principal,
        journal_store=journal_store,
    )
    _updates, next_offset = ydoc_store.read_updates(
        store, document_id=document.id
    )
    projection = project_bound_document(
        store,
        binding=binding,
        change=result.change,
        source_store=source_store,
        source_principal=source_principal,
        journal_store=journal_store,
        task_migration_store=task_migration_store,
        vault_root=vault_root,
    )
    return BoundDirectPush(binding, result.change, next_offset, projection)


def reconcile_document_source_dependency(
    store: TruthStore,
    *,
    binding: DomainDocumentBinding,
    source_store: SourceStore,
    source_principal: ActorRef,
    journal_store: JournalCaptureStore | None = None,
) -> JournalDocumentBinding | None:
    """Replace an edited exact-copy usage with a reviewable derivative usage.

    The document change is already durable before this seam runs. Every later
    step is idempotent and the Journal transition receipt lets the recovery
    sweep resume after a process stop at any cross-database boundary.
    """

    if (
        binding.domain_namespace != "journal"
        or binding.domain_kind != "running_note"
        or binding.role != "running_note"
    ):
        return None
    journal = journal_store or JournalCaptureStore()
    mirror = journal.get_document_binding(binding.domain_entity_id)
    if mirror is None or mirror.state == "retired":
        return mirror
    causality = DocumentCausalityStore(store.paths.sidecar)
    records = causality.changes_for_binding(binding.binding_id, limit=1000)
    direct = sorted(
        (item for item in records if item.operation_kind == "direct_editor_update"),
        key=lambda item: (item.committed_at, item.change_id),
    )
    if not direct:
        return mirror
    first_direct = direct[0]
    exact = next(
        (
            item
            for item in reversed(records)
            if item.operation_kind == "exact_source_copy"
            and item.source_ref is not None
            and item.source_representation_id is not None
        ),
        None,
    )
    if exact is None:
        return journal.mark_document_source_review_required(
            mirror.entry_id,
            details={
                "schema": "wb.source-maintenance-attention/v1",
                "kind": "source_dependency_unverifiable",
                "reason": "exact_source_change_missing",
                "bindingId": binding.binding_id,
                "directChangeId": first_direct.change_id,
            },
        )

    transition = journal.get_document_source_usage_transition(mirror.entry_id)
    if transition is None:
        try:
            reserved = source_store.reserve_usage(
                source_ref=SourceRef.parse(exact.source_ref),
                representation_id=exact.source_representation_id,
                principal=source_principal,
                purpose="journal.materialize",
                consumer_domain="cowork_document",
                consumer_id=mirror.source_consumer_id,
                use_kind="mixed_derivative",
                disclosure_kind="semantic_derivative",
                redaction_policy="review",
                selector={
                    "kind": "whole",
                    "transition_change_id": first_direct.change_id,
                },
            )
            if reserved.status == "reserved":
                source_store.precommit_recheck_usage(reserved.usage_id)
                source_store.acknowledge_usage(reserved.usage_id)
            elif reserved.status != "acknowledged":
                raise SourceUsageConflict()
        except (SourceRedacted, SourceUsageConflict):
            item = source_store.get_item(SourceRef.parse(exact.source_ref))
            if item is not None and item.lifecycle_state != "redacted":
                raise
            return journal.mark_document_source_review_required(
                mirror.entry_id,
                details={
                    "schema": "wb.source-maintenance-attention/v1",
                    "kind": "source_redaction_review_required",
                    "reason": "source_redacted_before_dependency_transition",
                    "bindingId": binding.binding_id,
                    "directChangeId": first_direct.change_id,
                    "sourceRef": exact.source_ref,
                    "redactionEventId": (
                        None if item is None else item.redaction_event_id
                    ),
                },
            )
        mirror, transition = journal.transition_document_source_usage(
            entry_id=mirror.entry_id,
            binding_id=binding.binding_id,
            change_id=first_direct.change_id,
            expected_prior_usage_id=mirror.source_usage_id,
            next_usage_id=reserved.usage_id,
            next_use_kind="mixed_derivative",
            next_disclosure_kind="semantic_derivative",
            next_redaction_policy="review",
        )

    if transition.state == "mirror_updated":
        released = source_store.release_usage_if_source_active(
            transition.prior_usage_id
        )
        if released is None:
            item = source_store.get_item(SourceRef.parse(exact.source_ref))
            return journal.mark_document_source_review_required(
                mirror.entry_id,
                details={
                    "schema": "wb.source-maintenance-attention/v1",
                    "kind": "source_redaction_review_required",
                    "reason": "redaction_raced_dependency_transition",
                    "bindingId": binding.binding_id,
                    "directChangeId": first_direct.change_id,
                    "sourceRef": exact.source_ref,
                    "redactionEventId": (
                        None if item is None else item.redaction_event_id
                    ),
                    "priorUsageId": transition.prior_usage_id,
                    "activeUsageId": transition.next_usage_id,
                },
            )
        journal.complete_document_source_usage_transition(transition.transition_id)
    return journal.get_document_binding(mirror.entry_id)


def project_bound_document(
    store: TruthStore,
    *,
    binding: DomainDocumentBinding,
    change: DocumentChangeRecord | None,
    source_store: SourceStore,
    source_principal: ActorRef,
    journal_store: JournalCaptureStore | None = None,
    task_migration_store: TaskNoteMigrationStore | None = None,
    vault_root=None,
) -> ProjectionCursor | ProjectionOutcome | None:
    """Project a supported domain-bound head through its owning adapter.

    Projection failure never rolls back an already-durable editor change.  The
    causality cursor remains the recovery authority and a later reconciliation
    sweep retries it; divergence itself is represented as a paused cursor.
    """

    if (
        binding.domain_namespace == "tasks"
        and binding.domain_kind == "task_note"
        and binding.role == "task_note"
    ):
        from work_buddy.paths import resolve
        from work_buddy.task_notes.migration import (
            BoundTaskNoteReader,
            TaskNoteProjectionWorker,
        )
        from work_buddy.task_notes.store import TaskNoteMigrationStore

        migrations = task_migration_store or TaskNoteMigrationStore(
            resolve("db/task-note-migration")
        )
        record = migrations.get_task_note(binding.domain_entity_id)
        if (
            record is None
            or record.binding_id != binding.binding_id
            or record.store_id != binding.store_id
            or record.document_id != binding.document_id
        ):
            logger.warning(
                "Bound task-note mirror %s is unavailable for projection",
                binding.domain_entity_id,
            )
            return None
        adapter = JournalContentAdapter(vault_root)
        reader = BoundTaskNoteReader(
            vault_root=adapter.vault_root,
            migration_store=migrations,
            bound_store=store,
        )
        worker = TaskNoteProjectionWorker(
            vault_root=adapter.vault_root,
            migrations=migrations,
            source_store=source_store,
            principal=source_principal,
            reader=reader,
            kernel=shared_document_kernel(),
        )
        try:
            return worker.project(binding.domain_entity_id)
        except Exception as exc:  # durable task-note cursor remains retryable
            logger.warning(
                "Deferred task-note projection for bound document %s (%s)",
                binding.document_id,
                getattr(exc, "code", type(exc).__name__),
            )
            return None

    if (
        binding.domain_namespace != "journal"
        or binding.domain_kind not in {"running_note", "logical_day_log"}
        or binding.role != binding.domain_kind
    ):
        return None
    journal = journal_store or JournalCaptureStore()
    migration = next(
        (
            item
            for item in journal.list_migrations()
            if item.binding_id == binding.binding_id
        ),
        None,
    )
    adapter = JournalContentAdapter(vault_root)
    worker = JournalProjectionWorker(
        kernel=shared_document_kernel(),
        adapter=JournalProjectionAdapter(adapter.vault_root),
        divergence_capture=FileDivergenceCapture(
            source_store=source_store,
            vault_root=adapter.vault_root,
            principal=source_principal,
        ),
    )
    try:
        cursor = worker.project(
            store,
            binding=binding,
            entry_id=(
                binding.domain_entity_id if migration is None else migration.marker_id
            ),
        )
    except Exception as exc:  # durable cursor/intent remains retryable
        logger.warning(
            "Deferred Journal projection for bound document %s (%s)",
            binding.document_id,
            getattr(exc, "code", type(exc).__name__),
        )
        return None

    if migration is not None:
        journal.mirror_migration_authority(
            entity_kind=migration.entity_kind,
            entity_id=migration.entity_id,
            state=(
                JournalMigrationState.PAUSED_DIVERGED
                if cursor.status == "paused_diverged"
                else JournalMigrationState.COWORK
            ),
            authority_epoch=binding.content_authority_epoch,
            rollback_deadline=migration.rollback_deadline,
            projection_state=cursor.status,
            divergence_source_ref=cursor.divergence_source_ref,
            error_code=(
                "journal_projection_diverged"
                if cursor.status == "paused_diverged"
                else None
            ),
        )
        return cursor

    # Running Note pilot reverse mirror. Journal migrations use the migration
    # mirror above and logical-day logs never have capture-entry mirrors.
    if binding.domain_kind != "running_note":
        return cursor
    entry = journal.get_entry(binding.domain_entity_id)
    if entry is None:
        logger.warning(
            "Bound Journal entry %s is unavailable for reverse projection",
            binding.domain_entity_id,
        )
        return cursor
    prior = journal.get_document_binding(entry.entry_id)
    effective_change = change
    if effective_change is None and prior is None:
        records = DocumentCausalityStore(store.paths.sidecar)
        # Initial pilot always records a change before cutting authority over.
        candidates = records.changes_for_binding(binding.binding_id, limit=1)
        effective_change = candidates[0] if candidates else None
    change_id = (
        effective_change.change_id
        if effective_change is not None
        else prior.change_id if prior is not None else None
    )
    if change_id is None:
        return cursor
    if prior is None:
        # The pilot response is published only after its Journal reverse mirror
        # commits. A missing mirror here therefore means recovery should replay
        # the pilot route, not guess at its source usage identity.
        return cursor
    href = "/app/cowork?" + urlencode(
        {
            "store_id": binding.store_id,
            "document_id": binding.document_id,
            "change_id": change_id,
        }
    )
    inspection = dict(prior.inspection) if prior is not None else {
        "schema": "cowork-running-note-pilot/v1",
        "binding": {},
    }
    inspection["coworkHref"] = href
    inspection["binding"] = {
        "bindingId": binding.binding_id,
        "domainNamespace": binding.domain_namespace,
        "domainKind": binding.domain_kind,
        "domainEntityId": binding.domain_entity_id,
        "storeId": binding.store_id,
        "documentId": binding.document_id,
        "contentAuthority": binding.content_authority,
        "contentAuthorityEpoch": binding.content_authority_epoch,
    }
    if effective_change is not None:
        inspection["change"] = {
            "changeId": effective_change.change_id,
            "operationKind": effective_change.operation_kind,
            "baseStructuredHeadSha256": effective_change.base_structured_head_sha256,
            "resultStructuredHeadSha256": effective_change.result_structured_head_sha256,
            "assurance": json.loads(effective_change.assurance_json),
            "actors": json.loads(effective_change.actors_json),
            "protocolVersion": effective_change.protocol_version,
            "runtimeVersion": effective_change.runtime_version,
            "schemaVersion": effective_change.schema_version,
        }
    inspection["journalProjection"] = {
        "status": cursor.status,
        "documentHeadSha256": cursor.document_head_sha256,
        "sectionSha256": cursor.section_sha256,
        "divergenceSourceRef": cursor.divergence_source_ref,
    }
    journal.record_document_binding(
        entry_id=entry.entry_id,
        binding_id=binding.binding_id,
        store_id=binding.store_id,
        document_id=binding.document_id,
        change_id=change_id,
        source_consumer_id=prior.source_consumer_id,
        source_usage_id=prior.source_usage_id,
        source_use_kind=prior.source_use_kind,
        source_disclosure_kind=prior.source_disclosure_kind,
        source_redaction_policy=prior.source_redaction_policy,
        cowork_href=href,
        content_authority_epoch=binding.content_authority_epoch,
        entry_version=entry.version,
        inspection=inspection,
        state="paused_diverged" if cursor.status == "paused_diverged" else "current",
    )
    return cursor


def reconcile_journal_documents(
    *,
    journal_store: JournalCaptureStore,
    source_store: SourceStore,
    source_principal: ActorRef,
    vault_root=None,
    registry: TruthStoreRegistry | None = None,
) -> tuple[ProjectionCursor, ...]:
    """Missed-event-safe sweep over every Journal-owned Co-work binding."""

    stores = sorted(
        {item.store_id for item in journal_store.list_document_bindings()}
        | {
            item.store_id
            for item in journal_store.list_migrations()
            if item.store_id is not None
        }
    )
    if not stores:
        return ()
    truth_registry = registry or TruthStoreRegistry()
    results: list[ProjectionCursor] = []
    for store_id in stores:
        try:
            store = truth_registry.open_store(store_id)
            causality = DocumentCausalityStore(store.paths.sidecar)
            for binding in causality.list_bindings(content_authority="co_work"):
                if (
                    binding.domain_namespace != "journal"
                    or binding.domain_kind not in {"running_note", "logical_day_log"}
                ):
                    continue
                if binding.domain_kind == "running_note":
                    reconcile_document_source_dependency(
                        store,
                        binding=binding,
                        source_store=source_store,
                        source_principal=source_principal,
                        journal_store=journal_store,
                    )
                cursor = project_bound_document(
                    store,
                    binding=binding,
                    change=None,
                    source_store=source_store,
                    source_principal=source_principal,
                    journal_store=journal_store,
                    vault_root=vault_root,
                )
                if cursor is not None:
                    results.append(cursor)
        except Exception as exc:
            logger.warning(
                "Deferred bound Journal document reconciliation for store %s (%s)",
                store_id,
                getattr(exc, "code", type(exc).__name__),
            )
    return tuple(results)


__all__ = [
    "BoundDirectPush",
    "apply_bound_direct_push",
    "current_domain_binding",
    "project_bound_document",
    "reconcile_document_source_dependency",
    "reconcile_journal_documents",
]
