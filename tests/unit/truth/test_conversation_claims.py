from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from work_buddy.conversation_truth import propose_claim_from_conversation_message
from work_buddy.conversations import store as conversations
from work_buddy.hindsight_projection.contracts import DesiredProjectionState
from work_buddy.hindsight_projection.truth_reader import (
    TruthHindsightProjectionPolicy,
    TruthStoreProjectionReader,
)
from work_buddy.mcp_server.ops import conversation_truth_ops
from work_buddy.mcp_server.op_registry import get_op, load_builtin_ops
from work_buddy.mcp_server.ops.conversation_truth_ops import (
    truth_claim_propose_from_conversation,
)
from work_buddy.security.actors import ActorRef
from work_buddy.security.local_identity import LocalIdentityAuthority
from work_buddy.sources import SourceRef, SourceStore, conversation_origin
from work_buddy.truth import queries
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.evidence_relations import validate_claim_evidence_role
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.source_provenance import provenance_for_subject
from work_buddy.truth.store import TruthStore


def _profile(store_id: str) -> dict[str, object]:
    return {
        "store_id": store_id,
        "profile": "conversation-source-test",
        "title": "Conversation source test",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
        "support_policy": {
            "fact": {
                "minimum_usable_supports": 1,
                "allowed_effects": ["supports"],
                "allow_human_assertion_as_source": True,
            }
        },
        "document_surface": {"enabled": False},
    }


@pytest.fixture
def conversation_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conversations, "_DB_PATH", tmp_path / "conversations.db")
    monkeypatch.setattr(conversations, "_LEGACY_DB_PATH", tmp_path / "legacy.db")
    with conversations.get_connection() as connection:
        conversations._ensure_schema(connection)


@pytest.fixture
def stores(tmp_path: Path) -> tuple[SourceStore, TruthStore]:
    return (
        SourceStore.create(tmp_path / "sources"),
        TruthStore.create(tmp_path / "truth", _profile("9" * 32)),
    )


@pytest.fixture
def actors() -> tuple[ActorRef, ActorRef, dict[str, str]]:
    local = ActorRef(
        "local-identity-authority",
        "enrolled-human-profile",
        "human",
        "local-tenant-scope",
    )
    semantic = ActorRef(
        local.issuer_authority_id,
        "conversation-truth-agent-semantic",
        "agent_run",
        local.tenant_scope_id,
    )
    producer = {
        "model": "semantic-model",
        "model_source": "session_manifest",
        "harness": "pytest",
        "surface": "mcp",
        "session_id": "semantic-session-0001",
    }
    return local, semantic, producer


def _message(*, role: str = "user", producer=None):
    conversation = conversations.create_conversation("Source conversation")
    message = conversations.add_message(
        conversation.conversation_id,
        role=role,
        content="The migration completed after the final integrity check.",
        producer=producer,
        message_id="conversation-message-0001",
    )
    assert message is not None
    return conversation, message


def _propose(
    sources: SourceStore,
    truth: TruthStore,
    local: ActorRef,
    semantic: ActorRef,
    producer: dict[str, str],
    *,
    idempotency_key: str = "conversation-claim-0001",
    proposition: str = "The migration completed after an integrity check.",
):
    conversation, message = _message()
    result = propose_claim_from_conversation_message(
        truth,
        sources,
        local_authority_actor=local,
        semantic_producer=semantic,
        producer_meta=producer,
        conversation_id=conversation.conversation_id,
        message_id=message.message_id,
        proposition=proposition,
        claim_kind="fact",
        idempotency_key=idempotency_key,
        run_ref="conversation-truth-run-0001",
    )
    return conversation, message, result


