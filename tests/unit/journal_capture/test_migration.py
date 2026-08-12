from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.cowork_integration import apply_bound_direct_push
from work_buddy.document_kernel.redaction_dispatch import (
    CoworkDocumentSourceDispatcher,
)
from work_buddy.document_kernel.domain_service import DomainContentStoreManager
from work_buddy.document_kernel.protocol import sha256_bytes
from work_buddy.journal_capture.migration import (
    CALLSITE_INVENTORY_SHA256,
    JOURNAL_CONTENT_CALLSITES,
    JournalMigrationService,
)
from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalMigrationComparison,
    JournalMigrationState,
    JournalProjectionDiverged,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import (
    ActorRef,
    SourceOutbox,
    SourceRef,
    SourceStore,
    redact_source,
)
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth import documents, ydoc_store


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def _raw_vault_write(_rel, path: Path, content: str, **_kwargs) -> bool:
    path.write_bytes(content.encode("utf-8"))
    return True


@pytest.fixture(autouse=True)
def _offline_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_buddy.obsidian.bridge.is_available", lambda: False)
    monkeypatch.setattr(
        "work_buddy.obsidian.vault_writer.vault_write", _raw_vault_write
    )


def _service(tmp_path: Path, *, cutover_enabled: bool = True):
    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    journal = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    principal = ActorRef(
        sources.authority_id,
        "journal-migration-service",
        "service",
        "tenant-journal-migration",
    )
    stores = DomainContentStoreManager(
        root=tmp_path / "domain-content",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    service = JournalMigrationService(
        vault_root=vault,
        journal_store=journal,
        source_store=sources,
        principal=principal,
        cutover_enabled=cutover_enabled,
        stores=stores,
    )
    return vault, journal, sources, stores, service


def _write_day(vault: Path, day_id: str, value: bytes) -> Path:
    path = vault / "journal" / f"{day_id}.md"
    path.write_bytes(value)
    return path


def _day_bytes(*, running: bytes = b"legacy running note\r\n") -> bytes:
    return (
        b"\xef\xbb\xbf# **Log**\r\n"
        b"\r\n* 9:00 AM - Existing log entry. #wb/journal/log\r\n\r\n"
        b"# **Private / Unknown**\r\n"
        b"owner bytes: caf\xc3\xa9\r\n\r\n"
        b"# **Running Notes / Considerations**\r\n\r\n"
        + running
        + b"\r\n% RUNNING END\r\n"
    )


def test_log_shadow_cutover_and_rollback_preserve_unowned_bytes(
    tmp_path: Path,
) -> None:
    day_id = "2026-08-10"
    vault, journal, sources, stores, service = _service(tmp_path)
    path = _write_day(vault, day_id, _day_bytes(running=b""))
    unknown = b"# **Private / Unknown**\r\nowner bytes: caf\xc3\xa9\r\n\r\n"
    try:
        selected = service.select_log(day_id)
        shadow = service.shadow_import("logical_day_log", day_id)
        assert shadow.comparison_state is JournalMigrationComparison.PARITY
        assert shadow.byte_parity is False
        assert shadow.normalized_parity is False
        assert shadow.structural_parity is True
        source = sources.get_item(SourceRef.parse(shadow.source_ref or ""))
        assert source is not None and source.source_role == "imported_file"

        cutover = service.cutover(
            "logical_day_log",
            day_id,
            rollback_deadline="2099-01-01T00:00:00+00:00",
        )
        assert cutover.mirrored_state is JournalMigrationState.COWORK
        assert cutover.mirrored_authority_epoch == 1
        assert unknown in path.read_bytes()
        assert b"wb:cowork-projection/v1" in path.read_bytes()

        rolled_back = service.rollback("logical_day_log", day_id)
        assert rolled_back.mirrored_state is JournalMigrationState.LEGACY
        assert rolled_back.mirrored_authority_epoch == 2
        assert unknown in path.read_bytes()
        assert b"wb:cowork-projection/v1" not in path.read_bytes()
        assert b"wb:journal-entry/v1" not in path.read_bytes()
        assert journal.recoverable_migration_operations() == ()

        store = stores.ensure(vault)
        binding = DocumentCausalityStore(store.paths.sidecar).get_binding(
            selected.binding_id or cutover.binding_id or ""
        )
        assert binding is not None
        assert binding.content_authority == "domain"
        assert binding.content_authority_epoch == 2

        inventory = service.inventory()
        assert inventory["days"][0]["logicalDayLog"]["comparison"] == "parity"
        service.certify_exit()
        assert service.latest_exit_evidence() is not None
        path.write_bytes(
            path.read_bytes().replace(b"Existing log entry", b"External legacy edit")
        )
        assert service.inventory()["days"][0]["logicalDayLog"]["comparison"] == (
            "mismatch"
        )
        assert service.latest_exit_evidence() is None
    finally:
        service.close()


def test_log_direct_editor_push_projects_through_migration_mirror(
    tmp_path: Path,
) -> None:
    day_id = "2026-08-10"
    vault, journal, sources, stores, service = _service(tmp_path)
    path = _write_day(vault, day_id, _day_bytes(running=b""))
    try:
        service.shadow_import("logical_day_log", day_id)
        migrated = service.cutover(
            "logical_day_log",
            day_id,
            rollback_deadline="2099-01-01T00:00:00+00:00",
        )
        assert migrated.binding_id is not None and migrated.document_id is not None
        store = stores.ensure(vault)
        binding = DocumentCausalityStore(store.paths.sidecar).get_binding(
            migrated.binding_id
        )
        assert binding is not None
        document = documents.get_document(store, migrated.document_id)
        assert document.ydoc_snapshot_sha256 is not None
        snapshot = ydoc_store.read_snapshot(
            store, snapshot_sha256=document.ydoc_snapshot_sha256
        )
        updates, _ = ydoc_store.read_updates(store, document_id=document.id)
        base_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        kernel = DocumentKernelClient()
        try:
            candidate = kernel.request(
                {
                    "kind": "replace_text",
                    "snapshotBase64": snapshot,
                    "updatesBase64": updates,
                    "expectedBaseStructuredHeadSha256": base_head,
                    "selector": {
                        "kind": "prosemirror_text/v1",
                        "from": 3,
                        "to": 10,
                        "expectedText": "9:00 AM",
                    },
                    "copiedText": "10:00 AM",
                    "copiedTextSha256": sha256_bytes(b"10:00 AM"),
                },
                request_id="journal_log_direct_candidate_01",
            )
        finally:
            kernel.close()
        assert candidate.update is not None
        pushed = apply_bound_direct_push(
            store,
            document,
            update=candidate.update,
            expected_head=base_head,
            expected_generation=documents.current_ydoc_generation(store, document.id),
            actors={"input_by": "human:test"},
            input_assurance="direct_human_input",
            source_store=sources,
            source_principal=service.principal,
            journal_store=journal,
            vault_root=vault,
        )
        assert pushed is not None and pushed.projection is not None
        assert pushed.projection.status == "committed"
        assert "10:00 AM" in path.read_text(encoding="utf-8-sig")
        mirrored = journal.get_migration("logical_day_log", day_id)
        assert mirrored is not None
        assert mirrored.projection_state == "committed"
        assert mirrored.mirrored_authority_epoch == binding.content_authority_epoch
    finally:
        service.close()


@pytest.mark.parametrize("entity_kind", ["logical_day_log", "running_note"])
def test_migration_source_redaction_scrubs_managed_compatibility_projection(
    tmp_path: Path,
    entity_kind: str,
) -> None:
    day_id = "2026-08-18"
    vault, journal, sources, stores, service = _service(tmp_path)
    path = _write_day(vault, day_id, _day_bytes())
    try:
        if entity_kind == "logical_day_log":
            selected = service.select_log(day_id)
            entity_id = day_id
            readable = "Existing log entry"
        else:
            selected = service.assign_running_note(day_id, start_line=2, end_line=2)
            entity_id = selected.entity_id
            readable = "legacy running note"
        shadow = service.shadow_import(entity_kind, entity_id)
        service.cutover(
            entity_kind,
            entity_id,
            rollback_deadline="2099-01-01T00:00:00+00:00",
        )
        assert readable in path.read_text(encoding="utf-8-sig")
        source_ref = SourceRef.parse(shadow.source_ref or "")
        authorization = "e" * 64
        sources.grant_access(
            source_ref=source_ref,
            principal=service.principal,
            purpose="redaction",
            access_mode="metadata",
            authorization_fingerprint=authorization,
        )
        redaction = redact_source(
            sources,
            source_ref=source_ref,
            actor=service.principal,
            authorization_fingerprint=authorization,
            reason_code="user_requested",
        )
        assert len(redaction.pending_effect_ids) == 1
        summary = CoworkDocumentSourceDispatcher(
            sources,
            journal,
            service_principal=service.principal,
            registry=stores.registry,
            vault_root=vault,
        ).drain()
        assert summary.completed == 1
        rendered = path.read_text(encoding="utf-8-sig")
        assert readable not in rendered
        assert "wb:journal-entry-redacted/v1" in rendered
        assert f"id={selected.marker_id}" in rendered
        assert "wb:cowork-projection/v1" not in rendered
        retired = journal.get_migration(entity_kind, entity_id)
        assert retired is not None
        assert retired.mirrored_state is JournalMigrationState.RETIRED
        effect = SourceOutbox(sources).get(redaction.pending_effect_ids[0])
        assert effect is not None and effect.status == "succeeded"
    finally:
        service.close()


def test_cutover_and_rollback_reconcile_crashes_at_authority_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day_id = "2026-08-11"
    vault, journal, _sources, stores, service = _service(tmp_path)
    path = _write_day(vault, day_id, _day_bytes(running=b""))
    try:
        service.select_log(day_id)
        shadow = service.shadow_import("logical_day_log", day_id)
        original_project = service.projector.project

        def crash_after_epoch(*args, **kwargs):
            binding = kwargs["binding"]
            assert binding.content_authority == "co_work"
            raise RuntimeError("injected_after_epoch")

        monkeypatch.setattr(service.projector, "project", crash_after_epoch)
        with pytest.raises(RuntimeError, match="injected_after_epoch"):
            service.cutover(
                "logical_day_log",
                day_id,
                rollback_deadline="2099-01-01T00:00:00+00:00",
            )
        assert journal.recoverable_migration_operations()
        binding = DocumentCausalityStore(stores.ensure(vault).paths.sidecar).get_binding(
            shadow.binding_id or ""
        )
        assert binding is not None and binding.content_authority == "co_work"

        monkeypatch.setattr(service.projector, "project", original_project)
        recovered = service.reconcile("logical_day_log", day_id)
        assert recovered.mirrored_state is JournalMigrationState.COWORK
        assert journal.recoverable_migration_operations() == ()

        original_unwrap = service.adapter.unwrap_managed_selection

        def crash_after_rollback_epoch(**_kwargs):
            raise RuntimeError("injected_after_rollback_epoch")

        monkeypatch.setattr(
            service.adapter, "unwrap_managed_selection", crash_after_rollback_epoch
        )
        with pytest.raises(RuntimeError, match="injected_after_rollback_epoch"):
            service.rollback("logical_day_log", day_id)
        binding = DocumentCausalityStore(stores.ensure(vault).paths.sidecar).get_binding(
            shadow.binding_id or ""
        )
        assert binding is not None and binding.content_authority == "domain"
        assert journal.recoverable_migration_operations()

        monkeypatch.setattr(service.adapter, "unwrap_managed_selection", original_unwrap)
        recovered = service.reconcile("logical_day_log", day_id)
        assert recovered.mirrored_state is JournalMigrationState.LEGACY
        assert recovered.mirrored_authority_epoch == 2
        assert journal.recoverable_migration_operations() == ()
        assert b"wb:journal-entry/v1" not in path.read_bytes()
    finally:
        service.close()


def test_reconcile_captures_external_divergence_instead_of_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day_id = "2026-08-12"
    vault, journal, sources, _stores, service = _service(tmp_path)
    path = _write_day(vault, day_id, _day_bytes(running=b""))
    try:
        service.select_log(day_id)
        service.shadow_import("logical_day_log", day_id)
        original_project = service.projector.project
        monkeypatch.setattr(
            service.projector,
            "project",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected_after_epoch")
            ),
        )
        with pytest.raises(RuntimeError, match="injected_after_epoch"):
            service.cutover(
                "logical_day_log",
                day_id,
                rollback_deadline="2099-01-01T00:00:00+00:00",
            )
        changed = path.read_bytes().replace(b"Existing log entry", b"External edit")
        path.write_bytes(changed)
        monkeypatch.setattr(service.projector, "project", original_project)

        paused = service.reconcile("logical_day_log", day_id)
        assert paused.mirrored_state is JournalMigrationState.PAUSED_DIVERGED
        assert paused.divergence_source_ref is not None
        assert b"External edit" in path.read_bytes()
        divergence = sources.get_item(SourceRef.parse(paused.divergence_source_ref))
        assert divergence is not None and divergence.source_role == "imported_file"
        assert journal.recoverable_migration_operations()
    finally:
        service.close()


