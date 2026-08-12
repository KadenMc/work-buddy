"""Forward Co-work→Journal projection with section CAS and divergence capture."""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from work_buddy.document_kernel.causality import (
    DocumentCausalityStore,
    DomainDocumentBinding,
    ProjectionCursor,
)
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.file_provider import WorkBuddyFileImportProvider
from work_buddy.document_kernel.protocol import sha256_bytes
from work_buddy.cowork.source_observation import (
    SourceObservationError,
    read_bounded_regular_file,
)
from work_buddy.sources import (
    ActorRef,
    OriginRef,
    ProviderRegistry,
    SourceStore,
    source_capture_from_origin,
)
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.store import TruthStore


_WRITE_LOCK = threading.RLock()
_MAX_JOURNAL_BYTES = 16 * 1024 * 1024


class JournalProjectionError(RuntimeError):
    code = "journal_document_projection_failed"
    retryable = True


class JournalProjectionDiverged(JournalProjectionError):
    code = "journal_document_projection_diverged"


@dataclass(frozen=True, slots=True)
class ManagedJournalSection:
    file_bytes: bytes
    file_sha256: str
    content: str
    body_start: int
    body_end: int
    body: str
    body_sha256: str


def _opening_pattern(entry_id: str) -> re.Pattern[str]:
    if re.fullmatch(r"[0-9a-f]{32}", entry_id) is None:
        raise JournalProjectionError("invalid_journal_entry_id")
    return re.compile(
        rf"^<!-- wb:journal-entry/v1 id={re.escape(entry_id)} "
        rf"content-sha256=[0-9a-f]{{64}} -->\r?\n",
        re.MULTILINE,
    )


def inspect_managed_section(path: Path, entry_id: str) -> ManagedJournalSection:
    try:
        observed = read_bounded_regular_file(
            path,
            maximum=_MAX_JOURNAL_BYTES,
            source_label="The Journal projection target",
            importer_id="journal-document-projection",
            retain_bytes=True,
        )
        assert observed.data is not None
        data = observed.data
    except SourceObservationError as exc:
        raise JournalProjectionError("journal_projection_target_unavailable") from exc
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalProjectionError("journal_projection_target_invalid_utf8") from exc
    opening = _opening_pattern(entry_id).search(content)
    closing_text = f"<!-- /wb:journal-entry/v1 id={entry_id} -->"
    if opening is None:
        raise JournalProjectionError("journal_projection_marker_missing")
    closing = content.find(closing_text, opening.end())
    if closing < 0 or content.find(closing_text, closing + 1) >= 0:
        raise JournalProjectionError("journal_projection_marker_ambiguous")
    body = content[opening.end() : closing]
    return ManagedJournalSection(
        file_bytes=data,
        file_sha256=sha256_bytes(data),
        content=content,
        body_start=opening.end(),
        body_end=closing,
        body=body,
        body_sha256=sha256_bytes(body.encode("utf-8")),
    )


class FileDivergenceCapture:
    """Capture the exact externally changed Journal file as a Source."""

    def __init__(
        self,
        *,
        source_store: SourceStore,
        vault_root: str | Path,
        principal: ActorRef,
    ) -> None:
        self.source_store = source_store
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.principal = principal
        self.provider = WorkBuddyFileImportProvider(
            self.vault_root,
            tenant_scope_id=principal.tenant_scope_id,
        )
        self.registry = ProviderRegistry()
        self.registry.register(self.provider)

    def __call__(self, path: Path, expected_digest: str) -> str:
        try:
            relative = path.resolve().relative_to(self.vault_root).as_posix()
        except ValueError as exc:
            raise JournalProjectionError("journal_projection_target_unavailable") from exc
        ref = source_capture_from_origin(
            self.source_store,
            self.registry,
            provider_id=self.provider.provider_id,
            origin_ref=OriginRef(
                provider_id=self.provider.provider_id,
                container_id=self.provider.container_id,
                native_item_id=relative,
                revision=expected_digest,
            ),
            principal=self.principal,
            purpose="document_projection_divergence",
            tenant_scope_id=self.principal.tenant_scope_id,
            originating_surface="journal_projection",
            expected_digest=expected_digest,
            namespace="journal-projection-divergence",
        )
        return ref.uri


