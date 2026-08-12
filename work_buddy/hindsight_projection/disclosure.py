"""Agent Execution-owned disclosure transport for Hindsight retain calls."""

from __future__ import annotations

import hashlib
from typing import Callable, Protocol

from work_buddy.agent_execution.disclosure import (
    DisclosureDirection,
    DisclosureGateway,
    DisclosureSelector,
    DisclosureState,
    SourceAcknowledgementState,
    create_source_bound_run,
)
from work_buddy.hindsight_projection.contracts import (
    DestinationObservationState,
    DestinationReceipt,
    DisclosureDeliveryReceipt,
    DisclosureReconciliation,
    ProjectionClaimSnapshot,
    ProjectionAuthorizationUnavailable,
    ProjectionDeliveryAmbiguous,
    ProjectionDeliveryNotStarted,
    ProjectionDestination,
    ProjectionEffect,
    ReconciliationState,
)
from work_buddy.sources.disclosure import SourcesDisclosureService
from work_buddy.sources.models import ActorRef


class CapturedSourceLifecycle(Protocol):
    def register(
        self,
        *,
        source_ref: str,
        representation_id: str,
        authorization_ref: str,
    ) -> None: ...

    def redact(
        self,
        source_ref: str,
        *,
        authorization_ref: str,
        reason_code: str,
    ) -> None: ...