def test_pre_cutover_divergence_is_captured_and_reconcile_stays_paused(
    tmp_path: Path,
) -> None:
    day_id = "2026-08-15"
    vault, journal, sources, stores, service = _service(tmp_path)
    path = _write_day(vault, day_id, _day_bytes(running=b""))
    try:
        service.select_log(day_id)
        shadow = service.shadow_import("logical_day_log", day_id)
        changed = path.read_bytes().replace(b"Existing log entry", b"Externally changed")
        path.write_bytes(changed)

        with pytest.raises(JournalProjectionDiverged):
            service.cutover(
                "logical_day_log",
                day_id,
                rollback_deadline="2099-01-01T00:00:00+00:00",
            )
        paused = journal.get_migration("logical_day_log", day_id)
        assert paused is not None
        assert paused.mirrored_state is JournalMigrationState.PAUSED_DIVERGED
        assert paused.divergence_source_ref is not None
        divergence = sources.get_item(SourceRef.parse(paused.divergence_source_ref))
        assert divergence is not None

        binding = DocumentCausalityStore(stores.ensure(vault).paths.sidecar).get_binding(
            shadow.binding_id or ""
        )
        assert binding is not None
        assert binding.content_authority == "domain"
        assert binding.content_authority_epoch == 0
        before = path.read_bytes()
        assert service.reconcile("logical_day_log", day_id) == paused
        assert path.read_bytes() == before
    finally:
        service.close()