def test_exact_message_proposal_reuses_source_and_records_complete_receipts(
    conversation_db: None,
    stores: tuple[SourceStore, TruthStore],
    actors: tuple[ActorRef, ActorRef, dict[str, str]],
) -> None:
    del conversation_db
    sources, truth = stores
    local, semantic, producer = actors
    conversation, message, result = _propose(
        sources, truth, local, semantic, producer
    )

    status = TruthLifecycle(truth).latest_status(result.proposal.claim_id)
    assert status is not None and status.status == "proposed"
    assert result.proposal.candidate_decision_id is None
    claim = truth.get_claim(result.proposal.claim_id)
    assert claim is not None
    assert claim.created_by_kind == "agent_run"
    assert claim.created_by_ref == semantic.canonical_id

    evidence = truth.get_evidence(result.proposal.evidence_id)
    span = truth.get_span(result.proposal.span_id)
    relation = truth.get_link(result.proposal.relation_id)
    assert evidence is not None and evidence.kind == "chat"
    assert span is not None
    assert span.quote_exact == message.content
    # A conversation envelope role of `user` is not fabricated into human
    # authorship. Human authority enters only through a later exact decision.
    assert span.author_kind == "unknown"
    assert span.author_ref is None
    assert relation is not None and relation.link_type == "evidence_relation"
    parsed_role = validate_claim_evidence_role(json.loads(relation.role_json or "{}"))
    assert parsed_role.evidential_effect == "supports"
    assert parsed_role.derivation_relationship == "paraphrase"

    resolutions = queries.evidence_source_resolutions(truth, evidence.id)
    assert len(resolutions) == 1
    assert json.loads(resolutions[0].source_ref_json) == result.source_ref.to_dict()
    assert resolutions[0].representation_id == result.representation_id
    assert resolutions[0].usage_id == result.proposal.usage_id
    with truth.connect() as connection:
        usage = connection.execute(
            "SELECT status, purpose FROM truth_source_usage_events "
            "WHERE usage_id = ? ORDER BY created_at, id",
            (result.proposal.usage_id,),
        ).fetchall()
    assert [(row["status"], row["purpose"]) for row in usage] == [
        ("reserved", "truth_claim_proposal"),
        ("acknowledged", "truth_claim_proposal"),
    ]

    projection = provenance_for_subject(
        truth, subject_kind="claim", subject_ref=claim.id
    )
    by_role = {event.role: event for event in projection.events}
    assert json.loads(by_role["semantic_producer"].actor_ref_json) == semantic.to_dict()
    assert "candidate_decision_actor" not in by_role
    assert "lifecycle_decision_actor" not in by_role

    replay = propose_claim_from_conversation_message(
        truth,
        sources,
        local_authority_actor=local,
        semantic_producer=semantic,
        producer_meta=producer,
        conversation_id=conversation.conversation_id,
        message_id=message.message_id,
        proposition=claim.proposition,
        claim_kind="fact",
        idempotency_key="conversation-claim-0001",
        run_ref="conversation-truth-run-0001",
    )
    assert replay.source_ref == result.source_ref
    assert replay.representation_id == result.representation_id
    assert replay.proposal.replayed is True
    assert replay.proposal.usage_id == result.proposal.usage_id
    with sources.connect() as connection:
        identities = connection.execute(
            "SELECT authority_id, source_item_id FROM source_origin_identities "
            "WHERE provider_id = ? AND occurrence_key = ?",
            (
                "work-buddy-conversation",
                conversation_origin(
                    conversation_id=conversation.conversation_id,
                    message_id=message.message_id,
                ).occurrence_key,
            ),
        ).fetchall()
    assert len(identities) == 1


def test_idempotency_conflict_and_source_author_are_independent_of_semantics(
    conversation_db: None,
    stores: tuple[SourceStore, TruthStore],
    actors: tuple[ActorRef, ActorRef, dict[str, str]],
) -> None:
    del conversation_db
    sources, truth = stores
    local, semantic, producer = actors
    conversation = conversations.create_conversation("Agent-authored source")
    message = conversations.add_message(
        conversation.conversation_id,
        role="agent",
        content="The migration completed after the final integrity check.",
        producer={
            "model": "source-message-model",
            "harness": "conversation-worker",
            "session_id": "source-message-session",
        },
        message_id="agent-message-source-0001",
    )
    assert message is not None
    result = propose_claim_from_conversation_message(
        truth,
        sources,
        local_authority_actor=local,
        semantic_producer=semantic,
        producer_meta=producer,
        conversation_id=conversation.conversation_id,
        message_id=message.message_id,
        proposition="The migration completed after an integrity check.",
        claim_kind="fact",
        idempotency_key="conversation-agent-source-0001",
    )
    span = truth.get_span(result.proposal.span_id)
    assert span is not None and span.author_kind == "agent_run"
    assert span.author_ref is not None
    assert span.author_ref != semantic.canonical_id

    with pytest.raises(InvariantViolation, match="different source request"):
        propose_claim_from_conversation_message(
            truth,
            sources,
            local_authority_actor=local,
            semantic_producer=semantic,
            producer_meta=producer,
            conversation_id=conversation.conversation_id,
            message_id=message.message_id,
            proposition="A conflicting semantic proposal.",
            claim_kind="fact",
            idempotency_key="conversation-agent-source-0001",
        )