class AgentExecutionProjectionDisclosure:
    """Capture exact derived bytes, write ahead, then call Hindsight once."""

    def __init__(
        self,
        *,
        gateway: DisclosureGateway,
        sources: SourcesDisclosureService,
        source_lifecycle: CapturedSourceLifecycle,
        recipient: str,
        provider_id: str,
        model_id: str,
        authorization_validator: Callable[[ProjectionEffect], None],
        producer: ActorRef | None = None,
    ) -> None:
        self.gateway = gateway
        self.sources = sources
        self.source_lifecycle = source_lifecycle
        self.recipient = recipient
        self.provider_id = provider_id
        self.model_id = model_id
        self.authorization_validator = authorization_validator
        self.producer = producer

    @staticmethod
    def _attempt_identity(effect_id: str, attempt_no: int) -> tuple[str, str]:
        digest = hashlib.sha256(
            f"{effect_id}\0{attempt_no}".encode("utf-8")
        ).hexdigest()
        return f"hindsight-{digest}", f"hindsight-worker-{digest}"

    def _run(self, effect: ProjectionEffect, attempt_no: int):
        run_id, worker_id = self._attempt_identity(effect.effect_id, attempt_no)
        return create_source_bound_run(
            self.gateway,
            run_id=run_id,
            worker_session_id=worker_id,
            recipient=self.recipient,
            provider_id=self.provider_id,
            model_id=self.model_id,
            authorization_ref=effect.spec.authorization_ref,
            purpose="truth_hindsight_projection",
        )

    def deliver(
        self,
        *,
        effect: ProjectionEffect,
        attempt_no: int,
        snapshot: ProjectionClaimSnapshot,
        destination: ProjectionDestination,
    ) -> DisclosureDeliveryReceipt:
        try:
            self.authorization_validator(effect)
        except Exception as exc:
            raise ProjectionAuthorizationUnavailable(
                "projection authorization is not active for this handoff"
            ) from exc
        run = self._run(effect, attempt_no)
        exact = snapshot.proposition_bytes
        try:
            captured = self.sources.capture_for_disclosure(
                exact_content=exact,
                source_role="derived_content",
                run_id=run.run_id,
                tool_call_id="hindsight-retain",
                idempotency_key=f"hindsight-source-{effect.effect_id}-{attempt_no}",
                direction=DisclosureDirection.INBOUND_TO_MODEL,
                purpose="truth_hindsight_projection",
                authorization_ref=effect.spec.authorization_ref,
                recipient=self.recipient,
                provider_id=self.provider_id,
                model_id=self.model_id,
                source_producer=self.producer,
            )
            self.source_lifecycle.register(
                source_ref=captured.source_ref,
                representation_id=captured.representation_id,
                authorization_ref=effect.spec.authorization_ref,
            )
        except Exception as exc:
            raise ProjectionDeliveryNotStarted(
                "projection source could not be prepared"
            ) from exc

        try:
            destination_receipt, entry = run.execute_resolved_inbound(
                tool_call_id="hindsight-retain",
                idempotency_key=f"hindsight-disclosure-{effect.effect_id}-{attempt_no}",
                source_ref=captured.source_ref,
                representation_id=captured.representation_id,
                selector=DisclosureSelector(kind="whole"),
                content_sha256=captured.content_sha256,
                byte_length=captured.byte_length,
                resolve_content=lambda: exact,
                handoff=lambda content: destination.upsert(snapshot, content),
                derivation_ref=None,
            )
        except Exception as exc:
            entries = self.gateway.store.list_entries(run.run_id)
            if not entries or not entries[-1].send_attempted:
                raise ProjectionDeliveryNotStarted(
                    "projection disclosure did not cross the send boundary"
                ) from exc
            raise ProjectionDeliveryAmbiguous(
                "projection disclosure outcome requires reconciliation"
            ) from exc

        expected_document_id = destination.document_id(
            effect.spec.claim_id,
            effect.spec.policy_id,
        )
        if destination_receipt.document_id != expected_document_id:
            raise ProjectionDeliveryAmbiguous(
                "destination acknowledged a different stable document"
            )
        binding = run.bind_output(
            output_ref=f"hindsight-document:{expected_document_id}",
            idempotency_key=f"hindsight-output-{effect.effect_id}-{attempt_no}",
        )
        return DisclosureDeliveryReceipt(
            destination=destination_receipt,
            captured_source_ref=captured.source_ref,
            captured_representation_id=captured.representation_id,
            content_sha256=captured.content_sha256,
            byte_length=captured.byte_length,
            disclosure_run_id=run.run_id,
            disclosure_entry_id=entry.id,
            disclosure_manifest_sha256=binding.manifest_sha256,
        )

    def reconcile(
        self,
        *,
        effect: ProjectionEffect,
        attempt_no: int,
        snapshot: ProjectionClaimSnapshot,
        destination: ProjectionDestination,
    ) -> DisclosureReconciliation:
        run = self._run(effect, attempt_no)
        entries = self.gateway.store.list_entries(run.run_id)
        if not entries:
            return DisclosureReconciliation(state=ReconciliationState.NOT_STARTED)
        entry = entries[-1]
        document_id = destination.document_id(effect.spec.claim_id, effect.spec.policy_id)
        observation = destination.inspect(document_id, snapshot.claim_generation)
        if observation.state is DestinationObservationState.PRESENT_MATCH:
            if entry.state is DisclosureState.POSSIBLY_SENT:
                entry = self.gateway.reconcile(
                    entry.id,
                    proven_outcome=DisclosureState.SENT,
                )
            elif (
                entry.state is DisclosureState.SENT
                and entry.source_acknowledgement
                is SourceAcknowledgementState.PENDING
            ):
                entry = self.gateway.reconcile_acknowledgement(entry.id)
            elif entry.state is DisclosureState.NOT_SENT:
                return DisclosureReconciliation(
                    state=ReconciliationState.NOT_STARTED
                )
            binding = run.bind_output(
                output_ref=f"hindsight-document:{document_id}",
                idempotency_key=f"hindsight-output-{effect.effect_id}-{attempt_no}",
            )
            return DisclosureReconciliation(
                state=ReconciliationState.APPLIED,
                receipt=DisclosureDeliveryReceipt(
                    destination=DestinationReceipt(
                        document_id=document_id,
                        claim_generation=snapshot.claim_generation,
                        acknowledged_at=observation.observed_at,
                    ),
                    captured_source_ref=entry.source_ref,
                    captured_representation_id=entry.representation_id,
                    content_sha256=entry.content_sha256,
                    byte_length=entry.byte_length,
                    disclosure_run_id=run.run_id,
                    disclosure_entry_id=entry.id,
                    disclosure_manifest_sha256=binding.manifest_sha256,
                ),
            )
        if entry.state is DisclosureState.NOT_SENT:
            return DisclosureReconciliation(state=ReconciliationState.NOT_STARTED)
        if entry.state is DisclosureState.SENT and observation.state in {
            DestinationObservationState.ABSENT,
            DestinationObservationState.PRESENT_OTHER,
        }:
            if entry.source_acknowledgement is SourceAcknowledgementState.PENDING:
                self.gateway.reconcile_acknowledgement(entry.id)
            return DisclosureReconciliation(
                state=ReconciliationState.SENT_DESTINATION_MISSING
            )
        return DisclosureReconciliation(state=ReconciliationState.AMBIGUOUS)

    def redact_captured_source(
        self,
        source_ref: str,
        *,
        authorization_ref: str,
        reason_code: str,
    ) -> None:
        self.source_lifecycle.redact(
            source_ref,
            authorization_ref=authorization_ref,
            reason_code=reason_code,
        )
