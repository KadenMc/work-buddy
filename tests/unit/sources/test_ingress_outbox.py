from __future__ import annotations

import json

import pytest

from work_buddy.sources import (
    AgentOutputRequest,
    ActorRef,
    DomainCommand,
    HumanInputRequest,
    InvalidSourceRequest,
    SourceIdempotencyConflict,
    SourceLeaseConflict,
    SourceOutbox,
    TrustedIngressContext,
    TrustedIngressService,
)


NOW = "2026-08-09T12:00:00.000+00:00"


def _context(
    *,
    issuer: ActorRef,
    human: ActorRef,
    service: ActorRef,
    tenant_id: str,
    auth_sha: str,
) -> TrustedIngressContext:
    return TrustedIngressContext(
        issuer=issuer,
        issuer_version="1.0",
        inputter=human,
        service_principal=service,
        tenant_scope_id=tenant_id,
        surface="journal_quick_capture",
        namespace="journal",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="local_surface_submission",
        authorization_fingerprint=auth_sha,
        permitted_purposes=("journal_effect",),
        gesture_receipt_id="gesture-00000001",
        gesture_context_sha256="b" * 64,
    )


def _command(auth_sha: str, *, expires_at: str | None = None) -> DomainCommand:
    return DomainCommand(
        schema="wb.journal-capture/v1",
        target_domain="journal",
        command_type="journal.capture.materialize",
        parameters={
            "day_id": "2026-08-09",
            "target_id": "running_notes",
            "mode": "dumb",
            "stated_at": None,
        },
        authorization_fingerprint=auth_sha,
        authorization_expires_at=expires_at,
    )


def test_ingress_atomically_persists_source_submission_command_and_effect(
    source_store,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    issuer: ActorRef,
    auth_sha: str,
) -> None:
    context = _context(
        issuer=issuer,
        human=human,
        service=service,
        tenant_id=tenant_id,
        auth_sha=auth_sha,
    )
    exact = "  preserve\r\nall whitespace  "
    request = HumanInputRequest(
        exact_content=exact,
        client_mutation_id="mutation-00000001",
        input_mode="paste",
        command=_command(auth_sha),
    )
    committed = TrustedIngressService(source_store).commit_human_input(context, request)
    assert committed.command_id and committed.effect_id
    assert not committed.deduplicated
    retried = TrustedIngressService(source_store).commit_human_input(context, request)
    assert retried.source_ref == committed.source_ref
    assert retried.submission_id == committed.submission_id
    assert retried.effect_id == committed.effect_id
    assert retried.deduplicated

    conn = source_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ingress_submissions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM source_commands").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM source_outbox").fetchone()[0] == 1
        representation = source_store._representation_row(conn, committed.source_ref)
        assert source_store._read_representation_row(representation) == exact.encode("utf-8")
        authors = conn.execute(
            "SELECT attribution_state, actor_ref_json FROM source_attributions "
            "WHERE role = 'author'"
        ).fetchall()
        assert [(row["attribution_state"], row["actor_ref_json"]) for row in authors] == [
            ("unknown", None)
        ]
        command_json = conn.execute(
            "SELECT parameters_json FROM source_commands"
        ).fetchone()[0]
        effect_json = conn.execute("SELECT payload_json FROM source_outbox").fetchone()[0]
        assert exact not in command_json
        assert exact not in effect_json
    finally:
        conn.close()


def test_agent_output_ingress_records_agent_authorship_and_is_idempotent(
    source_store,
    tenant_id: str,
    service: ActorRef,
    issuer: ActorRef,
    auth_sha: str,
) -> None:
    agent = ActorRef(
        issuer_authority_id=issuer.issuer_authority_id,
        subject="agent-run-00000001",
        kind="agent_run",
        tenant_scope_id=tenant_id,
    )
    context = _context(
        issuer=issuer,
        human=agent,
        service=service,
        tenant_id=tenant_id,
        auth_sha=auth_sha,
    )
    request = AgentOutputRequest(
        exact_content="generated Journal text",
        client_mutation_id="agent-output-mutation-0001",
        command=_command(auth_sha),
    )
    ingress = TrustedIngressService(source_store)
    first = ingress.commit_agent_output(context, request)
    retry = ingress.commit_agent_output(context, request)

    assert retry.source_ref == first.source_ref
    assert retry.effect_id == first.effect_id
    assert retry.deduplicated
    with source_store.connect() as conn:
        source = conn.execute(
            "SELECT source_role FROM source_items WHERE source_item_id=?",
            (first.source_ref.item_id,),
        ).fetchone()
        author = conn.execute(
            "SELECT attribution_state,actor_ref_json FROM source_attributions "
            "WHERE source_item_id=? AND role='author'",
            (first.source_ref.item_id,),
        ).fetchone()
    assert source["source_role"] == "agent_output"
    assert author["attribution_state"] == "identified"
    assert json.loads(author["actor_ref_json"])["kind"] == "agent_run"