def test_hindsight_becomes_eligible_only_after_separate_human_confirmation(
    conversation_db: None,
    stores: tuple[SourceStore, TruthStore],
    actors: tuple[ActorRef, ActorRef, dict[str, str]],
) -> None:
    del conversation_db
    sources, truth = stores
    local, semantic, producer = actors
    _conversation, _message_record, result = _propose(
        sources, truth, local, semantic, producer
    )
    policy = TruthHindsightProjectionPolicy(
        enabled=True,
        policy_id="confirmed-conversation-claims-v1",
        authorization_ref="policy:conversation-truth:test",
    )
    reader = TruthStoreProjectionReader(truth, policy=policy)
    claim = truth.get_claim(result.proposal.claim_id)
    assert claim is not None
    before = reader.desired_for_claim(
        result.proposal.claim_id,
        policy.policy_id,
        at=claim.created_at,
    )
    assert before.desired_state is DesiredProjectionState.REMOVE
    assert before.reason_code != "claim_confirmed"

    human = Actor("human", local.canonical_id)
    lifecycle = TruthLifecycle(truth)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=human,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=claim.canonical_sha256,
    )
    lifecycle.confirm_claim(
        claim_id=claim.id,
        gesture_id=gesture.id,
        actor=human,
        expected_context_sha256=None,
    )

    confirmed_status = lifecycle.latest_status(claim.id)
    assert confirmed_status is not None
    after = reader.desired_for_claim(
        claim.id, policy.policy_id, at=confirmed_status.at
    )
    assert after.desired_state is DesiredProjectionState.UPSERT
    assert after.reason_code == "claim_confirmed"
    snapshot = reader.resolve_snapshot(after, at=confirmed_status.at)
    assert snapshot.lifecycle_status == "confirmed"
    assert len(snapshot.source_dependencies) == 1
    assert snapshot.source_dependencies[0].source_ref == result.source_ref.uri
    assert confirmed_status.actor_kind == "human"
    assert confirmed_status.actor_ref == local.canonical_id


def test_public_capability_has_no_caller_controlled_actor_fields() -> None:
    load_builtin_ops()
    assert get_op("op.wb.truth_claim_propose_from_conversation") is not None
    parameters = inspect.signature(truth_claim_propose_from_conversation).parameters
    assert "agent_session_id" in parameters  # transport-owned gateway injection seam
    assert "selector" not in parameters
    assert {
        "actor",
        "actor_ref",
        "semantic_producer",
        "source_principal",
        "applier",
        "human_actor",
        "decision",
    }.isdisjoint(parameters)


def test_public_op_composes_default_authorities_and_registry(
    conversation_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del conversation_db
    truth = TruthStore.create(tmp_path / "registered-truth", _profile("8" * 32))
    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    registry.register(truth)
    identity_path = tmp_path / "local-identity.db"
    source_root = tmp_path / "default-sources"
    session_id = "gateway-session-0001"
    producer_model = "manifest-model"
    conversation, message = _message()

    monkeypatch.setattr(
        conversation_truth_ops,
        "LocalIdentityAuthority",
        lambda: LocalIdentityAuthority(identity_path),
    )
    monkeypatch.setattr(conversation_truth_ops, "TruthStoreRegistry", lambda: registry)
    monkeypatch.setattr(
        conversation_truth_ops,
        "resolve",
        lambda resource: source_root
        if resource == "stores/sources"
        else pytest.fail(f"unexpected resource: {resource}"),
    )
    class _Event:
        @staticmethod
        def to_dict():
            return {"event_id": "event-test", "published": True, "error": None}

    monkeypatch.setattr(
        conversation_truth_ops,
        "emit_truth_event",
        lambda *args, **kwargs: _Event(),
    )
    from work_buddy.mcp_server.ops import truth_ops

    monkeypatch.setattr(
        truth_ops,
        "_session_manifest",
        lambda requested: {
            "session_id": session_id,
            "harness_id": "pytest-gateway",
            "model": producer_model,
        }
        if requested == session_id
        else {},
    )

    response = truth_claim_propose_from_conversation(
        store_id=truth.store_id,
        conversation_id=conversation.conversation_id,
        message_id=message.message_id,
        proposition="The migration completed after an integrity check.",
        claim_kind="fact",
        producer_model=producer_model,
        idempotency_key="public-conversation-truth-0001",
        producer_call_id="model-call-0001",
        agent_session_id=session_id,
    )

    assert response["ok"] is True
    assert response["claim_status"] == "proposed"
    assert response["human_decision_recorded"] is False
    assert response["conversation_id"] == conversation.conversation_id
    assert response["message_id"] == message.message_id
    assert SourceStore.open(source_root).get_item(
        SourceRef.parse(response["source_ref"])
    ) is not None
    # The serialized actor is derived from the enrolled authority and the
    # gateway-owned session manifest, never from capability actor parameters.
    enrolled = LocalIdentityAuthority(identity_path).enrolled_actor()
    semantic = response["semantic_producer"]
    assert semantic["issuer_authority_id"] == enrolled.issuer_authority_id
    assert semantic["tenant_scope_id"] == enrolled.tenant_scope_id
    assert semantic["kind"] == "agent_run"
