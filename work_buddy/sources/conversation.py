"""Exact provider for Work Buddy's own durable conversation messages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from work_buddy.conversations import store as conversation_store
from work_buddy.sources.errors import (
    InvalidSourceRequest,
    SourceAccessDenied,
    SourceNotFound,
    SourceOriginMismatch,
)
from work_buddy.sources.models import (
    ActorRef,
    AttributionAssertion,
    OriginRef,
    canonical_sha256,
    sha256_bytes,
    utc_now,
)
from work_buddy.sources.providers import NativeCapture, NativeObservation
from work_buddy.security.local_identity import (
    HUMAN_AUTHORITY_ASSURANCE,
    HUMAN_AUTHORITY_BASIS,
    HUMAN_INPUT_INGRESS_SCHEMA,
)


PROVIDER_ID = "work-buddy-conversation"
_ALLOWED_PURPOSES = frozenset(
    {
        "truth_evidence",
        "truth_analysis",
        "recheck",
        "cowork_document_agent",
        "dashboard.assisted_draft",
    }
)


@dataclass(frozen=True, slots=True)
class ConversationMessageProvider:
    """Resolve one immutable message occurrence from the conversation store.

    The provider is bound to one trusted service principal at construction.
    Possessing a message ID or an ``OriginRef`` is not authorization.
    """

    principal: ActorRef
    authorization_fingerprint: str
    provider_id: str = PROVIDER_ID
    version: str = "1"
    stable_occurrence_identity: bool = True

    def __post_init__(self) -> None:
        if len(self.authorization_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.authorization_fingerprint
        ):
            raise InvalidSourceRequest()

    def canonicalize_origin(self, origin_ref: OriginRef) -> OriginRef:
        if (
            origin_ref.provider_id != self.provider_id
            or not origin_ref.container_id
            or not origin_ref.native_item_id
            or origin_ref.part is not None
            or origin_ref.coordinates
        ):
            raise SourceOriginMismatch()
        for value in (origin_ref.container_id, origin_ref.native_item_id):
            if len(value) > 256 or any(ord(character) < 0x20 for character in value):
                raise SourceOriginMismatch()
        return OriginRef(
            provider_id=self.provider_id,
            container_id=origin_ref.container_id,
            native_item_id=origin_ref.native_item_id,
            revision=origin_ref.revision,
        )

    def authorize(
        self,
        origin_ref: OriginRef,
        principal: ActorRef,
        purpose: str,
    ) -> bool:
        del origin_ref
        return principal == self.principal and purpose in _ALLOWED_PURPOSES

    def capture(self, origin_ref: OriginRef, purpose: str) -> NativeCapture:
        if purpose not in _ALLOWED_PURPOSES:
            raise SourceAccessDenied()
        row = self._row(origin_ref)
        content = str(row["content"]).encode("utf-8")
        revision = _revision(row)
        if origin_ref.revision is not None and origin_ref.revision != revision:
            raise SourceOriginMismatch()
        return NativeCapture(
            exact_content=content,
            media_type="text/markdown",
            representation_kind="decoded_text",
            encoding="utf-8",
            source_role="conversation_message",
            fidelity="exact_native_message_content",
            native_revision=revision,
            occurred_at=str(row["created_at"]),
            observed_at=utc_now(),
            authorization_fingerprint=self.authorization_fingerprint,
            attributions=self._attributions(row),
            schema_type="wb.conversation-message/v1",
        )

    def observe(self, origin_ref: OriginRef) -> NativeObservation:
        try:
            row = self._row(origin_ref)
        except SourceNotFound:
            return NativeObservation(
                kind="origin_unavailable",
                status="unavailable",
                observed_at=utc_now(),
                error_code="conversation_message_unavailable",
            )
        content = str(row["content"]).encode("utf-8")
        revision = _revision(row)
        return NativeObservation(
            kind="origin_unchanged",
            status="ok",
            observed_at=utc_now(),
            native_revision=revision,
            native_content_sha256=sha256_bytes(content),
        )

    def _row(self, origin_ref: OriginRef) -> Mapping[str, Any]:
        canonical = self.canonicalize_origin(origin_ref)
        with conversation_store.get_connection() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ? AND conversation_id = ?",
                (canonical.native_item_id, canonical.container_id),
            ).fetchone()
        if row is None:
            raise SourceNotFound()
        return dict(row)

    def _attributions(
        self,
        row: Mapping[str, Any],
    ) -> tuple[AttributionAssertion, ...]:
        role = str(row.get("role") or "unknown")
        issuer = ActorRef(
            issuer_authority_id=self.principal.issuer_authority_id,
            subject="work-buddy-conversation-store",
            kind="service",
            tenant_scope_id=self.principal.tenant_scope_id,
        )
        assertions: list[AttributionAssertion] = [
            AttributionAssertion(
                role="issuer",
                actor=issuer,
                basis="native_conversation_store",
                assurance="trusted_component",
                asserted_by=issuer,
            )
        ]
        producer = _object(row.get("producer"))
        ingress = _object(row.get("ingress_json"))
        if role == "user" and ingress is not None:
            try:
                inputter = ActorRef.from_dict(_object(ingress.get("inputter")) or {})
            except (TypeError, ValueError):
                inputter = None
            if (
                ingress.get("schema") == HUMAN_INPUT_INGRESS_SCHEMA
                and inputter is not None
                and inputter.kind == "human"
                and inputter.issuer_authority_id == self.principal.issuer_authority_id
                and inputter.tenant_scope_id == self.principal.tenant_scope_id
                and ingress.get("assurance") == HUMAN_AUTHORITY_ASSURANCE
                and ingress.get("basis") == HUMAN_AUTHORITY_BASIS
                and isinstance(ingress.get("gesture_id"), str)
                and bool(ingress.get("gesture_id"))
            ):
                assertions.append(
                    AttributionAssertion(
                        role="inputter",
                        actor=inputter,
                        basis=HUMAN_AUTHORITY_BASIS,
                        assurance=HUMAN_AUTHORITY_ASSURANCE,
                        asserted_by=issuer,
                    )
                )
        if role == "agent" and producer:
            agent = ActorRef(
                issuer_authority_id=self.principal.issuer_authority_id,
                subject=f"conversation-agent-{canonical_sha256(producer)[:32]}",
                kind="agent_run",
                tenant_scope_id=self.principal.tenant_scope_id,
            )
            assertions.append(
                AttributionAssertion(
                    role="author",
                    actor=agent,
                    basis="conversation_execution_producer",
                    assurance="native_record",
                    asserted_by=issuer,
                )
            )
        else:
            # A native role labels the provider envelope.  In particular,
            # legacy role=user is not evidence of who composed or submitted
            # the exact words.
            assertions.append(
                AttributionAssertion(
                    role="author",
                    actor=None,
                    state="unknown",
                    basis=f"conversation_role_{role}_is_not_authorship",
                    assurance="unknown",
                    asserted_by=issuer,
                )
            )
        return tuple(assertions)


def conversation_origin(*, conversation_id: str, message_id: str) -> OriginRef:
    """Construct the provider-native identity without resolving by text/order."""

    return OriginRef(
        provider_id=PROVIDER_ID,
        container_id=conversation_id,
        native_item_id=message_id,
    )


def _object(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _revision(row: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            "message_id": str(row["message_id"]),
            "conversation_id": str(row["conversation_id"]),
            "role": str(row["role"]),
            "content_sha256": sha256_bytes(str(row["content"]).encode("utf-8")),
            "created_at": str(row["created_at"]),
            "producer": _object(row.get("producer")),
        }
    )


__all__ = [
    "ConversationMessageProvider",
    "PROVIDER_ID",
    "conversation_origin",
]
