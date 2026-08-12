from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.domain_service import (
    DomainContentStoreManager,
    RunningNoteDocumentService,
)
from work_buddy.document_kernel.direct_edit import DirectDocumentEditService
from work_buddy.document_kernel.cowork_integration import (
    reconcile_document_source_dependency,
)
from work_buddy.document_kernel.redaction_dispatch import (
    CoworkDocumentSourceDispatcher,
)
from work_buddy.document_kernel.file_provider import WorkBuddyFileImportProvider
from work_buddy.document_kernel.journal_projection import (
    FileDivergenceCapture,
    JournalProjectionAdapter,
    JournalProjectionWorker,
)
from work_buddy.document_kernel.pilot import RunningNotePilotService
from work_buddy.document_kernel.protocol import sha256_bytes
from work_buddy.journal_capture.models import CaptureMode, CaptureTarget
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import (
    ActorRef,
    OutboxEffect,
    SourceOutbox,
    SourceStore,
    redact_source,
    resolve_and_reserve_source,
)
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth import documents as truth_documents, ydoc_store


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def _source(tmp_path: Path, text: str):
    store = SourceStore.create(tmp_path / "sources")
    tenant = "tenant-00000001"
    principal = ActorRef(store.authority_id, "journal-service", "service", tenant)
    item = store.capture_source(
        content=text,
        source_role="human_input",
        tenant_scope_id=tenant,
        originating_surface="journal",
    )
    store.grant_access(
        source_ref=item.source_ref,
        principal=principal,
        purpose="journal.materialize",
        access_mode="content",
        authorization_fingerprint="a" * 64,
        content_boundary={
            "representation_id": item.primary_representation_id,
            "max_bytes": 4096,
        },
    )
    reserved = resolve_and_reserve_source(
        store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        principal=principal,
        purpose="journal.materialize",
        consumer_domain="cowork_document",
        consumer_id="1" * 32,
        use_kind="exact_insertion",
        disclosure_kind="exact_readable_copy",
        redaction_policy="scrub",
        expected_digest=hashlib.sha256(text.encode()).hexdigest(),
    )
    return store, principal, reserved


def _daily_note(vault: Path, *, entry_id: str, text: str, day_id: str) -> Path:
    path = vault / "journal" / f"{day_id}.md"
    path.parent.mkdir(parents=True)
    digest = hashlib.sha256(text.encode()).hexdigest()
    path.write_text(
        "# Running Notes / Considerations\n"
        f"<!-- wb:journal-entry/v1 id={entry_id} content-sha256={digest} -->\n"
        f"{text.rstrip()}\n"
        f"<!-- /wb:journal-entry/v1 id={entry_id} -->\n"
        "% RUNNING END\n",
        encoding="utf-8",
    )
    return path