def test_authoritative_read_ignores_closed_rollout_gate_after_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day_id = "2026-08-17"
    vault, journal, _sources, stores, service = _service(tmp_path)
    path = _write_day(vault, day_id, _day_bytes(running=b""))
    try:
        service.select_log(day_id)
        service.shadow_import("logical_day_log", day_id)
        service.cutover(
            "logical_day_log",
            day_id,
            rollback_deadline="2099-01-01T00:00:00+00:00",
        )
        domain_store = stores.open_existing(vault)
        monkeypatch.setattr("work_buddy.paths.resolve", lambda _key: journal.path)
        monkeypatch.setattr(
            DomainContentStoreManager,
            "open_existing",
            lambda _self, _vault_root: domain_store,
        )
        path.write_bytes(
            path.read_bytes().replace(b"Existing log entry", b"External stale edit")
        )

        authoritative = service.adapter.read_day(day_id)
        assert "Existing log entry" in authoritative
        assert "External stale edit" not in authoritative
    finally:
        service.close()


def test_explicit_running_note_identity_and_exit_evidence_are_closed_by_facts(
    tmp_path: Path,
) -> None:
    day_id = "2026-08-13"
    vault, journal, _sources, _stores, service = _service(tmp_path)
    _write_day(vault, day_id, _day_bytes(running=b"same\r\nsame\r\n"))
    try:
        # Line one is the structural blank after the heading; the user-reviewed
        # passage begins on line two. Identity is assigned only from that exact
        # selection, never from a placeholder or line-derived durable ID.
        selected = service.assign_running_note(day_id, start_line=2, end_line=2)
        assert selected.entity_kind == "running_note"
        assert len(selected.entity_id) == 32
        assert service.assign_running_note(
            day_id, start_line=2, end_line=2
        ).entity_id == selected.entity_id
        duplicate_text = service.assign_running_note(
            day_id, start_line=3, end_line=3
        )
        assert duplicate_text.entity_id != selected.entity_id
        assert service.shadow_import(
            "running_note", selected.entity_id
        ).comparison_state is JournalMigrationComparison.PARITY
        inventory = service.inventory()
        assert inventory["days"][0]["unadmittedRunningNotes"] is True
        with pytest.raises(JournalCaptureConflict, match="unadmitted_running_notes"):
            service.certify_exit()
        assert journal.latest_exit_evidence() is None
        assert len(CALLSITE_INVENTORY_SHA256) == 64
        assert any(
            item.startswith("work_buddy/obsidian/vault_writer.py:")
            for item in JOURNAL_CONTENT_CALLSITES
        )
    finally:
        service.close()