class JournalProjectionAdapter:
    """Managed-section reader/writer; never overwrites an unexpected base."""

    def __init__(
        self,
        vault_root: str | Path,
        *,
        writer: Callable[[Path, bytes], None] | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self._writer = writer or self._write_vault

    def resolve(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise JournalProjectionError("journal_projection_target_unavailable")
        path = self.vault_root
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        for part in relative.parts:
            path = path / part
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise JournalProjectionError(
                    "journal_projection_target_unavailable"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & reparse
            ):
                raise JournalProjectionError("journal_projection_target_unavailable")
        path = path.resolve()
        try:
            path.relative_to(self.vault_root)
        except ValueError as exc:
            raise JournalProjectionError("journal_projection_target_unavailable") from exc
        return path

    def write_section(
        self,
        *,
        path: Path,
        entry_id: str,
        expected_body_sha256: str,
        replacement: str,
    ) -> tuple[str, str]:
        with _WRITE_LOCK:
            observed = inspect_managed_section(path, entry_id)
            target_sha = sha256_bytes(replacement.encode("utf-8"))
            if observed.body_sha256 == target_sha:
                return observed.file_sha256, observed.file_sha256
            if observed.body_sha256 != expected_body_sha256:
                raise JournalProjectionDiverged()
            updated = (
                observed.content[: observed.body_start]
                + replacement
                + observed.content[observed.body_end :]
            ).encode("utf-8")
            self._writer(path, updated)
            confirmed = inspect_managed_section(path, entry_id)
            if confirmed.body_sha256 != target_sha:
                raise JournalProjectionError("journal_projection_write_unverified")
            return observed.file_sha256, confirmed.file_sha256

    def _write_vault(self, path: Path, value: bytes) -> None:
        from work_buddy.obsidian.vault_writer import vault_write

        relative = path.relative_to(self.vault_root).as_posix()
        try:
            ok = vault_write(
                relative,
                path,
                value.decode("utf-8"),
                write_mode="replace",
                content_hint="wb:cowork-projection/v1",
                journal_owned_write=True,
            )
        except Exception as exc:
            raise JournalProjectionError("journal_projection_write_failed") from exc
        if not ok:
            raise JournalProjectionError("journal_projection_write_failed")


class JournalProjectionWorker:
    """Reconcile authoritative bound heads against durable Journal cursors."""

    def __init__(
        self,
        *,
        kernel: DocumentKernelClient,
        adapter: JournalProjectionAdapter,
        divergence_capture: Callable[[Path, str], str],
    ) -> None:
        self.kernel = kernel
        self.adapter = adapter
        self.divergence_capture = divergence_capture

    def project(
        self,
        store: TruthStore,
        *,
        binding: DomainDocumentBinding,
        entry_id: str,
        expected_initial_text: str | None = None,
    ) -> ProjectionCursor:
        if binding.content_authority != "co_work" or binding.lifecycle != "current":
            raise JournalProjectionError("journal_projection_not_authoritative")
        if binding.projection_path is None:
            raise JournalProjectionError("journal_projection_target_missing")
        causality = DocumentCausalityStore(store.paths.sidecar)
        document = documents.get_document(store, binding.document_id)
        if document.ydoc_snapshot_sha256 is None:
            raise JournalProjectionError("journal_projection_document_unavailable")
        snapshot = ydoc_store.read_snapshot(
            store, snapshot_sha256=document.ydoc_snapshot_sha256
        )
        updates, _ = ydoc_store.read_updates(store, document_id=document.id)
        head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        cursor = causality.projection_cursor(binding.binding_id)
        if cursor is not None and cursor.status == "paused_diverged":
            return cursor
        path = self.adapter.resolve(binding.projection_path)
        observed = inspect_managed_section(path, entry_id)
        if (
            cursor is not None
            and cursor.status == "committed"
            and cursor.document_head_sha256 == head
        ):
            if observed.body_sha256 != cursor.section_sha256:
                return self._pause(causality, binding, path, observed.file_sha256)
            return cursor
        outcome = self.kernel.request(
            {
                "kind": "project_markdown",
                "snapshotBase64": snapshot,
                "updatesBase64": updates,
                "expectedBaseStructuredHeadSha256": head,
            },
            request_id=f"projection_{binding.binding_id}_{head[:16]}",
        )
        projection = outcome.projection
        if projection is None:
            raise JournalProjectionError("journal_projection_kernel_result_missing")
        try:
            markdown = projection.decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - kernel invariant
            raise JournalProjectionError("journal_projection_kernel_result_invalid") from exc
        marker_open = (
            f"<!-- wb:cowork-projection/v1 binding={binding.binding_id} "
            f"epoch={binding.content_authority_epoch} head={head} -->"
        )
        marker_close = f"<!-- /wb:cowork-projection/v1 binding={binding.binding_id} -->"
        replacement = f"{marker_open}\n{markdown.rstrip()}\n{marker_close}\n"
        replacement_sha = sha256_bytes(replacement.encode("utf-8"))
        if cursor is None:
            raise JournalProjectionError("journal_projection_cursor_missing")
        if cursor.section_sha256 is None:
            if expected_initial_text is not None and (
                observed.body.rstrip("\r\n") != expected_initial_text.rstrip("\r\n")
            ):
                return self._pause(causality, binding, path, observed.file_sha256)
            cursor = causality.initialize_projection_base(
                binding.binding_id,
                content_authority_epoch=binding.content_authority_epoch,
                section_sha256=observed.body_sha256,
                file_sha256=observed.file_sha256,
            )
        if observed.body_sha256 == replacement_sha:
            # Ambiguous prior file write. Reconstruct the missing durable receipt.
            projection_id = causality.prepare_projection(
                binding_id=binding.binding_id,
                content_authority_epoch=binding.content_authority_epoch,
                document_head_sha256=head,
                expected_section_sha256=cursor.section_sha256,
                result_section_sha256=replacement_sha,
                result_projection_sha256=sha256_bytes(projection),
            )
            return causality.commit_projection(
                projection_id,
                base_file_sha256=cursor.file_sha256 or observed.file_sha256,
                result_file_sha256=observed.file_sha256,
                result_section_sha256=replacement_sha,
            )
        if observed.body_sha256 != cursor.section_sha256:
            return self._pause(causality, binding, path, observed.file_sha256)
        projection_id = causality.prepare_projection(
            binding_id=binding.binding_id,
            content_authority_epoch=binding.content_authority_epoch,
            document_head_sha256=head,
            expected_section_sha256=cursor.section_sha256,
            result_section_sha256=replacement_sha,
            result_projection_sha256=sha256_bytes(projection),
        )
        try:
            base_file, result_file = self.adapter.write_section(
                path=path,
                entry_id=entry_id,
                expected_body_sha256=cursor.section_sha256,
                replacement=replacement,
            )
        except JournalProjectionDiverged:
            fresh = inspect_managed_section(path, entry_id)
            return self._pause(causality, binding, path, fresh.file_sha256)
        except JournalProjectionError:
            # A write may have landed before its acknowledgement. Only the exact
            # target marker/body converts the ambiguity into success.
            fresh = inspect_managed_section(path, entry_id)
            if fresh.body_sha256 != replacement_sha:
                raise
            base_file = cursor.file_sha256 or fresh.file_sha256
            result_file = fresh.file_sha256
        return causality.commit_projection(
            projection_id,
            base_file_sha256=base_file,
            result_file_sha256=result_file,
            result_section_sha256=replacement_sha,
        )

    def _pause(
        self,
        causality: DocumentCausalityStore,
        binding: DomainDocumentBinding,
        path: Path,
        file_sha256: str,
    ) -> ProjectionCursor:
        source_ref = self.divergence_capture(path, file_sha256)
        return causality.pause_diverged(
            binding.binding_id,
            divergence_source_ref=source_ref,
        )

    def reconcile_store(
        self,
        store: TruthStore,
        *,
        entry_id_for_binding: Callable[[DomainDocumentBinding], str],
    ) -> tuple[ProjectionCursor, ...]:
        causality = DocumentCausalityStore(store.paths.sidecar)
        results: list[ProjectionCursor] = []
        for binding in causality.list_bindings(content_authority="co_work"):
            if binding.domain_namespace != "journal" or binding.domain_kind not in {
                "running_note",
                "logical_day_log",
            }:
                continue
            results.append(
                self.project(
                    store,
                    binding=binding,
                    entry_id=entry_id_for_binding(binding),
                )
            )
        return tuple(results)


__all__ = [
    "FileDivergenceCapture",
    "JournalProjectionAdapter",
    "JournalProjectionDiverged",
    "JournalProjectionError",
    "JournalProjectionWorker",
    "ManagedJournalSection",
    "inspect_managed_section",
]
