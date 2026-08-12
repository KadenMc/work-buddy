"""Content-free operator surface for conservative task-note migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from work_buddy.document_kernel.domain_service import DomainContentStoreManager
from work_buddy.sources import ActorRef, SourceStore
from work_buddy.task_notes.adapter import validate_note_uuid
from work_buddy.task_notes.change_service import TaskNoteSourceChangeService
from work_buddy.task_notes.migration import TaskNoteShadowImporter
from work_buddy.task_notes.store import TaskNoteCutoverBlocked, TaskNoteMigrationStore


class TaskNoteMigrationOperator:
    """Run one-note migration steps; never moves task-master authority."""

    def __init__(
        self,
        *,
        vault_root: str | Path,
        migrations: TaskNoteMigrationStore,
        sources: SourceStore,
        principal: ActorRef,
        stores: DomainContentStoreManager | None = None,
        journal_exit_evidence_provider: (
            Callable[[], Mapping[str, Any] | None] | None
        ) = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.migrations = migrations
        self.sources = sources
        self.principal = principal
        self.stores = stores or DomainContentStoreManager()
        self.journal_exit_evidence_provider = journal_exit_evidence_provider

    def inventory(self) -> dict[str, Any]:
        journal_exit_evidence = (
            None
            if self.journal_exit_evidence_provider is None
            else self.journal_exit_evidence_provider()
        )
        disk_ids = {
            path.stem
            for path in (self.vault_root / "tasks" / "notes").glob("*.md")
            if path.is_file()
        }
        records = {item.note_uuid: item for item in self.migrations.list_task_notes()}
        note_ids = sorted(disk_ids | set(records))
        notes: list[dict[str, Any]] = []
        for note_uuid in note_ids:
            try:
                validate_note_uuid(note_uuid)
            except ValueError:
                continue
            record = records.get(note_uuid)
            authority = self.migrations.get_authority(
                "tasks", "task_note", note_uuid
            )
            notes.append(
                {
                    "noteUuid": note_uuid,
                    "legacyFilePresent": note_uuid in disk_ids,
                    "authority": (
                        "legacy_authoritative" if authority is None else authority.state.value
                    ),
                    "authorityEpoch": 0 if authority is None else authority.epoch,
                    "comparison": None if record is None else record.comparison_state.value,
                    "projection": None if record is None else record.projection_state.value,
                    "bound": bool(record and record.binding_id and record.document_id),
                }
            )
        return {
            "schema": "wb.task-note-migration-inventory/v1",
            "gates": self.migrations.status_summary()["gates"],
            "journalExitEvidenceCurrent": journal_exit_evidence is not None,
            "journalExitReceiptId": (
                None
                if journal_exit_evidence is None
                else journal_exit_evidence["receipt_id"]
            ),
            "notes": notes,
        }

    def shadow_import(self, note_uuid: str) -> dict[str, Any]:
        with self._importer() as importer:
            result = importer.shadow_import(validate_note_uuid(note_uuid))
        return {
            "schema": "wb.task-note-shadow-import-result/v1",
            "noteUuid": result.note_uuid,
            "sourceRef": result.source_ref,
            "bindingId": result.binding_id,
            "storeId": result.store_id,
            "documentId": result.document_id,
            "byteParity": result.byte_parity,
            "normalizedParity": result.normalized_parity,
        }

    def validate_parity(self, note_uuid: str) -> dict[str, Any]:
        # Re-capture is provider-idempotent for an unchanged native revision;
        # changed bytes become a new Source and update only the shadow record.
        result = self.shadow_import(note_uuid)
        return {
            "schema": "wb.task-note-parity-result/v1",
            "noteUuid": result["noteUuid"],
            "byteParity": result["byteParity"],
            "normalizedParity": result["normalizedParity"],
            "bindingId": result["bindingId"],
        }

    def set_gate(self, gate: str, enabled: bool) -> dict[str, Any]:
        if gate != "task_note_cutover_gate":
            raise ValueError("unsupported task-note migration gate")
        self.migrations.set_gate(gate, enabled)
        return {
            "schema": "wb.task-note-migration-gates/v1",
            "gates": self.migrations.status_summary()["gates"],
        }

    def cutover(self, note_uuid: str, *, rollback_deadline: str) -> dict[str, Any]:
        note_uuid = validate_note_uuid(note_uuid)
        record = self.migrations.get_task_note(note_uuid)
        if record is None or record.source_content_sha256 is None:
            raise ValueError("task-note shadow import is required before cutover")
        journal_exit_evidence = (
            None
            if self.journal_exit_evidence_provider is None
            else self.journal_exit_evidence_provider()
        )
        if journal_exit_evidence is None:
            raise TaskNoteCutoverBlocked(
                "current Journal exit evidence is required before task-note cutover"
            )
        with self._importer() as importer:
            epoch, binding = importer.cutover(
                note_uuid,
                domain_revision=record.source_content_sha256,
                rollback_deadline=rollback_deadline,
                journal_exit_evidence=journal_exit_evidence,
            )
        return {
            "schema": "wb.task-note-cutover-result/v1",
            "noteUuid": note_uuid,
            "authority": epoch.state.value,
            "authorityEpoch": epoch.epoch,
            "bindingId": binding.binding_id,
            "rollbackDeadline": epoch.rollback_deadline,
            "journalExitReceiptId": journal_exit_evidence["receipt_id"],
        }

    def rollback(self, note_uuid: str) -> dict[str, Any]:
        note_uuid = validate_note_uuid(note_uuid)
        with self._importer() as importer:
            epoch, binding = importer.rollback(
                note_uuid,
                # Retained as a compatibility argument; rollback derives the
                # authoritative revision from the projected document head.
                domain_revision="",
            )
        return {
            "schema": "wb.task-note-rollback-result/v1",
            "noteUuid": note_uuid,
            "authority": epoch.state.value,
            "authorityEpoch": epoch.epoch,
            "bindingId": binding.binding_id,
            "domainRevision": binding.domain_revision,
        }

    def recover(self, *, limit: int = 25) -> dict[str, Any]:
        reconciled: list[str] = []
        projections: list[dict[str, Any]] = []
        with self._importer() as importer:
            for record in self.migrations.list_task_notes()[:limit]:
                if importer.recover_authority(record.note_uuid) is not None:
                    reconciled.append(record.note_uuid)
                projection = importer.reconcile_projection(record.note_uuid)
                if projection is not None:
                    projections.append(
                        {
                            "noteUuid": record.note_uuid,
                            "state": projection.state.value,
                            "documentHeadSha256": projection.document_head_sha256,
                        }
                    )
        with TaskNoteSourceChangeService(
            vault_root=self.vault_root,
            migrations=self.migrations,
            sources=self.sources,
            principal=self.principal,
            stores=self.stores,
        ) as service:
            recovered = service.recover_all(limit=limit)
        return {
            "schema": "wb.task-note-change-recovery/v1",
            "authorityReconciled": reconciled,
            "projections": projections,
            "recovered": [
                {
                    "operationId": item.operation.operation_id,
                    "noteUuid": item.operation.note_uuid,
                    "state": item.operation.state.value,
                    "changeId": item.change.change_id,
                    "projection": item.projection.state.value,
                }
                for item in recovered
            ],
        }

    def _importer(self) -> TaskNoteShadowImporter:
        return TaskNoteShadowImporter(
            vault_root=self.vault_root,
            migration_store=self.migrations,
            source_store=self.sources,
            principal=self.principal,
            stores=self.stores,
        )


__all__ = ["TaskNoteMigrationOperator"]