def test_inventory_reports_malformed_day_instead_of_omitting_it(tmp_path: Path) -> None:
    day_id = "2026-08-16"
    vault, journal, _sources, _stores, service = _service(tmp_path)
    _write_day(vault, day_id, b"# Unknown\ncontent\n")
    try:
        inventory = service.inventory()
        assert [item["dayId"] for item in inventory["days"]] == [day_id]
        assert inventory["days"][0]["inventoryError"]
        assert inventory["days"][0]["unadmittedRunningNotes"] is True
        with pytest.raises(JournalCaptureConflict, match="unadmitted_running_notes"):
            service.certify_exit()
        assert journal.latest_exit_evidence() is None
    finally:
        service.close()


def test_carried_banner_is_preserved_structure_not_a_running_note(tmp_path: Path) -> None:
    day_id = "2026-08-18"
    banner = (
        b"***'Running Notes / Considerations' carried over from 2026-08-17***\r\n"
    )
    vault, _journal, _sources, _stores, service = _service(tmp_path)
    path = _write_day(vault, day_id, _day_bytes(running=banner))
    try:
        inventory = service.inventory()
        assert inventory["days"][0]["unadmittedRunningNotes"] is False
        before = path.read_bytes()
        service.select_log(day_id)
        service.shadow_import("logical_day_log", day_id)
        service.cutover(
            "logical_day_log",
            day_id,
            rollback_deadline="2099-01-01T00:00:00+00:00",
        )
        assert banner in path.read_bytes()
        assert before.split(b"# **Running Notes / Considerations**", 1)[1] == (
            path.read_bytes().split(b"# **Running Notes / Considerations**", 1)[1]
        )
    finally:
        service.close()


