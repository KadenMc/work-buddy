"""Durable recording boundary for ordinary direct Co-work editor updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from work_buddy.document_kernel.causality import (
    DocumentCausalityStore,
    DocumentChangeRecord,
    DomainDocumentBinding,
)
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.protocol import (
    PROTOCOL_VERSION,
    RUNTIME_VERSION,
    SCHEMA_VERSION,
    sha256_bytes,
    structured_head_sha256,
)
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.store import TruthStore


@dataclass(frozen=True, slots=True)
class DirectEditResult:
    change: DocumentChangeRecord
    structured_head_sha256: str


class DirectDocumentEditService:
    """Validate one browser-produced Yjs update, CAS it, and retain causality.

    Python treats the update as opaque. The shared document kernel establishes
    that it can be applied and projected under the canonical schema; Python
    independently checks hashes, generation/head preconditions, idempotency,
    and durable append/receipt state.
    """

    def __init__(self, *, kernel: DocumentKernelClient | None = None) -> None:
        self.kernel = kernel or DocumentKernelClient()

    def apply(
        self,
        store: TruthStore,
        *,
        document_id: str,
        update: bytes,
        expected_base_structured_head_sha256: str,
        expected_base_generation_sha256: str,
        actors: Mapping[str, str | None],
        idempotency_key: str,
        binding: DomainDocumentBinding | None = None,
        input_assurance: str = "caller_supplied",
        lock_guard: Callable[[], None] | None = None,
    ) -> DirectEditResult:
        causality = DocumentCausalityStore(store.paths.sidecar)
        prior = causality.intent_for_idempotency(idempotency_key)
        if prior is not None:
            if (
                prior.operation_kind != "direct_editor_update"
                or prior.store_id != store.store_id
                or prior.document_id != document_id
                or prior.binding_id != (None if binding is None else binding.binding_id)
                or prior.base_structured_head_sha256
                != expected_base_structured_head_sha256
                or prior.base_generation_sha256 != expected_base_generation_sha256
                or (
                    prior.result_update_sha256 is not None
                    and prior.result_update_sha256 != sha256_bytes(update)
                )
            ):
                raise RuntimeError("direct_edit_idempotency_conflict")
            committed = causality.get_change(prior.change_id)
            if committed is not None:
                return DirectEditResult(
                    committed,
                    committed.result_structured_head_sha256,
                )
        document = documents.get_document(store, document_id)
        if document.ydoc_snapshot_sha256 is None:
            raise RuntimeError("direct_edit_document_unavailable")
        snapshot = ydoc_store.read_snapshot(
            store,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        updates, _ = ydoc_store.read_updates(store, document_id=document_id)
        base_head = structured_head_sha256(snapshot, updates)
        if prior is None and base_head != expected_base_structured_head_sha256:
            raise RuntimeError("direct_edit_base_conflict")
        generation = documents.current_ydoc_generation(store, document_id)
        if prior is None and generation != expected_base_generation_sha256:
            raise RuntimeError("direct_edit_generation_conflict")
        if prior is None:
            result_head = structured_head_sha256(snapshot, (*updates, update))
            intent = causality.prepare_change(
                idempotency_key=idempotency_key,
                operation_kind="direct_editor_update",
                store_id=store.store_id,
                document_id=document_id,
                binding_id=None if binding is None else binding.binding_id,
                base_snapshot_sha256=document.ydoc_snapshot_sha256,
                base_structured_head_sha256=base_head,
                base_generation_sha256=generation,
                selector={"kind": "opaque_yjs_update/v1"},
                actors=dict(actors),
            )
        else:
            intent = prior

        if intent.state == "prepared":
            if base_head != intent.base_structured_head_sha256:
                raise RuntimeError("direct_edit_base_conflict")
            result_head = structured_head_sha256(snapshot, (*updates, update))
            outcome = self.kernel.request(
                {
                    "kind": "validate_yjs_update",
                    "snapshotBase64": snapshot,
                    "updatesBase64": updates,
                    "expectedBaseStructuredHeadSha256": base_head,
                    "updateBase64": update,
                    "expectedResultStructuredHeadSha256": result_head,
                },
                request_id=f"direct_{intent.change_id}",
            )
            projection = outcome.projection
            returned_update = outcome.update
            if projection is None or returned_update != update:
                raise RuntimeError("document_kernel_direct_edit_binding_mismatch")
            update_sha = sha256_bytes(update)
            projection_sha = sha256_bytes(projection)
            store._store_blob_bytes(update_sha, update)
            store._store_blob_bytes(projection_sha, projection)
            intent = causality.record_materialized(
                intent.change_id,
                result_snapshot_sha256=document.ydoc_snapshot_sha256,
                result_structured_head_sha256=result_head,
                result_projection_sha256=projection_sha,
                result_update_sha256=update_sha,
                operation_manifest_sha256=str(
                    outcome.values["operationManifestSha256"]
                ),
                protocol_version=PROTOCOL_VERSION,
                runtime_version=RUNTIME_VERSION,
                schema_version=SCHEMA_VERSION,
            )

        assert intent.result_update_sha256 is not None
        update_path = store.resolve_blob_path(f"blobs/{intent.result_update_sha256}")
        if not update_path.is_file():
            raise RuntimeError("materialized_update_missing")
        materialized_update = update_path.read_bytes()
        if sha256_bytes(materialized_update) != intent.result_update_sha256:
            raise RuntimeError("materialized_update_hash_mismatch")
        current = documents.get_document(store, document_id)
        if current.ydoc_snapshot_sha256 != intent.base_snapshot_sha256:
            raise RuntimeError("direct_edit_snapshot_conflict")
        live_head = ydoc_store.current_structured_head(
            store,
            document_id=document_id,
            snapshot_sha256=current.ydoc_snapshot_sha256,
        )
        if live_head == intent.result_structured_head_sha256:
            pass
        elif live_head != intent.base_structured_head_sha256:
            raise RuntimeError("direct_edit_base_conflict")
        else:
            if (
                documents.current_ydoc_generation(store, document_id)
                != intent.base_generation_sha256
            ):
                raise RuntimeError("direct_edit_generation_conflict")
            _cursor, committed_head = ydoc_store.append_update_cas(
                store,
                document_id=document_id,
                snapshot_sha256=current.ydoc_snapshot_sha256,
                update=materialized_update,
                expected_structured_head_sha256=intent.base_structured_head_sha256,
                lock_guard=lock_guard,
            )
            if committed_head != intent.result_structured_head_sha256:
                raise RuntimeError("direct_edit_result_head_mismatch")
        record = causality.commit_change(
            intent.change_id,
            assurance={
                "structured_update": "document_kernel_verified",
                "inputter": input_assurance,
                "persistence": "persistence_verified",
                "journal_projection": "not_checked",
            },
        )
        return DirectEditResult(record, record.result_structured_head_sha256)


__all__ = ["DirectDocumentEditService", "DirectEditResult"]
