from __future__ import annotations

import pytest

from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.settings import get_journal_day_window
from work_buddy.sources import (
    ActorRef,
    DomainCommand,
    HumanInputRequest,
    SourceOutbox,
    SourceStore,
    TrustedIngressContext,
    TrustedIngressService,
    redact_source,
)


def _day_id() -> str:
    window = get_journal_day_window("2026-08-09")
    return f"journal-day:2026-08-09:{window.timezone}:{window.boundary}"


def _write(_rel, abs_path, content, **_kw):
    abs_path.write_bytes(content.encode("utf-8"))
    return True


def test_pending_source_command_recovers_once_after_process_restart(tmp_path, monkeypatch):
    sources = SourceStore.create(tmp_path / "sources")
    tenant = "tenant-journal-recovery"
    issuer = ActorRef("installation-authority", "trusted-ingress", "service", tenant)
    human = ActorRef("installation-authority", "local-profile", "human", tenant)
    service_actor = ActorRef(
        "installation-authority", "work-buddy-journal-service", "service", tenant
    )
    context = TrustedIngressContext(
        issuer=issuer,
        issuer_version="local-identity/v1",
        inputter=human,
        service_principal=service_actor,
        tenant_scope_id=tenant,
        surface="work-buddy-journal",
        namespace="journal-quick-capture",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="enrolled_local_session_gesture",
        authorization_fingerprint="a" * 64,
        permitted_purposes=("journal.materialize",),
        gesture_receipt_id="gesture-recovery",
        gesture_context_sha256="b" * 64,
    )
    command = DomainCommand(
        schema="wb.journal-capture/v1",
        target_domain="journal",
        command_type="journal.capture.materialize",
        parameters={
            "client_mutation_id": "capture-recovery-1",
            "day_id": _day_id(),
            "target_id": "running_notes",
            "mode": "dumb",
            "input_mode": "paste",
            "stated_at": "2026-08-09T15:15:00-04:00",
        },
        authorization_fingerprint="a" * 64,
    )
    committed = TrustedIngressService(sources).commit_human_input(
        context,
        HumanInputRequest(
            exact_content="same-looking entry\nwith exact spacing  ",
            client_mutation_id="capture-recovery-1",
            input_mode="paste",
            occurred_at="2026-08-09T15:15:00-04:00",
            command=command,
        ),
    )
    assert committed.effect_id is not None
    assert SourceOutbox(sources).get(committed.effect_id).status == "pending"

    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    day = vault / "journal" / "2026-08-09.md"
    day.write_text(
        "# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    store = JournalCaptureStore(tmp_path / "journal.db")
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(store, JournalContentAdapter(vault)),
        service_principal=service_actor,
    )

    first = dispatcher.drain()
    second = dispatcher.drain()

    assert first.delivered == 1
    assert second.delivered == 0
    assert SourceOutbox(sources).get(committed.effect_id).status == "succeeded"
    captures = store.list_captures("2026-08-09", limit=10)
    assert len(captures) == 1
    assert len(store.list_running_notes("2026-08-09")) == 1
    projected = day.read_text(encoding="utf-8")
    assert projected.count("same-looking entry\nwith exact spacing  ") == 1