def _services(
    tmp_path: Path,
    vault: Path,
    source_store: SourceStore,
    principal: ActorRef,
):
    kernel = DocumentKernelClient()
    manager = DomainContentStoreManager(
        root=tmp_path / "domain-content",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    documents = RunningNoteDocumentService(kernel=kernel, stores=manager)
    adapter = JournalProjectionAdapter(vault, writer=lambda path, data: path.write_bytes(data))
    divergence = FileDivergenceCapture(
        source_store=source_store,
        vault_root=vault,
        principal=principal,
    )
    projections = JournalProjectionWorker(
        kernel=kernel,
        adapter=adapter,
        divergence_capture=divergence,
    )
    return kernel, documents, projections


def test_running_note_capture_materializes_binds_projects_and_inspects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "A source-backed Running Note with café 🧭.\n"
    day_id = "2026-08-09"
    vault = tmp_path / "vault"
    sources, principal, reserved = _source(tmp_path, text)
    journal = JournalCaptureStore(tmp_path / "journal.db")
    capture = journal.create_capture(
        client_mutation_id="pilot-journal-capture",
        request_sha256=hashlib.sha256(text.encode()).hexdigest(),
        source_ref=reserved.resolved.source_ref.uri,
        representation_id=reserved.resolved.representation.representation_id,
        submission_id="pilot-submission",
        command_id="pilot-command",
        source_effect_id="pilot-effect",
        source_usage_id=None,
        day_id=day_id,
        requested_target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB,
        input_mode="direct_entry",
        stated_at=None,
        submitted_at="2026-08-09T21:00:00+00:00",
        authorization_fingerprint="a" * 64,
    )
    entry = journal.ensure_entry(
        capture_id=capture.capture_id,
        entry_kind=CaptureTarget.RUNNING_NOTES,
        markdown=text,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        projection_marker="pilot-projection-marker",
        created_at=capture.submitted_at,
    )
    entry_id = entry.entry_id
    journal_path = _daily_note(vault, entry_id=entry_id, text=text, day_id=day_id)
    kernel, documents, projections = _services(tmp_path, vault, sources, principal)
    try:
        result = RunningNotePilotService(
            documents=documents,
            projections=projections,
        ).execute(
            vault_root=vault,
            entry_id=entry_id,
            day_id=day_id,
            domain_revision="journal-entry-v1",
            source_store=sources,
            reserved_source=reserved,
            actors={
                "selected_by": "human:profile-00000001",
                "applied_by": "service:document-kernel",
                "reviewed_by": None,
            },
            idempotency_key="running-note-pilot-00000001",
            expected_initial_text=text,
        )
        assert result.document.binding.content_authority == "co_work"
        assert result.document.binding.content_authority_epoch == 1
        assert result.document.change.source_ref == reserved.resolved.source_ref.uri
        assert result.document.change.exact_copied_text_sha256 == hashlib.sha256(
            text.encode()
        ).hexdigest()
        assert result.projection.status == "committed"
        rendered = journal_path.read_text(encoding="utf-8")
        assert "wb:cowork-projection/v1" in rendered
        assert text.rstrip() in rendered
        inspection = result.inspection()
        assert inspection["coworkHref"].startswith("/app/cowork?")
        assert inspection["change"]["assurance"]["exact_copied_text"] == (  # type: ignore[index]
            "document_kernel_verified"
        )
        journal.record_document_binding(
            entry_id=entry_id,
            binding_id=result.document.binding.binding_id,
            store_id=result.document.binding.store_id,
            document_id=result.document.binding.document_id,
            change_id=result.document.change.change_id,
            source_consumer_id="1" * 32,
            source_usage_id=reserved.reservation.usage_id,
            cowork_href=result.cowork_href,
            content_authority_epoch=result.document.binding.content_authority_epoch,
            entry_version=entry.version,
            inspection=inspection,
        )

        # A normal browser update is validated/recorded at the durable sitting
        # boundary, then the missed-event-safe projection sweep follows its head.
        document = truth_documents.get_document(
            result.document.store,
            result.document.binding.document_id,
        )
        assert document.ydoc_snapshot_sha256 is not None
        snapshot = ydoc_store.read_snapshot(
            result.document.store,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        updates, _ = ydoc_store.read_updates(
            result.document.store,
            document_id=document.id,
        )
        base_head = ydoc_store.current_structured_head(
            result.document.store,
            document_id=document.id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        copied = "An updated"
        candidate = kernel.request(
            {
                "kind": "replace_text",
                "snapshotBase64": snapshot,
                "updatesBase64": updates,
                "expectedBaseStructuredHeadSha256": base_head,
                "selector": {
                    "kind": "prosemirror_text/v1",
                    "from": 1,
                    "to": 2,
                    "expectedText": "A",
                },
                "copiedText": copied,
                "copiedTextSha256": sha256_bytes(copied.encode()),
            },
            request_id="pilot_direct_candidate_01",
        )
        assert candidate.update is not None
        generation = truth_documents.current_ydoc_generation(
            result.document.store,
            document.id,
        )
        direct = DirectDocumentEditService(kernel=kernel).apply(
            result.document.store,
            document_id=document.id,
            update=candidate.update,
            expected_base_structured_head_sha256=base_head,
            expected_base_generation_sha256=generation,
            actors={"input_by": "human:profile-00000001"},
            idempotency_key="pilot-direct-edit-00000001",
            binding=result.document.binding,
        )
        assert direct.change.operation_kind == "direct_editor_update"
        assert DirectDocumentEditService(kernel=kernel).apply(
            result.document.store,
            document_id=document.id,
            update=candidate.update,
            expected_base_structured_head_sha256=base_head,
            expected_base_generation_sha256=generation,
            actors={"input_by": "human:profile-00000001"},
            idempotency_key="pilot-direct-edit-00000001",
            binding=result.document.binding,
        ) == direct

        # Once ordinary prose is committed, the exact-copy dependency becomes
        # a separately registered semantic derivative.  The old usage is not
        # released until the acknowledged replacement and Journal mirror are
        # both durable; a crash after release is recoverable from the receipt.
        original_release = sources.release_usage_if_source_active

        def checked_release(usage_id: str):
            active = journal.get_document_binding(entry_id)
            assert active is not None
            assert active.source_use_kind == "mixed_derivative"
            with sources.connect() as conn:
                next_usage = conn.execute(
                    "SELECT status FROM source_usage_intents WHERE usage_id=?",
                    (active.source_usage_id,),
                ).fetchone()
            assert next_usage is not None and next_usage["status"] == "acknowledged"
            return original_release(usage_id)

        monkeypatch.setattr(sources, "release_usage_if_source_active", checked_release)
        original_complete = journal.complete_document_source_usage_transition
        crashed = False

        def crash_after_release(transition_id: str):
            nonlocal crashed
            if not crashed:
                crashed = True
                raise RuntimeError("injected_crash_after_usage_release")
            return original_complete(transition_id)

        monkeypatch.setattr(
            journal,
            "complete_document_source_usage_transition",
            crash_after_release,
        )
        with pytest.raises(RuntimeError, match="injected_crash_after_usage_release"):
            reconcile_document_source_dependency(
                result.document.store,
                binding=result.document.binding,
                source_store=sources,
                source_principal=principal,
                journal_store=journal,
            )
        transitioned = reconcile_document_source_dependency(
            result.document.store,
            binding=result.document.binding,
            source_store=sources,
            source_principal=principal,
            journal_store=journal,
        )
        assert transitioned is not None
        assert transitioned.source_use_kind == "mixed_derivative"
        assert transitioned.source_disclosure_kind == "semantic_derivative"
        assert transitioned.source_redaction_policy == "review"
        receipt = journal.get_document_source_usage_transition(entry_id)
        assert receipt is not None and receipt.state == "complete"
        with sources.connect() as conn:
            rows = conn.execute(
                "SELECT use_kind,disclosure_kind,redaction_policy,status "
                "FROM source_usage_intents WHERE consumer_domain='cowork_document' "
                "AND consumer_id=? ORDER BY use_kind",
                ("1" * 32,),
            ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("exact_insertion", "exact_readable_copy", "scrub", "released"),
            ("mixed_derivative", "semantic_derivative", "review", "acknowledged"),
        ]
        projected_direct = projections.project(
            result.document.store,
            binding=result.document.binding,
            entry_id=entry_id,
        )
        assert projected_direct.document_head_sha256 == direct.structured_head_sha256
        rendered = journal_path.read_text(encoding="utf-8")
        assert "An updated source-backed Running Note" in rendered

        # An external edit inside the owned section is observed even when the
        # document head itself did not change, retained as an exact file Source,
        # and pauses rather than overwrites the file.
        journal_path.write_text(
            rendered.replace(
                "An updated source-backed Running Note with café 🧭.",
                "External edit that must not be clobbered",
            ),
            encoding="utf-8",
        )
        paused = projections.project(
            result.document.store,
            binding=result.document.binding,
            entry_id=entry_id,
        )
        assert paused.status == "paused_diverged"
        assert paused.divergence_source_ref is not None
        assert "External edit that must not be clobbered" in journal_path.read_text(
            encoding="utf-8"
        )
        divergence_item = sources.get_item(
            type(reserved.resolved.source_ref).parse(paused.divergence_source_ref)
        )
        assert divergence_item is not None
        assert divergence_item.source_role == "imported_file"

        # A stale scrub instruction can survive a redaction/transition race.
        # Causal history wins over that stale policy: the dispatcher records
        # review attention before comparing usage IDs and never broad-scrubs.
        dispatcher = CoworkDocumentSourceDispatcher(
            sources,
            journal,
            service_principal=principal,
            registry=documents.stores.registry,
            vault_root=vault,
        )
        source_ref = reserved.resolved.source_ref
        stale_payload = {
            "schema": "wb.source-redaction-effect/v1",
            "redaction_event_id": "f" * 32,
            "source_ref": source_ref.to_dict(),
            "usage_id": receipt.prior_usage_id,
            "consumer_domain": "cowork_document",
            "consumer_id": "1" * 32,
            "redaction_policy": "scrub",
            "redaction_epoch": 1,
        }
        stale = OutboxEffect(
            effect_id="e" * 32,
            command_id=None,
            target_domain="cowork_document",
            effect_type="source.redaction",
            payload=stale_payload,
            payload_sha256="0" * 64,
            authorization_fingerprint="a" * 64,
            authorization_expires_at=None,
            status="leased",
            attempts=1,
            lease_owner="test",
            lease_until=None,
            result_ref=None,
            error_code=None,
        )
        with pytest.raises(RuntimeError):
            dispatcher._deliver(stale)
        review = journal.get_document_binding(entry_id)
        assert review is not None
        assert review.source_maintenance_state == "review_required"
        assert review.source_maintenance["reason"] == "document_contains_direct_edits"

        sources.grant_access(
            source_ref=source_ref,
            principal=principal,
            purpose="redaction",
            access_mode="metadata",
            authorization_fingerprint="b" * 64,
        )
        redaction = redact_source(
            sources,
            source_ref=source_ref,
            actor=principal,
            authorization_fingerprint="b" * 64,
            reason_code="user_requested",
        )
        assert len(redaction.pending_effect_ids) == 1
        summary = dispatcher.drain()
        assert summary.deferred == 1
        effect = SourceOutbox(sources).get(redaction.pending_effect_ids[0])
        assert effect is not None
        assert effect.status == "retryable"
        assert effect.error_code == "cowork_document_redaction_review_required"
        retained = truth_documents.get_document(
            result.document.store,
            result.document.binding.document_id,
        )
        projection = result.document.store.resolve_blob_path(
            f"blobs/{retained.content_sha256}"
        ).read_text(encoding="utf-8")
        assert projection != "\\[redacted\\]\n"
        assert "source-backed Running Note" in projection
        current_mirror = journal.get_document_binding(entry_id)
        assert current_mirror is not None and current_mirror.state == "current"
        with sources.connect() as conn:
            usage = conn.execute(
                "SELECT status,maintenance_state FROM source_usage_intents "
                "WHERE usage_id=?",
                (current_mirror.source_usage_id,),
            ).fetchone()
        assert usage is not None
        assert tuple(usage) == ("acknowledged", "pending_redaction")
    finally:
        kernel.close()


def test_materialized_result_recovers_after_crash_before_document_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Recoverable exact note.\n"
    entry_id = "2" * 32
    day_id = "2026-08-10"
    vault = tmp_path / "vault"
    _daily_note(vault, entry_id=entry_id, text=text, day_id=day_id)
    sources, principal, reserved = _source(tmp_path, text)
    kernel, documents, projections = _services(tmp_path, vault, sources, principal)
    original = RunningNoteDocumentService._commit_or_reconcile

    def crash(*_args, **_kwargs):
        raise RuntimeError("injected_crash_after_materialized")

    monkeypatch.setattr(
        RunningNoteDocumentService,
        "_commit_or_reconcile",
        staticmethod(crash),
    )
    try:
        with pytest.raises(RuntimeError, match="injected_crash"):
            documents.materialize(
                vault_root=vault,
                entry_id=entry_id,
                day_id=day_id,
                domain_revision="journal-entry-v1",
                source_store=sources,
                reserved_source=reserved,
                actors={"selected_by": "human:profile-00000001"},
                idempotency_key="running-note-recovery-00000001",
            )
        monkeypatch.setattr(
            RunningNoteDocumentService,
            "_commit_or_reconcile",
            staticmethod(original),
        )
        recovered = documents.materialize(
            vault_root=vault,
            entry_id=entry_id,
            day_id=day_id,
            domain_revision="journal-entry-v1",
            source_store=sources,
            reserved_source=reserved,
            actors={"selected_by": "human:profile-00000001"},
            idempotency_key="running-note-recovery-00000001",
        )
        assert recovered.binding.content_authority == "co_work"
        assert recovered.change.operation_kind == "exact_source_copy"
    finally:
        kernel.close()