def test_agent_output_ingress_rejects_a_human_inputter(
    source_store,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    issuer: ActorRef,
    auth_sha: str,
) -> None:
    context = _context(
        issuer=issuer,
        human=human,
        service=service,
        tenant_id=tenant_id,
        auth_sha=auth_sha,
    )
    with pytest.raises(InvalidSourceRequest):
        TrustedIngressService(source_store).commit_agent_output(
            context,
            AgentOutputRequest(
                exact_content="must not be misattributed",
                client_mutation_id="agent-output-mutation-0002",
            ),
        )


def test_ingress_same_key_different_payload_conflicts_without_mutation(
    source_store,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    issuer: ActorRef,
    auth_sha: str,
) -> None:
    context = _context(
        issuer=issuer,
        human=human,
        service=service,
        tenant_id=tenant_id,
        auth_sha=auth_sha,
    )
    service_api = TrustedIngressService(source_store)
    service_api.commit_human_input(
        context,
        HumanInputRequest("first", "mutation-00000002", "direct_entry"),
    )
    with pytest.raises(SourceIdempotencyConflict) as caught:
        service_api.commit_human_input(
            context,
            HumanInputRequest("secret second payload", "mutation-00000002", "paste"),
        )
    assert "secret" not in str(caught.value)
    conn = source_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
    finally:
        conn.close()


def test_retry_with_fresh_single_use_gesture_deduplicates_semantic_mutation(
    source_store,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    issuer: ActorRef,
    auth_sha: str,
) -> None:
    first_context = _context(
        issuer=issuer,
        human=human,
        service=service,
        tenant_id=tenant_id,
        auth_sha=auth_sha,
    )
    second_context = TrustedIngressContext(
        issuer=issuer,
        issuer_version=first_context.issuer_version,
        inputter=human,
        service_principal=service,
        tenant_scope_id=tenant_id,
        surface=first_context.surface,
        namespace=first_context.namespace,
        sensitivity_class=first_context.sensitivity_class,
        retention_class=first_context.retention_class,
        inputter_assurance=first_context.inputter_assurance,
        authorization_fingerprint="e" * 64,
        permitted_purposes=first_context.permitted_purposes,
        gesture_receipt_id="gesture-00000002",
        gesture_context_sha256="f" * 64,
    )
    first_command = _command(auth_sha)
    second_command = _command("e" * 64)
    service_api = TrustedIngressService(source_store)
    first = service_api.commit_human_input(
        first_context,
        HumanInputRequest(
            "same semantic input",
            "mutation-gesture-retry",
            "direct_entry",
            command=first_command,
        ),
    )
    retried = service_api.commit_human_input(
        second_context,
        HumanInputRequest(
            "same semantic input",
            "mutation-gesture-retry",
            "direct_entry",
            command=second_command,
        ),
    )
    assert retried.deduplicated
    assert retried.source_ref == first.source_ref
    assert retried.effect_id == first.effect_id


def test_command_envelope_rejects_raw_content_fields(auth_sha: str) -> None:
    with pytest.raises(InvalidSourceRequest):
        DomainCommand(
            schema="wb.journal-capture/v1",
            target_domain="journal",
            command_type="journal.capture.materialize",
            parameters={"exact_text": "must live in Sources"},
            authorization_fingerprint=auth_sha,
        )