@pytest.mark.parametrize("mixed_projection", [False, True])
def test_source_redaction_scrubs_only_journal_owned_exact_bytes(
    tmp_path, monkeypatch, mixed_projection
):
    sources = SourceStore.create(tmp_path / "sources")
    tenant = "tenant-journal-redaction"
    issuer = ActorRef("installation-authority", "trusted-ingress", "service", tenant)
    human = ActorRef("installation-authority", "local-profile", "human", tenant)
    service_actor = ActorRef(
        "installation-authority", "work-buddy-journal-service", "service", tenant
    )
    auth = "a" * 64
    context = TrustedIngressContext(
        issuer=issuer,
        issuer_version="local-identity/v1",
        inputter=human,
        service_principal=service_actor,
        tenant_scope_id=tenant,
        surface="work-buddy-journal",
        namespace="journal-quick-capture",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="enrolled_local_session_gesture",
        authorization_fingerprint=auth,
        permitted_purposes=("journal.materialize",),
        gesture_receipt_id="gesture-redaction",
        gesture_context_sha256="b" * 64,
    )
    command = DomainCommand(
        schema="wb.journal-capture/v1",
        target_domain="journal",
        command_type="journal.capture.materialize",
        parameters={
            "client_mutation_id": "capture-redaction-1",
            "day_id": _day_id(),
            "target_id": "running_notes",
            "mode": "dumb",
            "input_mode": "paste",
            "stated_at": None,
        },
        authorization_fingerprint=auth,
    )
    exact = "managed private passage that must not remain readable"
    committed = TrustedIngressService(sources).commit_human_input(
        context,
        HumanInputRequest(
            exact_content=exact,
            client_mutation_id="capture-redaction-1",
            input_mode="paste",
            command=command,
        ),
    )
    assert committed.effect_id is not None

    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    day = vault / "journal" / "2026-08-09.md"
    day.write_text(
        "# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    store = JournalCaptureStore(tmp_path / "journal.db")
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(store, JournalContentAdapter(vault)),
        service_principal=service_actor,
    )
    assert dispatcher.drain().delivered == 1
    capture = store.get_capture_by_source_effect(committed.effect_id)
    assert capture is not None and capture.entry_id is not None
    assert capture.source_usage_id is not None
    assert exact in day.read_text(encoding="utf-8")

    document_usage = None
    if mixed_projection:
        document_usage = sources.reserve_usage(
            source_ref=committed.source_ref,
            representation_id=committed.representation_id,
            principal=service_actor,
            purpose="journal.materialize",
            consumer_domain="cowork_document",
            consumer_id="1" * 32,
            use_kind="mixed_derivative",
            disclosure_kind="semantic_derivative",
            redaction_policy="review",
            selector={"kind": "whole", "transition_change_id": "2" * 32},
        )
        sources.acknowledge_usage(document_usage.usage_id)
        store.record_document_binding(
            entry_id=capture.entry_id,
            binding_id="3" * 32,
            store_id="4" * 32,
            document_id="5" * 32,
            change_id="2" * 32,
            source_consumer_id="1" * 32,
            source_usage_id=document_usage.usage_id,
            source_use_kind="mixed_derivative",
            source_disclosure_kind="semantic_derivative",
            source_redaction_policy="review",
            cowork_href="/app/cowork?store_id=4&document_id=5",
            content_authority_epoch=1,
            entry_version=1,
            inspection={"schema": "test-mixed-projection/v1"},
        )

    sources.grant_access(
        source_ref=committed.source_ref,
        principal=human,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=auth,
    )
    redaction = redact_source(
        sources,
        source_ref=committed.source_ref,
        actor=human,
        authorization_fingerprint=auth,
        reason_code="user_requested",
    )
    assert len(redaction.pending_effect_ids) == (2 if mixed_projection else 1)
    assert dispatcher.drain().delivered == 1

    projected = day.read_text(encoding="utf-8")
    entry = store.get_entry(capture.entry_id)
    assert entry is not None
    assert entry.markdown == "[redacted]"
    assert entry.resolution_state == "redacted"
    assert store.list_running_notes("2026-08-09") == []
    with sources.connect() as conn:
        usage = conn.execute(
            "SELECT status,maintenance_state FROM source_usage_intents WHERE usage_id=?",
            (capture.source_usage_id,),
        ).fetchone()
    assert tuple(usage) == ("released", "completed")
    effects = {
        effect.target_domain: effect
        for effect_id in redaction.pending_effect_ids
        if (effect := SourceOutbox(sources).get(effect_id)) is not None
    }
    assert effects["journal"].status == "succeeded"
    if mixed_projection:
        assert exact in projected
        assert "wb:journal-entry-redacted/v1" not in projected
        assert entry.projection_state.value == "paused_diverged"
        assert effects["cowork_document"].status == "pending"
        mirror = store.get_document_binding(capture.entry_id)
        assert mirror is not None
        assert mirror.source_maintenance_state == "review_required"
        assert mirror.source_maintenance["reason"] == (
            "journal_exact_copy_redacted_file_is_mixed_derivative"
        )
        assert document_usage is not None
        with sources.connect() as conn:
            pending = conn.execute(
                "SELECT status,maintenance_state FROM source_usage_intents "
                "WHERE usage_id=?",
                (document_usage.usage_id,),
            ).fetchone()
        assert tuple(pending) == ("acknowledged", "pending_redaction")
    else:
        assert exact not in projected
        assert "wb:journal-entry-redacted/v1" in projected
    completed = redact_source(
        sources,
        source_ref=committed.source_ref,
        actor=human,
        authorization_fingerprint=auth,
        reason_code="user_requested",
    )
    assert completed.managed_copy_state == (
        "pending" if mixed_projection else "complete"
    )
