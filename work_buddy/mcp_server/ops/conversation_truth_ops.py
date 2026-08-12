"""Agent capability for source-backed claims from durable conversations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from work_buddy.conversation_truth import propose_claim_from_conversation_message
from work_buddy.mcp_server.op_registry import register_op
from work_buddy.paths import resolve
from work_buddy.security.actors import ActorRef
from work_buddy.security.local_identity import LocalIdentityAuthority
from work_buddy.sources import SourceStore
from work_buddy.sources.models import canonical_sha256
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.registry import TruthStoreRegistry


def _gateway_semantic_producer(
    *,
    local_actor: ActorRef,
    producer_model: str,
    agent_session_id: str | None,
    producer_call_id: str | None,
) -> tuple[ActorRef, Mapping[str, Any]]:
    """Derive one qualified actor from the transport-owned MCP session.

    The existing Truth identity resolver checks the gateway-injected session
    against its manifest and rejects model conflicts.  Callers never supply an
    actor reference, issuer, tenant, actor kind, or authority subject.
    """

    from work_buddy.mcp_server.ops.truth_ops import _agent_actor

    actor = _agent_actor(
        producer_model=producer_model,
        agent_session_id=agent_session_id,
        producer_call_id=producer_call_id,
    )
    identity = {
        "schema": "wb.conversation-truth-semantic-producer/v1",
        "legacy_actor_ref": actor.ref,
        "producer": dict(actor.meta),
    }
    return (
        ActorRef(
            issuer_authority_id=local_actor.issuer_authority_id,
            subject=f"conversation-truth-agent-{canonical_sha256(identity)[:32]}",
            kind="agent_run",
            tenant_scope_id=local_actor.tenant_scope_id,
        ),
        dict(actor.meta),
    )


def truth_claim_propose_from_conversation(
    store_id: str,
    conversation_id: str,
    message_id: str,
    proposition: str,
    claim_kind: str,
    producer_model: str,
    idempotency_key: str,
    evidential_effect: str = "supports",
    derivation_relationship: str = "paraphrase",
    structured: Mapping[str, Any] | None = None,
    scope: str = "store",
    valid_from: str | None = None,
    valid_to: str | None = None,
    confidence_extraction: float | None = None,
    relation_diagnostics: Mapping[str, Any] | None = None,
    producer_call_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Propose one claim supported by one exact durable conversation message."""

    local_actor = LocalIdentityAuthority().enrolled_actor()
    semantic_producer, producer_meta = _gateway_semantic_producer(
        local_actor=local_actor,
        producer_model=producer_model,
        agent_session_id=agent_session_id,
        producer_call_id=producer_call_id,
    )
    truth_store = TruthStoreRegistry().open_store(store_id)
    source_store = SourceStore.create(resolve("stores/sources"))
    result = propose_claim_from_conversation_message(
        truth_store,
        source_store,
        local_authority_actor=local_actor,
        semantic_producer=semantic_producer,
        producer_meta=producer_meta,
        conversation_id=conversation_id,
        message_id=message_id,
        proposition=proposition,
        claim_kind=claim_kind,
        idempotency_key=idempotency_key,
        # This public vertical slice intentionally supports the complete
        # addressed message. It does not accept caller-supplied source bytes.
        selector=None,
        evidential_effect=evidential_effect,
        derivation_relationship=derivation_relationship,
        structured=structured,
        scope=scope,
        valid_from=valid_from,
        valid_to=valid_to,
        confidence_extraction=confidence_extraction,
        relation_diagnostics=relation_diagnostics,
        run_ref=(
            "mcp-conversation-truth:"
            f"{str(agent_session_id or 'unresolved')}:"
            f"{str(producer_call_id or idempotency_key)}"
        ),
    )
    latest = TruthLifecycle(truth_store).latest_status(result.proposal.claim_id)
    emission = (
        emit_truth_event(
            "truth.claim_proposed",
            store_id=truth_store.store_id,
            subject_kind="claim",
            subject_id=result.proposal.claim_id,
            data={
                "created": True,
                "source_kind": "conversation_message",
            },
        )
        if result.proposal.claim_created and not result.proposal.replayed
        else None
    )
    return {
        "ok": True,
        **result.to_dict(),
        "claim_status": None if latest is None else latest.status,
        "human_decision_recorded": False,
        "event": None if emission is None else emission.to_dict(),
    }


register_op(
    "op.wb.truth_claim_propose_from_conversation",
    truth_claim_propose_from_conversation,
    replace=True,
)


__all__ = ["truth_claim_propose_from_conversation"]