def test_outbox_leases_expire_receipts_are_idempotent_and_auth_expiry_pauses(
    source_store,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    issuer: ActorRef,
    auth_sha: str,
) -> None:
    context = _context(
        issuer=issuer,
        human=human,
        service=service,
        tenant_id=tenant_id,
        auth_sha=auth_sha,
    )
    active = TrustedIngressService(source_store).commit_human_input(
        context,
        HumanInputRequest(
            "active",
            "mutation-00000003",
            "direct_entry",
            command=_command(auth_sha, expires_at="2026-08-09T13:00:00.000+00:00"),
        ),
    )
    expired = TrustedIngressService(source_store).commit_human_input(
        context,
        HumanInputRequest(
            "expired",
            "mutation-00000004",
            "direct_entry",
            command=_command(auth_sha, expires_at="2026-08-09T11:00:00.000+00:00"),
        ),
    )
    outbox = SourceOutbox(source_store)
    leased = outbox.lease("worker-00000001", limit=10, lease_seconds=30, at=NOW)
    assert [effect.effect_id for effect in leased] == [active.effect_id]
    assert outbox.get(expired.effect_id).status == "paused"
    with pytest.raises(SourceLeaseConflict):
        outbox.complete(
            active.effect_id,
            "wrong-worker",
            result_ref="journal-entry-00000001",
            result_sha256="c" * 64,
            at=NOW,
        )
    receipt = outbox.complete(
        active.effect_id,
        "worker-00000001",
        result_ref="journal-entry-00000001",
        result_sha256="c" * 64,
        at=NOW,
    )
    repeated = outbox.complete(
        active.effect_id,
        "worker-00000001",
        result_ref="journal-entry-00000001",
        result_sha256="c" * 64,
        at=NOW,
    )
    assert repeated == receipt


def test_expired_lease_is_recovered_without_reusing_session_grant(
    source_store,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    issuer: ActorRef,
    auth_sha: str,
) -> None:
    context = _context(
        issuer=issuer,
        human=human,
        service=service,
        tenant_id=tenant_id,
        auth_sha=auth_sha,
    )
    committed = TrustedIngressService(source_store).commit_human_input(
        context,
        HumanInputRequest(
            "retry",
            "mutation-00000005",
            "direct_entry",
            command=_command(auth_sha),
        ),
    )
    outbox = SourceOutbox(source_store)
    first = outbox.lease(
        "worker-00000001", limit=1, lease_seconds=1, at="2026-08-09T12:00:00.000+00:00"
    )[0]
    second = outbox.lease(
        "worker-00000002", limit=1, lease_seconds=30, at="2026-08-09T12:00:02.000+00:00"
    )[0]
    assert first.effect_id == second.effect_id == committed.effect_id
    assert second.attempts == 2


def test_exact_and_filtered_outbox_leasing_do_not_consume_other_domains(
    source_store,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    issuer: ActorRef,
    auth_sha: str,
) -> None:
    context = _context(
        issuer=issuer,
        human=human,
        service=service,
        tenant_id=tenant_id,
        auth_sha=auth_sha,
    )
    ingress = TrustedIngressService(source_store)
    journal = ingress.commit_human_input(
        context,
        HumanInputRequest(
            "journal",
            "mutation-exact-lease-1",
            "direct_entry",
            command=_command(auth_sha),
        ),
    )
    truth_command = DomainCommand(
        schema="wb.truth-capture/v1",
        target_domain="truth",
        command_type="truth.capture.materialize",
        parameters={"candidate_id": "candidate-00000001"},
        authorization_fingerprint=auth_sha,
    )
    truth = ingress.commit_human_input(
        context,
        HumanInputRequest(
            "truth",
            "mutation-exact-lease-2",
            "direct_entry",
            command=truth_command,
        ),
    )
    outbox = SourceOutbox(source_store)
    leased_truth = outbox.lease_exact(
        truth.effect_id, "truth-worker", at=NOW
    )
    assert leased_truth.effect_id == truth.effect_id
    assert outbox.lease_exact(truth.effect_id, "truth-worker", at=NOW) == leased_truth
    leased_journal = outbox.lease(
        "journal-worker",
        target_domain="journal",
        effect_type="journal.capture.materialize",
        at=NOW,
    )
    assert [effect.effect_id for effect in leased_journal] == [journal.effect_id]
    assert [effect.effect_id for effect in outbox.list(target_domain="truth")] == [
        truth.effect_id
    ]
