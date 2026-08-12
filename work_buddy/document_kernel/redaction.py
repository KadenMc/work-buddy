"""Managed-copy scrub for source-backed Co-work documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

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
from work_buddy.document_kernel.runtime_service import shared_document_kernel
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor
from work_buddy.truth.store import TruthStore


_REDACTED_DOCUMENT = b"[redacted]\n"


@dataclass(frozen=True, slots=True)
class BoundDocumentRedaction:
    binding: DomainDocumentBinding
    change: DocumentChangeRecord
    replacement_document_version_id: str


def _replacement_document_version_id(
    store: TruthStore,
    *,
    change: DocumentChangeRecord,
    redaction_event_id: str,
) -> str:
    """Resolve the exact immutable version produced by a scrub attempt."""

    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT id FROM document_versions WHERE document_id = ? "
            "AND projection_sha256 = ? AND ydoc_snapshot_sha256 = ? "
            "AND detail = ? ORDER BY rowid DESC LIMIT 1",
            (
                change.document_id,
                change.result_projection_sha256,
                change.result_snapshot_sha256,
                f"source-redaction:{redaction_event_id}",
            ),
        ).fetchone()
    if row is None:
        raise RuntimeError("document_redaction_replacement_version_missing")
    return str(row["id"])


class BoundDocumentRedactionService:
    def __init__(self, *, kernel: DocumentKernelClient | None = None) -> None:
        self.kernel = kernel or shared_document_kernel()

    def scrub(
        self,
        store: TruthStore,
        *,
        binding: DomainDocumentBinding,
        source_ref: str,
        redaction_event_id: str,
        actors: Mapping[str, str | None],
    ) -> BoundDocumentRedaction:
        """Replace the canonical document with a content-free tombstone.

        The source itself is already redacted, so the operation never resolves
        or replays its old bytes.  A durable change intent and snapshot
        replacement marker close every crash boundary before the source usage
        may be released.
        """

        causality = DocumentCausalityStore(store.paths.sidecar)
        key = f"source-redaction:{redaction_event_id}:{binding.binding_id}"
        prior = causality.intent_for_idempotency(key)
        if prior is not None:
            committed = causality.get_change(prior.change_id)
            if committed is not None:
                retired = causality.retire_binding(binding.binding_id)
                return BoundDocumentRedaction(
                    retired,
                    committed,
                    _replacement_document_version_id(
                        store,
                        change=committed,
                        redaction_event_id=redaction_event_id,
                    ),
                )

        document = documents.get_document(store, binding.document_id)
        if document.ydoc_snapshot_sha256 is None:
            raise RuntimeError("bound_document_has_no_snapshot")
        snapshot = ydoc_store.read_snapshot(
            store, snapshot_sha256=document.ydoc_snapshot_sha256
        )
        updates, _ = ydoc_store.read_updates(store, document_id=document.id)
        base_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        generation = documents.current_ydoc_generation(store, document.id)
        intent = causality.prepare_change(
            idempotency_key=key,
            operation_kind="source_redaction_scrub",
            store_id=store.store_id,
            document_id=document.id,
            binding_id=binding.binding_id,
            source_ref=source_ref,
            base_snapshot_sha256=document.ydoc_snapshot_sha256,
            base_structured_head_sha256=base_head,
            base_generation_sha256=generation,
            selector={
                "kind": "whole_document/v1",
                "redaction_event_id": redaction_event_id,
            },
            actors=dict(actors),
        )
        if intent.state == "prepared":
            source_sha = sha256_bytes(_REDACTED_DOCUMENT)
            outcome = self.kernel.request(
                {
                    "kind": "bootstrap_markdown",
                    "sourceBase64": _REDACTED_DOCUMENT,
                    "sourceSha256": source_sha,
                    "newlineStyle": "lf",
                    "utf8Bom": False,
                    "trailingNewlineCount": 1,
                },
                request_id=f"redact_{intent.change_id}",
            )
            result_snapshot = outcome.snapshot
            result_projection = outcome.projection
            if result_snapshot is None or result_projection is None:
                raise RuntimeError("document_kernel_missing_result")
            result_snapshot_sha = ydoc_store.write_snapshot(
                store,
                snapshot=result_snapshot,
                expected_sha256=sha256_bytes(result_snapshot),
            )
            result_projection_sha = sha256_bytes(result_projection)
            empty_update_sha = sha256_bytes(b"")
            store._store_blob_bytes(result_projection_sha, result_projection)
            store._store_blob_bytes(empty_update_sha, b"")
            intent = causality.record_materialized(
                intent.change_id,
                result_snapshot_sha256=result_snapshot_sha,
                result_structured_head_sha256=structured_head_sha256(result_snapshot),
                result_projection_sha256=result_projection_sha,
                result_update_sha256=empty_update_sha,
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
            raise RuntimeError("materialized_projection_missing")
        result_projection = projection_path.read_bytes()
        if sha256_bytes(result_projection) != intent.result_projection_sha256:
            raise RuntimeError("materialized_projection_hash_mismatch")

        with ydoc_store.document_lock(store, document.id):
            ydoc_store.recover_compaction_locked(store, document_id=document.id)
            current = documents.get_document(store, document.id)
            if current.ydoc_snapshot_sha256 != intent.result_snapshot_sha256:
                if current.ydoc_snapshot_sha256 != intent.base_snapshot_sha256:
                    raise RuntimeError("document_redaction_base_conflict")
                replacement = ydoc_store.prepare_snapshot_replacement_locked(
                    store,
                    document_id=document.id,
                    snapshot=result_snapshot,
                    expected_new_snapshot_sha256=intent.result_snapshot_sha256,
                    expected_current_snapshot_sha256=intent.base_snapshot_sha256,
                    expected_current_structured_head_sha256=(
                        intent.base_structured_head_sha256
                    ),
                    projection_sha256=intent.result_projection_sha256,
                )
                try:
                    documents.commit_document_version(
                        store,
                        document_id=document.id,
                        kind="materialized",
                        projection_sha256=intent.result_projection_sha256,
                        ydoc_snapshot_sha256=replacement.snapshot_sha256,
                        structured_head_sha256=replacement.structured_head_sha256,
                        actor=Actor(kind="system", ref="sources-redaction"),
                        detail=f"source-redaction:{redaction_event_id}",
                    )
                except BaseException:
                    ydoc_store.abort_snapshot_replacement_locked(
                        store,
                        document_id=document.id,
                        expected_snapshot_sha256=replacement.snapshot_sha256,
                    )
                    raise
                ydoc_store.finish_snapshot_replacement_locked(
                    store,
                    document_id=document.id,
                    expected_snapshot_sha256=replacement.snapshot_sha256,
                )
        change = causality.commit_change(
            intent.change_id,
            assurance={
                "content_removal": "document_kernel_replacement_verified",
                "source_redaction_event": redaction_event_id,
                "persistence": "persistence_verified",
                "journal_projection": "separately_managed",
            },
        )
        retired = causality.retire_binding(binding.binding_id)
        return BoundDocumentRedaction(
            retired,
            change,
            _replacement_document_version_id(
                store,
                change=change,
                redaction_event_id=redaction_event_id,
            ),
        )


__all__ = ["BoundDocumentRedaction", "BoundDocumentRedactionService"]
