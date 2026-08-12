"""Source-backed Truth proposals from exact Work Buddy conversation messages.

This module is the narrow composition seam between the durable conversation
store, Sources, and Truth.  A caller addresses one message by its native
``conversation_id`` + ``message_id`` identity.  It never locates a message by
text, ordering, or a best-effort search.

The semantic producer and the source author are deliberately independent:
the former is the agent run that prepared the proposition, while the latter
comes only from the conversation provider's retained attribution assertions.
No human decision is inferred by this proposal path.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from work_buddy.security.actors import ActorRef
from work_buddy.sources import (
    ConversationMessageProvider,
    ProviderRegistry,
    SourceRef,
    SourceStore,
    conversation_origin,
    resolve_source,
    source_capture_from_origin,
)
from work_buddy.sources.models import canonical_json, canonical_sha256
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.source_claims import (
    SOURCE_PURPOSE,
    SourceClaimActors,
    SourceClaimCandidate,
    SourceClaimProposalResult,
    truth_claim_propose_from_source,
)
from work_buddy.truth.store import TruthStore


CAPTURE_PURPOSE = "truth_evidence"
ORIGINATING_SURFACE = "work-buddy-conversation"
TRUTH_SERVICE_SUBJECT = "work-buddy-truth-service"
TRUTH_KERNEL_SUBJECT = "work-buddy-truth-kernel"


@dataclass(frozen=True, slots=True)
class ConversationClaimProposalResult:
    """Content-free envelope around the atomic source-backed Truth result."""

    conversation_id: str
    message_id: str
    source_ref: SourceRef
    representation_id: str
    native_revision: str | None
    semantic_producer: ActorRef
    proposal: SourceClaimProposalResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "source_ref": self.source_ref.uri,
            "representation_id": self.representation_id,
            "native_revision": self.native_revision,
            "semantic_producer": self.semantic_producer.to_dict(),
            "proposal": self.proposal.to_dict(),
        }


def _access_binding_value(
    *,
    truth_store: TruthStore,
    source_ref: SourceRef,
    representation_id: str,
    byte_length: int,
    principal: ActorRef,
) -> dict[str, Any]:
    return {
        "schema": "wb.conversation-truth-access/v1",
        "truth_store_id": truth_store.store_id,
        "source_ref": source_ref.to_dict(),
        "representation_id": representation_id,
        "principal": principal.to_dict(),
        "purpose": SOURCE_PURPOSE,
        "access_mode": "content",
        "scope": {
            "consumer_domain": "truth",
            "use_kind": "evidence_snapshot",
        },
        "content_boundary": {
            "representation_id": representation_id,
            "max_bytes": byte_length,
        },
    }


def _ensure_truth_access(
    source_store: SourceStore,
    truth_store: TruthStore,
    *,
    source_ref: SourceRef,
    representation_id: str,
    byte_length: int,
    principal: ActorRef,
) -> None:
    """Install or verify the deterministic Truth-only content grant.

    Provider capture uses the separate ``truth_evidence`` purpose.  The grant
    used by the composite claim write is therefore explicit, scoped, stable
    across retry, and independently revocable.  A revoked deterministic grant
    is never silently replaced.
    """

    value = _access_binding_value(
        truth_store=truth_store,
        source_ref=source_ref,
        representation_id=representation_id,
        byte_length=byte_length,
        principal=principal,
    )
    fingerprint = canonical_sha256(value)
    binding_id = canonical_sha256(
        {"domain": "work-buddy.conversation-truth-access/v1", **value}
    )[:32]
    try:
        source_store.grant_access(
            source_ref=source_ref,
            principal=principal,
            purpose=SOURCE_PURPOSE,
            access_mode="content",
            authorization_fingerprint=fingerprint,
            scope=value["scope"],
            trusted_service_id=TRUTH_SERVICE_SUBJECT,
            content_boundary=value["content_boundary"],
            binding_id=binding_id,
        )
        return
    except sqlite3.IntegrityError:
        # An exact replay reaches the same immutable binding.  Verify every
        # security-bearing field rather than treating occurrence as equality.
        pass

    with source_store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM source_access_bindings WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
    expected_scope = json.dumps(
        value["scope"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    expected_boundary = json.dumps(
        value["content_boundary"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_principal = canonical_json(principal.to_dict())
    if (
        row is None
        or str(row["authority_id"]) != source_ref.authority_id
        or str(row["source_item_id"]) != source_ref.item_id
        or str(row["principal_ref_json"]) != expected_principal
        or str(row["purpose"]) != SOURCE_PURPOSE
        or str(row["access_mode"]) != "content"
        or str(row["scope_json"]) != expected_scope
        or str(row["content_boundary_json"]) != expected_boundary
        or str(row["authorization_fingerprint"]) != fingerprint
        or row["revoked_at"] is not None
    ):
        raise InvariantViolation(
            "conversation Truth source access is absent, revoked, or conflicts"
        )


def _selector_for_content(
    content: str,
    selector: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if selector is not None:
        if not isinstance(selector, Mapping):
            raise InvariantViolation("selector must be a mapping")
        return dict(selector)
    return {
        "exact": content,
        "prefix": "",
        "suffix": "",
        "start": 0,
        "end": len(content),
    }


def propose_claim_from_conversation_message(
    truth_store: TruthStore,
    source_store: SourceStore,
    *,
    local_authority_actor: ActorRef,
    semantic_producer: ActorRef,
    producer_meta: Mapping[str, Any],
    conversation_id: str,
    message_id: str,
    proposition: str,
    claim_kind: str,
    idempotency_key: str,
    selector: Mapping[str, Any] | None = None,
    evidential_effect: str = "supports",
    derivation_relationship: str = "paraphrase",
    structured: Mapping[str, Any] | None = None,
    scope: str = "store",
    valid_from: str | None = None,
    valid_to: str | None = None,
    confidence_extraction: float | None = None,
    relation_diagnostics: Mapping[str, Any] | None = None,
    run_ref: str | None = None,
) -> ConversationClaimProposalResult:
    """Capture one exact message and atomically propose one supported claim."""

    if local_authority_actor.kind != "human":
        raise InvariantViolation("local identity authority is not enrolled")
    if semantic_producer.kind != "agent_run":
        raise InvariantViolation("conversation claim semantics require an agent run")
    if (
        semantic_producer.issuer_authority_id
        != local_authority_actor.issuer_authority_id
        or semantic_producer.tenant_scope_id
        != local_authority_actor.tenant_scope_id
    ):
        raise InvariantViolation("conversation claim actors cross authority boundaries")
    for label, value in (
        ("conversation_id", conversation_id),
        ("message_id", message_id),
        ("idempotency_key", idempotency_key),
    ):
        if not isinstance(value, str) or not value.strip():
            raise InvariantViolation(f"{label} must be a nonempty string")

    service_principal = ActorRef(
        issuer_authority_id=local_authority_actor.issuer_authority_id,
        subject=TRUTH_SERVICE_SUBJECT,
        kind="service",
        tenant_scope_id=local_authority_actor.tenant_scope_id,
    )
    applier = ActorRef(
        issuer_authority_id=local_authority_actor.issuer_authority_id,
        subject=TRUTH_KERNEL_SUBJECT,
        kind="system",
        tenant_scope_id=local_authority_actor.tenant_scope_id,
    )
    provider = ConversationMessageProvider(
        principal=service_principal,
        authorization_fingerprint=canonical_sha256(
            {
                "schema": "wb.conversation-source-authorization/v1",
                "principal": service_principal.to_dict(),
                "purpose": CAPTURE_PURPOSE,
            }
        ),
    )
    providers = ProviderRegistry()
    providers.register(provider)
    origin = conversation_origin(
        conversation_id=conversation_id,
        message_id=message_id,
    )
    source_ref = source_capture_from_origin(
        source_store,
        providers,
        provider_id=provider.provider_id,
        origin_ref=origin,
        principal=service_principal,
        purpose=CAPTURE_PURPOSE,
        tenant_scope_id=local_authority_actor.tenant_scope_id,
        originating_surface=ORIGINATING_SURFACE,
    )
    item = source_store.get_item(source_ref)
    if item is None:
        raise InvariantViolation("captured conversation source is unavailable")
    representation = source_store.get_representation(item.primary_representation_id)
    if representation is None or representation.source_ref != source_ref:
        raise InvariantViolation("captured conversation representation is unavailable")

    _ensure_truth_access(
        source_store,
        truth_store,
        source_ref=source_ref,
        representation_id=representation.representation_id,
        byte_length=representation.byte_length,
        principal=service_principal,
    )
    resolved = resolve_source(
        source_store,
        source_ref=source_ref,
        representation_id=representation.representation_id,
        principal=service_principal,
        purpose=SOURCE_PURPOSE,
    )
    try:
        content = resolved.content.decode("utf-8")
    except UnicodeDecodeError as exc:  # provider contract defense in depth
        raise InvariantViolation("conversation message is not UTF-8 text") from exc

    actors = SourceClaimActors(
        semantic_producer=semantic_producer,
        selector=semantic_producer,
        candidate_preparer=semantic_producer,
        applier=applier,
        producer_meta=dict(producer_meta),
        run_ref=run_ref,
    )
    proposal = truth_claim_propose_from_source(
        truth_store,
        source_store,
        source_ref=source_ref,
        representation_id=representation.representation_id,
        expected_content_sha256=representation.content_sha256,
        expected_native_revision=item.native_revision,
        source_principal=service_principal,
        candidate=SourceClaimCandidate(
            proposition=proposition,
            claim_kind=claim_kind,
            selector=_selector_for_content(content, selector),
            evidential_effect=evidential_effect,
            derivation_relationship=derivation_relationship,
            structured=structured,
            scope=scope,
            valid_from=valid_from,
            valid_to=valid_to,
            confidence_extraction=confidence_extraction,
            relation_diagnostics=relation_diagnostics,
        ),
        actors=actors,
        idempotency_key=idempotency_key,
        # Deliberately no CandidateDecisionAuthorization: proposing is not a
        # human lifecycle decision and cannot imply confirmation.
        decision=None,
    )
    return ConversationClaimProposalResult(
        conversation_id=conversation_id,
        message_id=message_id,
        source_ref=source_ref,
        representation_id=representation.representation_id,
        native_revision=item.native_revision,
        semantic_producer=semantic_producer,
        proposal=proposal,
    )


__all__ = [
    "CAPTURE_PURPOSE",
    "ConversationClaimProposalResult",
    "ORIGINATING_SURFACE",
    "TRUTH_KERNEL_SUBJECT",
    "TRUTH_SERVICE_SUBJECT",
    "propose_claim_from_conversation_message",
]
