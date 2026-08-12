"""Crash-safe, source-backed whole-document changes for bound task notes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from work_buddy.document_kernel.causality import (
    DocumentCausalityStore,
    DocumentChangeRecord,
)
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.domain_service import (
    DomainContentStoreManager,
    RunningNoteDocumentService,
)
from work_buddy.document_kernel.protocol import (
    PROTOCOL_VERSION,
    RUNTIME_VERSION,
    SCHEMA_VERSION,
    sha256_bytes,
    structured_head_sha256,
)
from work_buddy.sources import (
    ActorRef,
    ReservedResolution,
    SourceRef,
    SourceStore,
    resolve_and_reserve_source,
)
from work_buddy.sources.models import canonical_sha256
from work_buddy.task_notes.adapter import TaskNoteContentError, validate_note_uuid
from work_buddy.task_notes.migration import (
    BoundTaskNoteReader,
    TaskNoteProjectionWorker,
    _newline_style,
    _trailing_newlines,
)
from work_buddy.task_notes.models import (
    AuthorityState,
    ChangeOperationState,
    ProjectionOutcome,
    ProjectionState,
    SourceDependencyState,
    TaskNoteChangeOperation,
)
from work_buddy.task_notes.store import (
    TaskNoteMigrationConflict,
    TaskNoteMigrationStore,
)
from work_buddy.truth import documents, ydoc_store


@dataclass(frozen=True, slots=True)
class TaskNoteChangeResult:
    operation: TaskNoteChangeOperation
    change: DocumentChangeRecord
    projection: ProjectionOutcome


class TaskNoteSourceChangeService:
    """Replace one Co-work-authoritative task-note body from exact Source bytes.

    Sources remains the redaction authority, the document change receipt is
    the durable content-change authority, and the task-note store records only
    the cross-domain recovery/mirror state required to finish acknowledgement
    and compatibility projection after a crash.
    """

    def __init__(
        self,
        *,
        vault_root: str | Path,
        migrations: TaskNoteMigrationStore,
        sources: SourceStore,
        principal: ActorRef,
        kernel: DocumentKernelClient | None = None,
        stores: DomainContentStoreManager | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.migrations = migrations
        self.sources = sources
        self.principal = principal
        self.kernel = kernel or DocumentKernelClient()
        self._owns_kernel = kernel is None
        self.stores = stores or DomainContentStoreManager()
        self.reader = BoundTaskNoteReader(
            vault_root=self.vault_root,
            migration_store=migrations,
            stores=self.stores,
        )
        self.projector = TaskNoteProjectionWorker(
            vault_root=self.vault_root,
            migrations=migrations,
            source_store=sources,
            principal=principal,
            reader=self.reader,
            kernel=self.kernel,
        )

    def close(self) -> None:
        if self._owns_kernel:
            self.kernel.close()

    def __enter__(self) -> "TaskNoteSourceChangeService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def replace(
        self,
        note_uuid: str,
        content: str,
        *,
        idempotency_key: str,
        actors: Mapping[str, str | None] | None = None,
    ) -> TaskNoteChangeResult:
        """Capture exact UTF-8 input, then replace through a reserved Source use."""

        note_uuid = validate_note_uuid(note_uuid)
        exact = content.encode("utf-8")
        content_sha = sha256_bytes(exact)
        key = f"task-note-replace:{idempotency_key}"
        request_sha = canonical_sha256(
            {
                "schema": "wb.task-note-source-change/v1",
                "note_uuid": note_uuid,
                "content_sha256": content_sha,
            }
        )
        operation = self.migrations.begin_change_operation(
            idempotency_key=key,
            request_sha256=request_sha,
            note_uuid=note_uuid,
        )
        try:
            self._admit_authority(note_uuid)
            if operation.source_ref is None:
                item = self.sources.capture_source(
                    content=exact,
                    source_role="derived_content",
                    tenant_scope_id=self.principal.tenant_scope_id,
                    originating_surface="task_note_content_adapter",
                    media_type="text/markdown",
                    representation_kind="decoded_text",
                    encoding="utf-8",
                    namespace="task-note-source-change",
                    producer=self.principal,
                )
                representation = self.sources.get_representation(
                    item.primary_representation_id
                )
                if representation is None or representation.content_sha256 != content_sha:
                    raise TaskNoteContentError(
                        "captured task-note Source could not be verified"
                    )
                operation = self.migrations.advance_change_operation(
                    operation.operation_id,
                    state=ChangeOperationState.PREPARED,
                    source_ref=item.source_ref.uri,
                    representation_id=representation.representation_id,
                    source_content_sha256=content_sha,
                )
            elif operation.source_content_sha256 != content_sha:
                raise TaskNoteMigrationConflict(
                    "task-note change Source does not match the request"
                )
            reserved = self._reserve(operation)
            return self._apply_reserved(
                operation,
                reserved,
                actors=actors
                or {
                    "selected_by": self.principal.canonical_id,
                    "applied_by": self.principal.canonical_id,
                },
            )
        except Exception as exc:
            latest = self.migrations.get_change_operation(operation.operation_id)
            if latest is not None and latest.state not in {
                ChangeOperationState.COMPLETED,
                ChangeOperationState.REVIEW_REQUIRED,
            }:
                self.migrations.advance_change_operation(
                    operation.operation_id,
                    state=ChangeOperationState.RECOVERABLE,
                    error_code=getattr(exc, "code", type(exc).__name__),
                )
            raise

    def recover(self, operation_id: str) -> TaskNoteChangeResult:
        """Resume a captured operation without accepting replacement bytes again."""

        operation = self.migrations.get_change_operation(operation_id)
        if operation is None:
            raise KeyError("task_note_change_operation_not_found")
        self._admit_authority(operation.note_uuid)
        reserved = self._reserve(operation)
        return self._apply_reserved(
            operation,
            reserved,
            actors={
                "selected_by": self.principal.canonical_id,
                "applied_by": self.principal.canonical_id,
                "recovered_by": self.principal.canonical_id,
            },
        )

    def recover_all(self, *, limit: int = 25) -> tuple[TaskNoteChangeResult, ...]:
        results: list[TaskNoteChangeResult] = []
        for operation in self.migrations.recoverable_change_operations()[: max(1, limit)]:
            results.append(self.recover(operation.operation_id))
        return tuple(results)

    def _admit_authority(self, note_uuid: str):
        authority = self.migrations.get_authority("tasks", "task_note", note_uuid)
        migration = self.migrations.get_task_note(note_uuid)
        if (
            authority is None
            or authority.state is not AuthorityState.COWORK
            or migration is None
            or migration.binding_id is None
            or migration.document_id is None
            or migration.store_id is None
        ):
            raise TaskNoteContentError(
                "task note is not a bound Co-work-authoritative document"
            )
        store = self.stores.ensure(self.vault_root)
        if store.store_id != migration.store_id:
            raise TaskNoteContentError("task-note store binding does not match")
        binding = DocumentCausalityStore(store.paths.sidecar).get_binding(
            migration.binding_id
        )
        if (
            binding is None
            or binding.lifecycle != "current"
            or binding.content_authority != "co_work"
            or binding.content_authority_epoch != authority.epoch
            or binding.document_id != migration.document_id
        ):
            raise TaskNoteContentError("task-note authority binding is not current")
        return authority, migration, store, binding

    def _reserve(self, operation: TaskNoteChangeOperation) -> ReservedResolution:
        _authority, migration, _store, binding = self._admit_authority(
            operation.note_uuid
        )
        if (
            operation.source_ref is None
            or operation.representation_id is None
            or operation.source_content_sha256 is None
        ):
            raise TaskNoteContentError("task-note change Source is incomplete")
        source_ref = SourceRef.parse(operation.source_ref)
        consumer_id = operation.operation_id
        if operation.source_usage_id is None:
            self.sources.grant_access(
                source_ref=source_ref,
                principal=self.principal,
                purpose="task_note.replace",
                access_mode="content",
                authorization_fingerprint=canonical_sha256(
                    {
                        "schema": "wb.task-note-source-access/v1",
                        "operation_id": operation.operation_id,
                        "source_ref": operation.source_ref,
                        "representation_id": operation.representation_id,
                        "content_sha256": operation.source_content_sha256,
                    }
                ),
                scope={
                    "consumer_domain": "cowork_document",
                    "use_kind": "exact_insertion",
                },
                content_boundary={
                    "representation_id": operation.representation_id,
                },
            )
        reserved = resolve_and_reserve_source(
            self.sources,
            source_ref=source_ref,
            representation_id=operation.representation_id,
            principal=self.principal,
            purpose="task_note.replace",
            consumer_domain="cowork_document",
            consumer_id=consumer_id,
            use_kind="exact_insertion",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole_document/v1"},
            expected_digest=operation.source_content_sha256,
        )
        if (
            operation.source_usage_id is not None
            and operation.source_usage_id != reserved.reservation.usage_id
        ):
            raise TaskNoteMigrationConflict(
                "task-note change Source reservation does not match"
            )
        self.migrations.record_source_dependency(
            usage_id=reserved.reservation.usage_id,
            note_uuid=operation.note_uuid,
            consumer_id=consumer_id,
            relationship="whole_document_replace",
            source_ref=source_ref.uri,
            representation_id=operation.representation_id,
            content_sha256=operation.source_content_sha256,
            redaction_epoch=reserved.reservation.redaction_epoch,
            binding_id=binding.binding_id,
            store_id=migration.store_id,
            document_id=migration.document_id,
        )
        self.migrations.advance_change_operation(
            operation.operation_id,
            state=ChangeOperationState.SOURCE_RESERVED,
            source_usage_id=reserved.reservation.usage_id,
        )
        return reserved

    def _apply_reserved(
        self,
        operation: TaskNoteChangeOperation,
        reserved: ReservedResolution,
        *,
        actors: Mapping[str, str | None],
    ) -> TaskNoteChangeResult:
        _authority, migration, store, binding = self._admit_authority(
            operation.note_uuid
        )
        resolved = reserved.resolved
        if sha256_bytes(resolved.content) != resolved.representation.content_sha256:
            raise TaskNoteContentError("task-note Source digest changed")
        resolved.content.decode("utf-8-sig")
        causality = DocumentCausalityStore(store.paths.sidecar)
        change_key = f"task-note-source-change:{operation.operation_id}"
        intent = causality.intent_for_idempotency(change_key)
        committed = None if intent is None else causality.get_change(intent.change_id)
        if committed is None:
            if intent is None:
                document = documents.get_document(store, migration.document_id)
                if document.ydoc_snapshot_sha256 is None:
                    raise TaskNoteContentError("bound task-note head is unavailable")
                base_head = ydoc_store.current_structured_head(
                    store,
                    document_id=document.id,
                    snapshot_sha256=document.ydoc_snapshot_sha256,
                )
                intent = causality.prepare_change(
                    idempotency_key=change_key,
                    operation_kind="exact_source_copy",
                    store_id=store.store_id,
                    document_id=document.id,
                    binding_id=binding.binding_id,
                    source_ref=resolved.source_ref.uri,
                    source_representation_id=resolved.representation.representation_id,
                    source_content_sha256=resolved.representation.content_sha256,
                    exact_copied_text_sha256=resolved.representation.content_sha256,
                    base_snapshot_sha256=document.ydoc_snapshot_sha256,
                    base_structured_head_sha256=base_head,
                    base_generation_sha256=documents.current_ydoc_generation(
                        store, document.id
                    ),
                    selector={"kind": "whole_document/v1"},
                    actors=dict(actors),
                )
                self.migrations.advance_change_operation(
                    operation.operation_id,
                    state=ChangeOperationState.SOURCE_RESERVED,
                    change_id=intent.change_id,
                )
            elif (
                intent.source_ref != resolved.source_ref.uri
                or intent.source_representation_id
                != resolved.representation.representation_id
                or intent.source_content_sha256
                != resolved.representation.content_sha256
                or intent.binding_id != binding.binding_id
            ):
                raise TaskNoteMigrationConflict(
                    "task-note document change receipt does not match Source"
                )

            if intent.state == "prepared":
                current = documents.get_document(store, intent.document_id)
                if current.ydoc_snapshot_sha256 != intent.base_snapshot_sha256:
                    raise TaskNoteContentError("task-note document changed during recovery")
                if (
                    documents.current_ydoc_generation(store, intent.document_id)
                    != intent.base_generation_sha256
                ):
                    raise TaskNoteContentError("task-note generation changed during recovery")
                snapshot = ydoc_store.read_snapshot(
                    store, snapshot_sha256=intent.base_snapshot_sha256
                )
                updates, _ = ydoc_store.read_updates(
                    store, document_id=intent.document_id
                )
                outcome = self.kernel.request(
                    {
                        "kind": "apply_source_markdown",
                        "snapshotBase64": snapshot,
                        "updatesBase64": updates,
                        "expectedBaseStructuredHeadSha256": (
                            intent.base_structured_head_sha256
                        ),
                        "sourceBase64": resolved.content,
                        "sourceSha256": resolved.representation.content_sha256,
                        "newlineStyle": _newline_style(resolved.content),
                        "utf8Bom": resolved.content.startswith(b"\xef\xbb\xbf"),
                        "trailingNewlineCount": _trailing_newlines(resolved.content),
                    },
                    request_id=f"task_note_change_{intent.change_id}",
                )
                if (
                    outcome.snapshot is None
                    or outcome.projection is None
                    or outcome.update is None
                    or outcome.values.get("exactCopiedTextSha256")
                    != resolved.representation.content_sha256
                ):
                    raise TaskNoteContentError(
                        "document kernel did not verify the exact task-note copy"
                    )
                result_snapshot = outcome.snapshot
                result_projection = outcome.projection
                ydoc_store.write_snapshot(
                    store,
                    snapshot=result_snapshot,
                    expected_sha256=sha256_bytes(result_snapshot),
                )
                store._store_blob_bytes(
                    sha256_bytes(result_projection), result_projection
                )
                intent = causality.record_materialized(
                    intent.change_id,
                    result_snapshot_sha256=sha256_bytes(result_snapshot),
                    result_structured_head_sha256=structured_head_sha256(
                        result_snapshot
                    ),
                    result_projection_sha256=sha256_bytes(result_projection),
                    result_update_sha256=sha256_bytes(outcome.update),
                    operation_manifest_sha256=str(
                        outcome.values["operationManifestSha256"]
                    ),
                    protocol_version=PROTOCOL_VERSION,
                    runtime_version=RUNTIME_VERSION,
                    schema_version=SCHEMA_VERSION,
                )
            assert intent.result_snapshot_sha256 is not None
            assert intent.result_projection_sha256 is not None
            result_snapshot = ydoc_store.read_snapshot(
                store, snapshot_sha256=intent.result_snapshot_sha256
            )
            projection_path = store.resolve_blob_path(
                f"blobs/{intent.result_projection_sha256}"
            )
            if not projection_path.is_file():
                raise TaskNoteContentError("materialized task-note projection is missing")
            result_projection = projection_path.read_bytes()
            if sha256_bytes(result_projection) != intent.result_projection_sha256:
                raise TaskNoteContentError("materialized task-note projection changed")
            if reserved.reservation.status == "reserved":
                self.sources.precommit_recheck_usage(
                    reserved.reservation.usage_id
                )
            committed = RunningNoteDocumentService._commit_or_reconcile(
                store,
                causality=causality,
                intent=intent,
                result_snapshot=result_snapshot,
                result_projection=result_projection,
            )

        assert committed is not None
        result_head = committed.result_structured_head_sha256
        self.migrations.update_source_dependency(
            reserved.reservation.usage_id,
            result_document_head_sha256=result_head,
        )
        self.migrations.advance_change_operation(
            operation.operation_id,
            state=ChangeOperationState.DOCUMENT_COMMITTED,
            change_id=committed.change_id,
            result_document_head_sha256=result_head,
        )
        self.sources.acknowledge_usage(reserved.reservation.usage_id)
        self.migrations.update_source_dependency(
            reserved.reservation.usage_id,
            state=SourceDependencyState.ACKNOWLEDGED,
        )
        self.migrations.advance_change_operation(
            operation.operation_id,
            state=ChangeOperationState.ACKNOWLEDGED,
        )

        projection = self.projector.project(operation.note_uuid)
        if projection.state is ProjectionState.CURRENT:
            self._release_superseded(
                operation.note_uuid, current_usage_id=reserved.reservation.usage_id
            )
            state = ChangeOperationState.COMPLETED
        else:
            state = ChangeOperationState.REVIEW_REQUIRED
        final = self.migrations.advance_change_operation(
            operation.operation_id,
            state=state,
            projection_state=projection.state.value,
        )
        return TaskNoteChangeResult(final, committed, projection)

    def _release_superseded(self, note_uuid: str, *, current_usage_id: str) -> None:
        for prior in self.migrations.source_dependencies_for_note(
            note_uuid, active_only=True
        ):
            if prior.usage_id == current_usage_id:
                continue
            released = self.sources.release_usage_if_source_active(prior.usage_id)
            if released is None:
                self.migrations.update_source_dependency(
                    prior.usage_id,
                    state=SourceDependencyState.REVIEW_REQUIRED,
                    superseded_by_usage_id=current_usage_id,
                )
            else:
                self.migrations.update_source_dependency(
                    prior.usage_id,
                    state=SourceDependencyState.RELEASED,
                    superseded_by_usage_id=current_usage_id,
                )


__all__ = [
    "TaskNoteChangeResult",
    "TaskNoteSourceChangeService",
]
