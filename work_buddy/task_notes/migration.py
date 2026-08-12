"""Exact shadow import and non-clobbering task-note projection services."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.domain_service import DomainContentStoreManager
from work_buddy.document_kernel.file_provider import WorkBuddyFileImportProvider
from work_buddy.document_kernel.protocol import sha256_bytes, structured_head_sha256
from work_buddy.sources import (
    ActorRef,
    OriginRef,
    ProviderRegistry,
    SourceStore,
    resolve_and_reserve_source,
    source_capture_from_origin,
)
from work_buddy.task_notes.adapter import (
    CoworkTaskNotePort,
    TaskNoteContentError,
    validate_note_uuid,
)
from work_buddy.task_notes.models import (
    AuthorityState,
    ProjectionOutcome,
    ProjectionState,
    SourceDependencyState,
)
from work_buddy.task_notes.store import TaskNoteMigrationStore
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor
from work_buddy.truth.store import TruthStore


_PROJECTION_MARKER = re.compile(
    r"^<!-- wb:cowork-task-note/v1 note-uuid=(?P<uuid>[A-Za-z0-9_-]+) "
    r"authority-epoch=(?P<epoch>\d+) generation=(?P<generation>\d+) "
    r"document-head=(?P<head>[0-9a-f]{64}) -->$",
    re.MULTILINE,
)
_REDACTED_PROJECTION_MARKER = (
    "<!-- wb:cowork-task-note-redacted/v1 note-uuid={note_uuid} "
    "redaction-event={redaction_event_id} -->\n"
)
_PROJECTION_WRITE_LOCK = threading.RLock()


class TaskNoteProjectionDiverged(TaskNoteContentError):
    code = "task_note_projection_diverged"


def normalized_markdown_sha256(value: bytes) -> str:
    """Explicit migration comparison policy.

    Byte parity is recorded independently.  Normalized parity strips a UTF-8
    BOM and normalizes newline encodings only; it does not trim whitespace,
    rewrite Markdown, or ignore a trailing newline.
    """

    text = value.decode("utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode("utf-8"))


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


def _title(content: bytes, fallback: str) -> str:
    text = content.decode("utf-8-sig")
    match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else fallback


@dataclass(frozen=True, slots=True)
class ShadowImportResult:
    note_uuid: str
    source_ref: str
    binding_id: str
    store_id: str
    document_id: str
    byte_parity: bool
    normalized_parity: bool


class TaskNoteShadowImporter:
    """Capture one legacy note exactly and bind an inert shadow document."""

    def __init__(
        self,
        *,
        vault_root: str | Path,
        migration_store: TaskNoteMigrationStore,
        source_store: SourceStore,
        principal: ActorRef,
        kernel: DocumentKernelClient | None = None,
        stores: DomainContentStoreManager | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.migrations = migration_store
        self.sources = source_store
        self.principal = principal
        self.kernel = kernel or DocumentKernelClient()
        self._owns_kernel = kernel is None
        self.stores = stores or DomainContentStoreManager()
        self.provider = WorkBuddyFileImportProvider(
            self.vault_root,
            tenant_scope_id=principal.tenant_scope_id,
        )
        self.providers = ProviderRegistry()
        self.providers.register(self.provider)

    def close(self) -> None:
        if self._owns_kernel:
            self.kernel.close()

    def __enter__(self) -> "TaskNoteShadowImporter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def shadow_import(self, note_uuid: str) -> ShadowImportResult:
        note_uuid = validate_note_uuid(note_uuid)
        relative = f"tasks/notes/{note_uuid}.md"
        source_ref = source_capture_from_origin(
            self.sources,
            self.providers,
            provider_id=self.provider.provider_id,
            origin_ref=OriginRef(
                provider_id=self.provider.provider_id,
                container_id=self.provider.container_id,
                native_item_id=relative,
                revision=None,
            ),
            principal=self.principal,
            purpose="file_import",
            tenant_scope_id=self.principal.tenant_scope_id,
            originating_surface="task_note_shadow_import",
            namespace="task-note-shadow-import",
        )
        item = self.sources.get_item(source_ref)
        if item is None:
            raise TaskNoteContentError("captured task-note Source is unavailable")
        representation = self.sources.get_representation(
            item.primary_representation_id
        )
        if representation is None:
            raise TaskNoteContentError("captured task-note representation is unavailable")
        digest = representation.content_sha256
        store = self.stores.ensure(self.vault_root)
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.binding_for_domain("tasks", "task_note", note_uuid, "task_note")
        consumer_id = hashlib.sha256(
            f"task-note-shadow:{note_uuid}".encode()
        ).hexdigest()[:32]
        reserved = resolve_and_reserve_source(
            self.sources,
            source_ref=source_ref,
            representation_id=item.primary_representation_id,
            principal=self.principal,
            purpose="file_import",
            consumer_domain="cowork_document",
            consumer_id=consumer_id,
            use_kind="exact_insertion",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole_document/v1"},
            expected_digest=digest,
        )
        exact = reserved.resolved.content
        if binding is None:
            document = self._bootstrap_document(
                store,
                note_uuid=note_uuid,
                content=exact,
                source_ref=source_ref.uri,
                source_digest=digest,
            )
            binding = causality.ensure_binding(
                domain_namespace="tasks",
                domain_kind="task_note",
                domain_entity_id=note_uuid,
                domain_revision=digest,
                store_id=store.store_id,
                document_id=document.id,
                role="task_note",
                created_by="service:task-note-shadow-import",
                projection_path=relative,
                migration_origin="task-note-shadow-import/v1",
            )
        else:
            document = documents.get_document(store, binding.document_id)

        if document.ydoc_snapshot_sha256 is None:
            raise TaskNoteContentError("bound task-note head is unavailable")
        document_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        self.migrations.record_source_dependency(
            usage_id=reserved.reservation.usage_id,
            note_uuid=note_uuid,
            consumer_id=consumer_id,
            relationship="shadow_import",
            source_ref=source_ref.uri,
            representation_id=item.primary_representation_id,
            content_sha256=digest,
            redaction_epoch=reserved.reservation.redaction_epoch,
            binding_id=binding.binding_id,
            store_id=store.store_id,
            document_id=document.id,
        )
        self.migrations.update_source_dependency(
            reserved.reservation.usage_id,
            result_document_head_sha256=document_head,
        )
        if reserved.reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reserved.reservation.usage_id)
        self.sources.acknowledge_usage(reserved.reservation.usage_id)
        self.migrations.update_source_dependency(
            reserved.reservation.usage_id,
            state=SourceDependencyState.ACKNOWLEDGED,
        )

        projection = store.resolve_blob_path(f"blobs/{document.content_sha256}").read_bytes()
        byte_parity = projection == exact
        normalized_parity = normalized_markdown_sha256(projection) == normalized_markdown_sha256(exact)
        self.migrations.record_shadow(
            note_uuid=note_uuid,
            source_ref=source_ref.uri,
            source_content_sha256=digest,
            legacy_file_sha256=digest,
            legacy_normalized_sha256=normalized_markdown_sha256(exact),
            document_projection_sha256=sha256_bytes(projection),
            document_normalized_sha256=normalized_markdown_sha256(projection),
            binding_id=binding.binding_id,
            store_id=store.store_id,
            document_id=document.id,
            byte_parity=byte_parity,
            normalized_parity=normalized_parity,
            domain_revision=digest,
        )
        return ShadowImportResult(
            note_uuid,
            source_ref.uri,
            binding.binding_id,
            store.store_id,
            document.id,
            byte_parity,
            normalized_parity,
        )

    def cutover(
        self,
        note_uuid: str,
        *,
        domain_revision: str,
        rollback_deadline: str | None = None,
        journal_exit_evidence: Mapping[str, Any] | None = None,
    ):
        """Advance both migration and document-binding authority idempotently."""

        note_uuid = validate_note_uuid(note_uuid)
        migration = self.migrations.get_task_note(note_uuid)
        if migration is None or migration.binding_id is None:
            raise TaskNoteContentError("task-note shadow binding is unavailable")
        store = self.stores.ensure(self.vault_root)
        causality = DocumentCausalityStore(store.paths.sidecar)
        mirrored = self.migrations.get_authority("tasks", "task_note", note_uuid)
        current_binding = causality.get_binding(migration.binding_id)
        if (
            mirrored is not None
            and mirrored.state is AuthorityState.COWORK
            and current_binding is not None
            and current_binding.content_authority == "co_work"
            and current_binding.content_authority_epoch == mirrored.epoch
            and current_binding.domain_revision == mirrored.domain_revision
        ):
            if mirrored.rollback_deadline != rollback_deadline:
                raise TaskNoteContentError(
                    "task-note cutover has a different rollback deadline"
                )
            return mirrored, current_binding
        path = self.vault_root / "tasks" / "notes" / f"{note_uuid}.md"
        if not path.is_file():
            raise TaskNoteContentError("task-note Markdown changed after parity review")
        current_revision = sha256_bytes(path.read_bytes())
        if (
            migration.legacy_file_sha256 != current_revision
            or migration.source_content_sha256 != domain_revision
            or current_revision != domain_revision
        ):
            raise TaskNoteContentError("task-note Markdown changed after parity review")
        current_epoch = self.migrations.validate_cutover(
            "tasks",
            "task_note",
            note_uuid,
            rollback_deadline=rollback_deadline,
            journal_exit_evidence=journal_exit_evidence,
        )
        if rollback_deadline is None:  # enforced by validate_cutover for tasks
            raise TaskNoteContentError("task-note rollback window is required")
        current_epoch = self.migrations.record_cutover_intent(
            "tasks",
            "task_note",
            note_uuid,
            expected_epoch=current_epoch.epoch,
            domain_revision=domain_revision,
            rollback_deadline=rollback_deadline,
        )
        binding = causality.cutover_to_cowork(
            migration.binding_id,
            domain_revision=domain_revision,
        )
        if binding.content_authority_epoch not in {
            current_epoch.epoch,
            current_epoch.epoch + 1,
        }:
            raise TaskNoteContentError("task-note authority epochs disagree")
        epoch = self.migrations.mirror_authority(
            "tasks",
            "task_note",
            note_uuid,
            state=AuthorityState.COWORK,
            epoch=binding.content_authority_epoch,
            domain_revision=binding.domain_revision,
            rollback_deadline=rollback_deadline,
        )
        return epoch, binding

    def rollback(
        self,
        note_uuid: str,
        *,
        domain_revision: str,
    ):
        """Restore legacy authority while fencing the prior projection epoch."""

        note_uuid = validate_note_uuid(note_uuid)
        migration = self.migrations.get_task_note(note_uuid)
        epoch = self.migrations.get_authority("tasks", "task_note", note_uuid)
        if migration is None or migration.binding_id is None or epoch is None:
            raise TaskNoteContentError("task-note shadow binding is unavailable")
        store = self.stores.ensure(self.vault_root)
        causality = DocumentCausalityStore(store.paths.sidecar)
        current_binding = causality.get_binding(migration.binding_id)
        if (
            epoch.state is AuthorityState.LEGACY
            and current_binding is not None
            and current_binding.content_authority == "domain"
            and current_binding.content_authority_epoch == epoch.epoch
        ):
            return epoch, current_binding
        validated = self.migrations.validate_rollback(
            "tasks", "task_note", note_uuid
        )
        worker = self._projection_worker()
        projected = worker.project(note_uuid)
        if projected.state is ProjectionState.PAUSED_DIVERGED:
            raise TaskNoteProjectionDiverged(
                "Resolve the external task-note edit before rollback."
            )
        if projected.document_head_sha256 is None:
            raise TaskNoteContentError("bound task-note head is unavailable")
        binding = causality.rollback_to_domain(
            migration.binding_id,
            domain_revision=projected.document_head_sha256,
            expected_epoch=validated.epoch,
        )
        worker.unwrap_after_rollback(
            note_uuid,
            expected_authority_epoch=validated.epoch,
            expected_document_head=projected.document_head_sha256,
        )
        rolled_back = self.migrations.mirror_authority(
            "tasks",
            "task_note",
            note_uuid,
            state=AuthorityState.LEGACY,
            epoch=binding.content_authority_epoch,
            domain_revision=projected.document_head_sha256,
            rollback_deadline=None,
        )
        if binding.content_authority_epoch != rolled_back.epoch:
            raise TaskNoteContentError("task-note rollback epochs disagree")
        return rolled_back, binding

    def recover_authority(self, note_uuid: str):
        """Repair the migration mirror from the canonical document binding."""

        note_uuid = validate_note_uuid(note_uuid)
        migration = self.migrations.get_task_note(note_uuid)
        epoch = self.migrations.get_authority("tasks", "task_note", note_uuid)
        if migration is None or migration.binding_id is None or epoch is None:
            return None
        store = self.stores.ensure(self.vault_root)
        binding = DocumentCausalityStore(store.paths.sidecar).get_binding(
            migration.binding_id
        )
        if binding is None or binding.content_authority_epoch <= epoch.epoch:
            return None
        if binding.content_authority_epoch != epoch.epoch + 1:
            raise TaskNoteContentError("task-note authority epochs disagree")
        state = (
            AuthorityState.COWORK
            if binding.content_authority == "co_work"
            else AuthorityState.LEGACY
        )
        if binding.domain_revision is None:
            raise TaskNoteContentError("task-note binding revision is unavailable")
        if state is AuthorityState.COWORK and epoch.rollback_deadline is None:
            raise TaskNoteContentError("task-note cutover recovery intent is unavailable")
        if state is AuthorityState.LEGACY:
            if epoch.state is not AuthorityState.COWORK:
                raise TaskNoteContentError("task-note rollback recovery intent is unavailable")
            self._projection_worker().unwrap_after_rollback(
                note_uuid,
                expected_authority_epoch=epoch.epoch,
                expected_document_head=binding.domain_revision,
            )
        return self.migrations.mirror_authority(
            "tasks",
            "task_note",
            note_uuid,
            state=state,
            epoch=binding.content_authority_epoch,
            domain_revision=binding.domain_revision,
            rollback_deadline=(epoch.rollback_deadline if state is AuthorityState.COWORK else None),
        )

    def reconcile_projection(self, note_uuid: str) -> ProjectionOutcome | None:
        """Replay missed projection work for one authoritative task note."""

        note_uuid = validate_note_uuid(note_uuid)
        authority = self.migrations.get_authority("tasks", "task_note", note_uuid)
        if authority is None or authority.state is not AuthorityState.COWORK:
            return None
        return self._projection_worker().project(note_uuid)

    def _projection_worker(self) -> "TaskNoteProjectionWorker":
        reader = BoundTaskNoteReader(
            vault_root=self.vault_root,
            migration_store=self.migrations,
            stores=self.stores,
        )
        return TaskNoteProjectionWorker(
            vault_root=self.vault_root,
            migrations=self.migrations,
            source_store=self.sources,
            principal=self.principal,
            reader=reader,
            kernel=self.kernel,
        )

    def _bootstrap_document(
        self,
        store: TruthStore,
        *,
        note_uuid: str,
        content: bytes,
        source_ref: str,
        source_digest: str,
    ):
        outcome = self.kernel.request(
            {
                "kind": "bootstrap_markdown",
                "sourceBase64": content,
                "sourceSha256": source_digest,
                "newlineStyle": _newline_style(content),
                "utf8Bom": content.startswith(b"\xef\xbb\xbf"),
                "trailingNewlineCount": _trailing_newlines(content),
            },
            request_id=f"task_note_shadow_{note_uuid}",
        )
        if outcome.snapshot is None or outcome.projection is None:
            raise TaskNoteContentError("document kernel returned no task-note result")
        snapshot_sha = ydoc_store.write_snapshot(
            store,
            snapshot=outcome.snapshot,
            expected_sha256=sha256_bytes(outcome.snapshot),
        )
        document_id = hashlib.sha256(
            f"task-note-document:{note_uuid}".encode()
        ).hexdigest()[:32]
        record, _version, _created = documents.register_ready_document(
            store,
            path=f"tasks/notes/{note_uuid}.md",
            title=_title(content, note_uuid),
            document_class="co_authored",
            projection_bytes=outcome.projection,
            ydoc_snapshot_sha256=snapshot_sha,
            structured_head_sha256=structured_head_sha256(outcome.snapshot),
            actor=Actor(kind="system", ref="task-note-shadow-import"),
            mode="import",
            document_meta={
                "domain_content": True,
                "source": {
                    "kind": "file_import",
                    "source_ref": source_ref,
                    "sha256": source_digest,
                    "writeback_policy": "never",
                    "authorship": "unknown",
                },
            },
            document_id=document_id,
            version_id=hashlib.sha256(
                f"task-note-version:{note_uuid}:{source_digest}".encode()
            ).hexdigest()[:32],
        )
        self.stores.registry.touch(store)
        return record


class BoundTaskNoteReader(CoworkTaskNotePort):
    """Read bound Co-work projections; writes stay intentionally gated."""

    def __init__(
        self,
        *,
        vault_root: str | Path,
        migration_store: TaskNoteMigrationStore,
        stores: DomainContentStoreManager | None = None,
        source_store: SourceStore | None = None,
        principal: ActorRef | None = None,
        bound_store: TruthStore | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.migrations = migration_store
        self.stores = stores or DomainContentStoreManager()
        self.source_store = source_store
        self.principal = principal
        self.bound_store = bound_store

    def _record(self, note_uuid: str):
        record = self.migrations.get_task_note(validate_note_uuid(note_uuid))
        if record is None or record.document_id is None:
            return None, None
        store = self.bound_store or self.stores.ensure(self.vault_root)
        if record.store_id is not None and store.store_id != record.store_id:
            raise TaskNoteContentError("bound task-note store is unavailable")
        document = documents.get_document(store, record.document_id)
        return store, document

    def read(self, note_uuid: str) -> str | None:
        store, document = self._record(note_uuid)
        if store is None or document is None:
            return None
        try:
            return store.resolve_blob_path(f"blobs/{document.content_sha256}").read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeError) as exc:
            raise TaskNoteContentError("bound task-note projection is unavailable") from exc

    def modified_at(self, note_uuid: str) -> float:
        store, _document = self._record(note_uuid)
        if store is None:
            return 0.0
        return store.paths.db.stat().st_mtime

    def replace(self, note_uuid: str, content: str, *, idempotency_key: str) -> None:
        from work_buddy.paths import resolve
        from work_buddy.task_notes.change_service import TaskNoteSourceChangeService

        sources = self.source_store or SourceStore.create(resolve("stores/sources"))
        principal = self.principal
        if principal is None:
            from work_buddy.dashboard import local_identity_api

            enrolled = local_identity_api._authority().enrolled_actor()
            principal = ActorRef(
                issuer_authority_id=enrolled.issuer_authority_id,
                subject="work-buddy-task-note-service",
                kind="service",
                tenant_scope_id=enrolled.tenant_scope_id,
            )
        with TaskNoteSourceChangeService(
            vault_root=self.vault_root,
            migrations=self.migrations,
            sources=sources,
            principal=principal,
            stores=self.stores,
        ) as service:
            service.replace(note_uuid, content, idempotency_key=idempotency_key)

    def retire(self, note_uuid: str, *, idempotency_key: str) -> None:
        record = self.migrations.get_task_note(validate_note_uuid(note_uuid))
        if record is None or record.binding_id is None:
            return
        store = self.stores.ensure(self.vault_root)
        DocumentCausalityStore(store.paths.sidecar).retire_binding(record.binding_id)


class TaskNoteProjectionWorker:
    """Materialize an authoritative Co-work head without clobbering edits."""

    def __init__(
        self,
        *,
        vault_root: str | Path,
        migrations: TaskNoteMigrationStore,
        source_store: SourceStore,
        principal: ActorRef,
        reader: BoundTaskNoteReader | None = None,
        kernel: DocumentKernelClient | None = None,
        writer: Callable[[Path, bytes, str], None] | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.migrations = migrations
        self.sources = source_store
        self.principal = principal
        self.reader = reader or BoundTaskNoteReader(
            vault_root=self.vault_root,
            migration_store=migrations,
        )
        if kernel is None:
            from work_buddy.document_kernel.runtime_service import (
                shared_document_kernel,
            )

            kernel = shared_document_kernel()
        self.kernel = kernel
        self.writer = writer or self._atomic_write_if_unchanged
        self.provider = WorkBuddyFileImportProvider(
            self.vault_root,
            tenant_scope_id=principal.tenant_scope_id,
        )
        self.providers = ProviderRegistry()
        self.providers.register(self.provider)

    def project(self, note_uuid: str) -> ProjectionOutcome:
        note_uuid = validate_note_uuid(note_uuid)
        authority = self.migrations.get_authority("tasks", "task_note", note_uuid)
        if authority is None or authority.state is not AuthorityState.COWORK:
            raise TaskNoteContentError("task note is not Co-work authoritative")
        record = self.migrations.get_task_note(note_uuid)
        if record is None or record.document_id is None:
            raise TaskNoteContentError("task-note binding is unavailable")
        content, store, document, head = self._current_projection(note_uuid)
        binding = DocumentCausalityStore(store.paths.sidecar).get_binding(record.binding_id)  # type: ignore[arg-type]
        if (
            binding is None
            or binding.lifecycle != "current"
            or binding.content_authority != "co_work"
            or binding.content_authority_epoch != authority.epoch
        ):
            raise TaskNoteContentError("task-note binding authority is not committed")
        if (
            record.projection_state is ProjectionState.CURRENT
            and record.projection_document_head == head
        ):
            path = self.vault_root / f"tasks/notes/{note_uuid}.md"
            try:
                current_sha = sha256_bytes(path.read_bytes())
            except OSError as exc:
                raise TaskNoteContentError(
                    "task-note projection target is unavailable"
                ) from exc
            if current_sha == record.projection_result_sha256:
                return ProjectionOutcome(
                    note_uuid,
                    ProjectionState.CURRENT,
                    record.projection_generation,
                    record.projection_result_sha256,
                    head,
                )
            source_ref = self._capture_divergence(note_uuid, current_sha)
            paused = self.migrations.pause_diverged(note_uuid, source_ref=source_ref)
            return ProjectionOutcome(
                note_uuid,
                paused.projection_state,
                paused.projection_generation,
                current_sha,
                head,
                source_ref,
            )
        generation = record.projection_generation + 1
        marker = (
            "<!-- wb:cowork-task-note/v1 "
            f"note-uuid={note_uuid} authority-epoch={authority.epoch} "
            f"generation={generation} document-head={head} -->"
        )
        projected = content.rstrip("\r\n") + "\n\n" + marker + "\n"
        desired = projected.encode("utf-8")
        desired_sha = sha256_bytes(desired)
        path = self.vault_root / f"tasks/notes/{note_uuid}.md"
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise TaskNoteContentError("task-note projection target is unavailable") from exc
        current_sha = sha256_bytes(current)
        expected = record.projection_result_sha256 or record.legacy_file_sha256
        if current_sha == desired_sha:
            updated = self.migrations.record_projection(
                note_uuid,
                base_sha256=expected or current_sha,
                result_sha256=desired_sha,
                generation=generation,
                document_head=head,
            )
            return ProjectionOutcome(
                note_uuid, updated.projection_state, generation, desired_sha, head
            )
        if expected is None or current_sha != expected:
            source_ref = self._capture_divergence(note_uuid, current_sha)
            paused = self.migrations.pause_diverged(note_uuid, source_ref=source_ref)
            return ProjectionOutcome(
                note_uuid,
                paused.projection_state,
                paused.projection_generation,
                current_sha,
                head,
                source_ref,
            )
        try:
            self.writer(path, desired, current_sha)
        except TaskNoteProjectionDiverged:
            try:
                fresh_sha = sha256_bytes(path.read_bytes())
            except OSError as exc:
                raise TaskNoteContentError(
                    "task-note projection target is unavailable"
                ) from exc
            source_ref = self._capture_divergence(note_uuid, fresh_sha)
            paused = self.migrations.pause_diverged(note_uuid, source_ref=source_ref)
            return ProjectionOutcome(
                note_uuid,
                paused.projection_state,
                paused.projection_generation,
                fresh_sha,
                head,
                source_ref,
            )
        try:
            confirmed = path.read_bytes()
        except OSError as exc:
            raise TaskNoteContentError("task-note projection could not be verified") from exc
        try:
            marker_match = _PROJECTION_MARKER.search(confirmed.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise TaskNoteContentError(
                "task-note projection could not be verified"
            ) from exc
        if (
            sha256_bytes(confirmed) != desired_sha
            or marker_match is None
            or marker_match.group("uuid") != note_uuid
            or int(marker_match.group("epoch")) != authority.epoch
            or int(marker_match.group("generation")) != generation
            or marker_match.group("head") != head
        ):
            raise TaskNoteContentError("task-note projection could not be verified")
        updated = self.migrations.record_projection(
            note_uuid,
            base_sha256=current_sha,
            result_sha256=desired_sha,
            generation=generation,
            document_head=head,
        )
        return ProjectionOutcome(
            note_uuid, updated.projection_state, generation, desired_sha, head
        )

    def unwrap_after_rollback(
        self,
        note_uuid: str,
        *,
        expected_authority_epoch: int,
        expected_document_head: str,
    ) -> str:
        """Remove only the exact fenced compatibility projection.

        Canonical authority must already have moved back to the domain before
        this write, which fences the prior projection epoch.  The operation is
        restart-safe: a file already equal to the current document projection
        is accepted as an ambiguous successful unwrap.
        """

        note_uuid = validate_note_uuid(note_uuid)
        record = self.migrations.get_task_note(note_uuid)
        if record is None:
            raise TaskNoteContentError("task-note binding is unavailable")
        content, _store, _document, current_head = self._current_projection(note_uuid)
        if current_head != expected_document_head:
            raise TaskNoteContentError("bound task-note head changed during rollback")
        desired = content.encode("utf-8")
        desired_sha = sha256_bytes(desired)
        path = self.vault_root / f"tasks/notes/{note_uuid}.md"
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise TaskNoteContentError("task-note projection target is unavailable") from exc
        current_sha = sha256_bytes(current)
        if current_sha == desired_sha:
            self.migrations.reset_projection_after_rollback(note_uuid)
            return desired_sha
        if (
            record.projection_state is not ProjectionState.CURRENT
            or record.projection_document_head != expected_document_head
            or record.projection_result_sha256 != current_sha
        ):
            source_ref = self._capture_divergence(note_uuid, current_sha)
            self.migrations.pause_diverged(note_uuid, source_ref=source_ref)
            raise TaskNoteProjectionDiverged(
                "task-note projection changed during rollback"
            )
        marker = (
            "<!-- wb:cowork-task-note/v1 "
            f"note-uuid={note_uuid} authority-epoch={expected_authority_epoch} "
            f"generation={record.projection_generation} "
            f"document-head={expected_document_head} -->"
        )
        expected = (content.rstrip("\r\n") + "\n\n" + marker + "\n").encode("utf-8")
        if current != expected:
            source_ref = self._capture_divergence(note_uuid, current_sha)
            self.migrations.pause_diverged(note_uuid, source_ref=source_ref)
            raise TaskNoteProjectionDiverged(
                "task-note projection changed during rollback"
            )
        self.writer(path, desired, current_sha)
        try:
            confirmed_sha = sha256_bytes(path.read_bytes())
        except OSError as exc:
            raise TaskNoteContentError("task-note rollback could not be verified") from exc
        if confirmed_sha != desired_sha:
            raise TaskNoteContentError("task-note rollback could not be verified")
        self.migrations.reset_projection_after_rollback(note_uuid)
        return desired_sha

    def redact_compatibility_copy(
        self,
        note_uuid: str,
        *,
        redaction_event_id: str,
    ) -> str:
        """Replace the exact managed task-note file with a content-free marker."""

        note_uuid = validate_note_uuid(note_uuid)
        record = self.migrations.get_task_note(note_uuid)
        if record is None:
            raise TaskNoteContentError("task-note binding is unavailable")
        marker = _REDACTED_PROJECTION_MARKER.format(
            note_uuid=note_uuid,
            redaction_event_id=redaction_event_id,
        ).encode("utf-8")
        marker_sha = sha256_bytes(marker)
        path = self.vault_root / f"tasks/notes/{note_uuid}.md"
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise TaskNoteContentError("task-note projection target is unavailable") from exc
        current_sha = sha256_bytes(current)
        if current_sha == marker_sha:
            return marker_sha
        allowed = {
            value
            for value in (record.projection_result_sha256, record.legacy_file_sha256)
            if value is not None
        }
        if current_sha not in allowed:
            raise TaskNoteProjectionDiverged(
                "task-note compatibility copy changed before source redaction"
            )
        self.writer(path, marker, current_sha)
        try:
            confirmed_sha = sha256_bytes(path.read_bytes())
        except OSError as exc:
            raise TaskNoteContentError(
                "task-note source redaction could not be verified"
            ) from exc
        if confirmed_sha != marker_sha:
            raise TaskNoteContentError(
                "task-note source redaction could not be verified"
            )
        return marker_sha

    def _current_projection(
        self, note_uuid: str
    ) -> tuple[str, TruthStore, Any, str]:
        """Render the current structured head, including direct Yjs updates."""

        store, document = self.reader._record(note_uuid)
        if store is None or document is None or document.ydoc_snapshot_sha256 is None:
            raise TaskNoteContentError("bound task-note head is unavailable")
        snapshot = ydoc_store.read_snapshot(
            store, snapshot_sha256=document.ydoc_snapshot_sha256
        )
        updates, _ = ydoc_store.read_updates(store, document_id=document.id)
        head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        outcome = self.kernel.request(
            {
                "kind": "project_markdown",
                "snapshotBase64": snapshot,
                "updatesBase64": updates,
                "expectedBaseStructuredHeadSha256": head,
            },
            request_id=f"task_note_projection_{note_uuid}_{head[:16]}",
        )
        if outcome.projection is None:
            raise TaskNoteContentError("document kernel returned no task-note projection")
        try:
            content = outcome.projection.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TaskNoteContentError("task-note projection is not UTF-8") from exc
        return content, store, document, head

    def _capture_divergence(self, note_uuid: str, digest: str) -> str:
        relative = f"tasks/notes/{note_uuid}.md"
        source_ref = source_capture_from_origin(
            self.sources,
            self.providers,
            provider_id=self.provider.provider_id,
            origin_ref=OriginRef(
                provider_id=self.provider.provider_id,
                container_id=self.provider.container_id,
                native_item_id=relative,
                revision=digest,
            ),
            principal=self.principal,
            purpose="document_projection_divergence",
            tenant_scope_id=self.principal.tenant_scope_id,
            originating_surface="task_note_projection",
            expected_revision=digest,
            expected_digest=digest,
            namespace="task-note-projection-divergence",
        )
        return source_ref.uri

    @staticmethod
    def _atomic_write_if_unchanged(path: Path, value: bytes, expected_sha256: str) -> None:
        with _PROJECTION_WRITE_LOCK:
            try:
                current = path.read_bytes()
            except OSError as exc:
                raise TaskNoteContentError(
                    "task-note projection target is unavailable"
                ) from exc
            if sha256_bytes(current) != expected_sha256:
                raise TaskNoteProjectionDiverged(
                    "task-note projection base changed before replacement"
                )
            tmp = path.with_suffix(".md.wbtmp")
            try:
                tmp.write_bytes(value)
                os.replace(tmp, path)
            except OSError as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise TaskNoteContentError("task-note projection write failed") from exc


__all__ = [
    "BoundTaskNoteReader",
    "ShadowImportResult",
    "TaskNoteProjectionWorker",
    "TaskNoteProjectionDiverged",
    "TaskNoteShadowImporter",
    "normalized_markdown_sha256",
]
