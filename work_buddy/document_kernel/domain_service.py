"""Domain-bound Co-work documents and source-backed Running Note pilot."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from work_buddy.document_kernel.causality import (
    DocumentCausalityStore,
    DocumentChangeRecord,
    DomainDocumentBinding,
    PreparedDocumentChange,
)
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.protocol import (
    PROTOCOL_VERSION,
    RUNTIME_VERSION,
    SCHEMA_VERSION,
    sha256_bytes,
    structured_head_sha256,
)
from work_buddy.paths import data_dir
from work_buddy.sources import ReservedResolution, SourceStore
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import TruthStore


_STORE_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class BoundDocumentChange:
    binding: DomainDocumentBinding
    change: DocumentChangeRecord
    store: TruthStore


def _newline_style(value: bytes) -> str:
    if b"\r\n" in value:
        return "crlf"
    if b"\n" in value:
        return "lf"
    if b"\r" in value:
        return "cr"
    return "none"


def _trailing_newlines(value: bytes) -> int:
    text = value.decode("utf-8-sig")
    count = 0
    cursor = len(text)
    while cursor > 0:
        if text[:cursor].endswith("\r\n"):
            cursor -= 2
        elif text[cursor - 1] in "\r\n":
            cursor -= 1
        else:
            break
        count += 1
    return count


class DomainContentStoreManager:
    """One deterministic, machine-registered domain-content store per vault."""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        registry: TruthStoreRegistry | None = None,
    ) -> None:
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else data_dir("cowork-domain-content")
        )
        self.registry = registry or TruthStoreRegistry()

    @staticmethod
    def vault_id(vault_root: str | Path) -> str:
        canonical = Path(vault_root).expanduser().resolve()
        return hashlib.sha256(
            f"work-buddy-vault-domain-content/v1\0{canonical}".encode("utf-8")
        ).hexdigest()[:32]

    def ensure(self, vault_root: str | Path) -> TruthStore:
        canonical = Path(vault_root).expanduser().resolve()
        vault_id = self.vault_id(canonical)
        store_root = self.root / vault_id
        sidecar = store_root / ".wbuddy" / "cowork"
        with _STORE_LOCK:
            if (sidecar / "store.yaml").is_file():
                store = TruthStore.open(sidecar)
            else:
                from work_buddy.backups.source_foundation_restore import (
                    require_source_foundation_writable,
                )

                require_source_foundation_writable(
                    "document_domain_content_store.create"
                )
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                profile = {
                    "store_id": vault_id,
                    "profile": "cowork-domain-content-v1",
                    "title": f"{canonical.name} content",
                    "allowed_claim_kinds": ["fact", "preference", "decision", "commitment"],
                    "required_fields": {},
                    "gate": {
                        "rejected_content": "retain",
                        "confirmation_surfaces": ["dashboard", "cli", "chat_consent"],
                        "block_materialize_on_flags": False,
                    },
                    "projection": "resident",
                    "export_committed": True,
                    "document_surface": {
                        "enabled": True,
                        "allowed_document_classes": ["co_authored"],
                        "feedback_capture": True,
                    },
                }
                store = TruthStore.create(sidecar, profile)
            self.registry.register(store)
            return store

    def open_existing(self, vault_root: str | Path) -> TruthStore:
        """Open an already-bound domain store without creating or registering it."""

        canonical = Path(vault_root).expanduser().resolve()
        sidecar = (
            self.root
            / self.vault_id(canonical)
            / ".wbuddy"
            / "cowork"
        )
        if not (sidecar / "store.yaml").is_file():
            raise FileNotFoundError("domain_content_store_missing")
        return TruthStore.open(sidecar)


class RunningNoteDocumentService:
    """Materialize one stable Journal Running Note into a bound Co-work doc."""

    def __init__(
        self,
        *,
        kernel: DocumentKernelClient | None = None,
        stores: DomainContentStoreManager | None = None,
    ) -> None:
        self.kernel = kernel or DocumentKernelClient()
        self.stores = stores or DomainContentStoreManager()

    def _ensure_document(
        self,
        store: TruthStore,
        *,
        entry_id: str,
        domain_kind: str = "running_note",
        document_path: str | None = None,
        title: str = "Running Note",
    ):
        path = document_path or f"journal/running-notes/{entry_id}.md"
        existing = next(
            (item for item in documents.list_documents(store) if item.path == path),
            None,
        )
        if existing is not None:
            return existing
        empty_sha = sha256_bytes(b"")
        outcome = self.kernel.request(
            {
                "kind": "bootstrap_markdown",
                "sourceBase64": b"",
                "sourceSha256": empty_sha,
                "newlineStyle": "none",
                "utf8Bom": False,
                "trailingNewlineCount": 0,
            },
            request_id=f"bootstrap_{entry_id}",
        )
        snapshot = outcome.snapshot
        projection = outcome.projection
        if snapshot is None or projection is None:
            raise RuntimeError("document_kernel_missing_result")
        snapshot_sha = ydoc_store.write_snapshot(
            store,
            snapshot=snapshot,
            expected_sha256=sha256_bytes(snapshot),
        )
        head = structured_head_sha256(snapshot)
        document_id = hashlib.sha256(
            f"journal-running-note-document:{entry_id}".encode()
        ).hexdigest()[:32]
        record, _version, _created = documents.register_ready_document(
            store,
            path=path,
            title=title,
            document_class="co_authored",
            projection_bytes=projection,
            ydoc_snapshot_sha256=snapshot_sha,
            structured_head_sha256=head,
            actor=Actor(kind="system", ref="document-kernel"),
            mode="create",
            document_meta={
                "domain_content": True,
                "source": {
                    "kind": "domain_binding",
                    "writeback_policy": "never",
                },
            },
            document_id=document_id,
            version_id=hashlib.sha256(
                f"journal-running-note-version:{entry_id}:initial".encode()
            ).hexdigest()[:32],
        )
        self.stores.registry.touch(store)
        return record

    def materialize(
        self,
        *,
        vault_root: str | Path,
        entry_id: str,
        day_id: str,
        domain_revision: str,
        source_store: SourceStore,
        reserved_source: ReservedResolution,
        actors: Mapping[str, str | None],
        idempotency_key: str,
        projection_path: str | None = None,
        domain_kind: str = "running_note",
        role: str = "running_note",
        document_path: str | None = None,
        title: str = "Running Note",
        migration_origin: str = "journal-capture-v1",
        cutover: bool = True,
    ) -> BoundDocumentChange:
        resolved = reserved_source.resolved
        if sha256_bytes(resolved.content) != resolved.representation.content_sha256:
            raise RuntimeError("source_resolution_digest_mismatch")
        # Running Notes are Markdown-compatible UTF-8 text; decoding here is an
        # independent admission check, not a Yjs interpretation.
        resolved.content.decode("utf-8-sig")
        store = self.stores.ensure(vault_root)
        document = self._ensure_document(
            store,
            entry_id=entry_id,
            domain_kind=domain_kind,
            document_path=document_path,
            title=title,
        )
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.ensure_binding(
            domain_namespace="journal",
            domain_kind=domain_kind,
            domain_entity_id=entry_id,
            domain_revision=domain_revision,
            store_id=store.store_id,
            document_id=document.id,
            role=role,
            created_by=actors.get("selected_by") or "system:journal-capture",
            projection_path=projection_path or f"journal/{day_id}.md",
            projection_mode="managed_section",
            migration_origin=migration_origin,
        )
        source_ref = resolved.source_ref.uri
        current = documents.get_document(store, document.id)
        if current.ydoc_snapshot_sha256 is None:
            raise RuntimeError("bound_document_has_no_snapshot")
        snapshot = ydoc_store.read_snapshot(
            store, snapshot_sha256=current.ydoc_snapshot_sha256
        )
        updates, _cursor = ydoc_store.read_updates(store, document_id=document.id)
        base_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=current.ydoc_snapshot_sha256,
        )
        generation = documents.current_ydoc_generation(store, document.id)
        intent = causality.prepare_change(
            idempotency_key=idempotency_key,
            operation_kind="exact_source_copy",
            store_id=store.store_id,
            document_id=document.id,
            binding_id=binding.binding_id,
            source_ref=source_ref,
            source_representation_id=resolved.representation.representation_id,
            source_content_sha256=resolved.representation.content_sha256,
            exact_copied_text_sha256=resolved.representation.content_sha256,
            base_snapshot_sha256=current.ydoc_snapshot_sha256,
            base_structured_head_sha256=base_head,
            base_generation_sha256=generation,
            selector={"kind": "whole_document/v1"},
            actors=dict(actors),
        )
        committed = causality.get_change(intent.change_id)
        if committed is not None:
            if cutover:
                binding = causality.cutover_to_cowork(
                    binding.binding_id,
                    domain_revision=domain_revision,
                )
            source_store.acknowledge_usage(reserved_source.reservation.usage_id)
            return BoundDocumentChange(binding, committed, store)

        if intent.state == "prepared":
            outcome = self.kernel.request(
                {
                    "kind": "apply_source_markdown",
                    "snapshotBase64": snapshot,
                    "updatesBase64": updates,
                    "expectedBaseStructuredHeadSha256": base_head,
                    "sourceBase64": resolved.content,
                    "sourceSha256": resolved.representation.content_sha256,
                    "newlineStyle": _newline_style(resolved.content),
                    "utf8Bom": resolved.content.startswith(b"\xef\xbb\xbf"),
                    "trailingNewlineCount": _trailing_newlines(resolved.content),
                },
                request_id=f"change_{intent.change_id}",
            )
            result_snapshot = outcome.snapshot
            result_projection = outcome.projection
            result_update = outcome.update
            if result_snapshot is None or result_projection is None or result_update is None:
                raise RuntimeError("document_kernel_missing_result")
            if outcome.values.get("exactCopiedTextSha256") != (
                resolved.representation.content_sha256
            ):
                raise RuntimeError("document_kernel_copy_binding_mismatch")
            result_head = structured_head_sha256(result_snapshot)
            # The kernel result becomes durable while still isolated from the
            # canonical document pointer. This closes the crash boundary
            # between recording `materialized` and committing the CAS.
            ydoc_store.write_snapshot(
                store,
                snapshot=result_snapshot,
                expected_sha256=sha256_bytes(result_snapshot),
            )
            store._store_blob_bytes(
                sha256_bytes(result_projection),
                result_projection,
            )
            intent = causality.record_materialized(
                intent.change_id,
                result_snapshot_sha256=sha256_bytes(result_snapshot),
                result_structured_head_sha256=result_head,
                result_projection_sha256=sha256_bytes(result_projection),
                result_update_sha256=sha256_bytes(result_update),
                operation_manifest_sha256=str(
                    outcome.values["operationManifestSha256"]
                ),
                protocol_version=PROTOCOL_VERSION,
                runtime_version=RUNTIME_VERSION,
                schema_version=SCHEMA_VERSION,
            )
        else:
            assert intent.result_snapshot_sha256 is not None
            assert intent.result_projection_sha256 is not None
            result_snapshot = ydoc_store.read_snapshot(
                store,
                snapshot_sha256=intent.result_snapshot_sha256,
            )
            projection_path = store.resolve_blob_path(
                f"blobs/{intent.result_projection_sha256}"
            )
            if not projection_path.is_file():
                raise RuntimeError("materialized_projection_missing")
            result_projection = projection_path.read_bytes()
            if sha256_bytes(result_projection) != intent.result_projection_sha256:
                raise RuntimeError("materialized_projection_hash_mismatch")

        source_store.precommit_recheck_usage(reserved_source.reservation.usage_id)
        record = self._commit_or_reconcile(
            store,
            causality=causality,
            intent=intent,
            result_snapshot=result_snapshot,
            result_projection=result_projection,
        )
        if cutover:
            binding = causality.cutover_to_cowork(
                binding.binding_id,
                domain_revision=domain_revision,
            )
        source_store.acknowledge_usage(reserved_source.reservation.usage_id)
        return BoundDocumentChange(binding, record, store)

    @staticmethod
    def _commit_or_reconcile(
        store: TruthStore,
        *,
        causality: DocumentCausalityStore,
        intent: PreparedDocumentChange,
        result_snapshot: bytes | None,
        result_projection: bytes | None,
    ) -> DocumentChangeRecord:
        assert intent.result_snapshot_sha256 is not None
        assert intent.result_structured_head_sha256 is not None
        assert intent.result_projection_sha256 is not None
        document = documents.get_document(store, intent.document_id)
        with ydoc_store.document_lock(store, intent.document_id):
            ydoc_store.recover_compaction_locked(store, document_id=intent.document_id)
            document = documents.get_document(store, intent.document_id)
            if document.ydoc_snapshot_sha256 == intent.result_snapshot_sha256:
                # The pointer transaction committed before a crash. Recovery
                # above completed log rotation; only the causality receipt lags.
                pass
            else:
                if document.ydoc_snapshot_sha256 != intent.base_snapshot_sha256:
                    raise RuntimeError("document_change_base_conflict")
                if (
                    documents.current_ydoc_generation(store, intent.document_id)
                    != intent.base_generation_sha256
                ):
                    raise RuntimeError("document_change_generation_conflict")
                if result_snapshot is None or result_projection is None:
                    raise RuntimeError("materialized_change_requires_replay")
                if sha256_bytes(result_snapshot) != intent.result_snapshot_sha256:
                    raise RuntimeError("materialized_snapshot_hash_mismatch")
                if sha256_bytes(result_projection) != intent.result_projection_sha256:
                    raise RuntimeError("materialized_projection_hash_mismatch")
                store._store_blob_bytes(intent.result_projection_sha256, result_projection)
                replacement = ydoc_store.prepare_snapshot_replacement_locked(
                    store,
                    document_id=intent.document_id,
                    snapshot=result_snapshot,
                    expected_new_snapshot_sha256=intent.result_snapshot_sha256,
                    expected_current_snapshot_sha256=intent.base_snapshot_sha256,
                    expected_current_structured_head_sha256=intent.base_structured_head_sha256,
                    projection_sha256=intent.result_projection_sha256,
                )
                try:
                    documents.commit_document_version(
                        store,
                        document_id=intent.document_id,
                        kind="materialized",
                        projection_sha256=intent.result_projection_sha256,
                        ydoc_snapshot_sha256=replacement.snapshot_sha256,
                        structured_head_sha256=replacement.structured_head_sha256,
                        actor=Actor(kind="system", ref="document-kernel"),
                        detail=f"document-change:{intent.change_id}",
                    )
                except BaseException:
                    ydoc_store.abort_snapshot_replacement_locked(
                        store,
                        document_id=intent.document_id,
                        expected_snapshot_sha256=replacement.snapshot_sha256,
                    )
                    raise
                ydoc_store.finish_snapshot_replacement_locked(
                    store,
                    document_id=intent.document_id,
                    expected_snapshot_sha256=replacement.snapshot_sha256,
                )
        return causality.commit_change(
            intent.change_id,
            assurance={
                "exact_copied_text": "document_kernel_verified",
                "structured_mapping": "document_kernel_verified",
                "persistence": "persistence_verified",
                "journal_projection": "not_checked",
            },
        )


__all__ = [
    "BoundDocumentChange",
    "DomainContentStoreManager",
    "RunningNoteDocumentService",
]