def test_exit_evidence_is_derived_after_settled_inventory(tmp_path: Path) -> None:
    day_id = "2026-08-14"
    vault, journal, _sources, _stores, service = _service(tmp_path)
    _write_day(vault, day_id, _day_bytes(running=b""))
    try:
        service.select_log(day_id)
        service.shadow_import("logical_day_log", day_id)
        service.cutover(
            "logical_day_log",
            day_id,
            rollback_deadline="2099-01-01T00:00:00+00:00",
        )
        result = service.certify_exit()
        evidence = journal.latest_exit_evidence()
        assert evidence is not None
        assert evidence == {
            "receipt_id": result["receiptId"],
            "inventory_sha256": result["inventorySha256"],
            "callsite_inventory_sha256": CALLSITE_INVENTORY_SHA256,
            "authority_summary": {
                "schema": "wb.journal-exit-evidence/v1",
                "days": 1,
                "entities": 1,
                "cutoverGate": "open",
            },
            "created_at": evidence["created_at"],
        }
        assert service.latest_exit_evidence() == evidence

        # An external edit inside an owned projection invalidates evidence
        # immediately from current file/cursor facts, before a worker mutates
        # the persisted mirror to paused_diverged.
        path = vault / "journal" / f"{day_id}.md"
        settled_bytes = path.read_bytes()
        path.write_bytes(
            settled_bytes.replace(b"Existing log entry", b"External owned edit")
        )
        assert service.latest_exit_evidence() is None
        with pytest.raises(JournalCaptureConflict, match="observed_divergence"):
            service.certify_exit()
        path.write_bytes(settled_bytes)
        assert service.latest_exit_evidence() == evidence

        path.write_bytes(
            settled_bytes.replace(
                b"# **Private / Unknown**",
                b"external log text\r\n# **Private / Unknown**",
            )
        )
        assert service.latest_exit_evidence() is None
        path.write_bytes(settled_bytes)
        assert service.latest_exit_evidence() == evidence

        # A newly observed unmarked Running Note changes the migration cohort,
        # so the persisted receipt remains historical but is no longer current.
        path.write_bytes(
            path.read_bytes().replace(
                b"% RUNNING END", b"manual unadmitted note\r\n% RUNNING END"
            )
        )
        assert journal.latest_exit_evidence() == evidence
        assert service.latest_exit_evidence() is None
    finally:
        service.close()
