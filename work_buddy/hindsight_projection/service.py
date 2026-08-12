"""Delivery and deterministic reconciliation for Truth-derived memory."""

from __future__ import annotations

from dataclasses import dataclass
from work_buddy.hindsight_projection.contracts import (
    DesiredProjectionState,
    DestinationObservationState,
    OutboxState,
    ProjectionConflict,
    ProjectionDeliveryAmbiguous,
    ProjectionAuthorizationUnavailable,
    ProjectionDeliveryNotStarted,
    ProjectionDependencyRegistry,
    ProjectionDisclosureTransport,
    ProjectionDestination,
    ProjectionEffect,
    ProjectionIneligible,
    ProjectionIntentSpec,
    ProjectionLease,
    ProjectionReceipt,
    ReceiptState,
    ReconciliationState,
    TruthProjectionReader,
    utc_now,
)
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore


@dataclass(frozen=True, slots=True)
class ProcessResult:
    state: str
    effect_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    enqueued: int
    unchanged: int
    destination_repairs: int
    dependency_repairs: int
    source_cleanups: int


class TruthHindsightProjectionService:
    """Consumes Truth-owned intents without treating Hindsight as authority."""

    def __init__(
        self,
        *,
        store: TruthHindsightProjectionStore,
        truth: TruthProjectionReader,
        destination: ProjectionDestination,
        disclosure: ProjectionDisclosureTransport,
        dependencies: ProjectionDependencyRegistry,
    ) -> None:
        self.store = store
        self.truth = truth
        self.destination = destination
        self.disclosure = disclosure
        self.dependencies = dependencies

    def process_next(
        self,
        *,
        worker_id: str,
        at: str | None = None,
        lease_seconds: int = 60,
    ) -> ProcessResult:
        moment = at or utc_now()
        lease = self.store.acquire_next(
            worker_id=worker_id,
            at=moment,
            lease_seconds=lease_seconds,
        )
        if lease is None:
            return ProcessResult(state="idle")

        effect = lease.effect
        try:
            current = self.truth.desired_for_claim(
                effect.spec.claim_id,
                effect.spec.policy_id,
                at=moment,
            )
        except Exception:
            self.store.mark_retryable(
                lease,
                error_code="truth_desired_state_unavailable",
            )
            return ProcessResult(
                state="failed_retryable",
                effect_id=effect.effect_id,
                error_code="truth_desired_state_unavailable",
            )

        if current.request_sha256 != effect.spec.request_sha256:
            self.store.enqueue(current)
            self._release_attempt_dependencies(lease)
            self.store.mark_superseded(lease)
            return ProcessResult(state="superseded", effect_id=effect.effect_id)

        if effect.spec.desired_state is DesiredProjectionState.REMOVE:
            return self._process_remove(lease, moment=moment)
        return self._process_upsert(lease, moment=moment)

    def _process_upsert(
        self,
        lease: ProjectionLease,
        *,
        moment: str,
    ) -> ProcessResult:
        effect = lease.effect
        try:
            snapshot = self.truth.resolve_snapshot(effect.spec, at=moment)
            snapshot.validate_for(effect.spec, at=moment)
        except ProjectionIneligible:
            return self._supersede_from_truth(lease, moment=moment)
        except Exception:
            self.store.mark_retryable(
                lease,
                error_code="truth_snapshot_unavailable",
            )
            return ProcessResult(
                state="failed_retryable",
                effect_id=effect.effect_id,
                error_code="truth_snapshot_unavailable",
            )

        usages = self.store.attempt_usages(lease)
        if not usages:
            try:
                usages = self.dependencies.reserve(
                    effect=effect,
                    attempt_no=lease.attempt_no,
                    snapshot=snapshot,
                )
                self.store.record_dependencies(lease, usages)
            except Exception:
                self.store.mark_retryable(
                    lease,
                    error_code="source_dependency_reservation_failed",
                )
                return ProcessResult(
                    state="failed_retryable",
                    effect_id=effect.effect_id,
                    error_code="source_dependency_reservation_failed",
                )

        if lease.reconcile_existing:
            try:
                reconciled = self.disclosure.reconcile(
                    effect=effect,
                    attempt_no=lease.attempt_no,
                    snapshot=snapshot,
                    destination=self.destination,
                )
            except Exception:
                self.store.mark_reconciling(
                    lease,
                    error_code="projection_reconciliation_unavailable",
                )
                return ProcessResult(
                    state="reconciling",
                    effect_id=effect.effect_id,
                    error_code="projection_reconciliation_unavailable",
                )
            if reconciled.state is ReconciliationState.APPLIED:
                assert reconciled.receipt is not None
                return self._complete_upsert(lease, snapshot, reconciled.receipt, usages)
            if reconciled.state is ReconciliationState.NOT_STARTED:
                self._release(usages)
                self.store.mark_retryable(
                    lease,
                    error_code="projection_delivery_not_started",
                )
                return ProcessResult(
                    state="failed_retryable",
                    effect_id=effect.effect_id,
                    error_code="projection_delivery_not_started",
                )
            if reconciled.state is ReconciliationState.SENT_DESTINATION_MISSING:
                # The prior disclosure is accounted for, but the replaceable
                # destination no longer has the desired derivative.  A new
                # attempt/run may safely re-establish it under the same durable
                # policy; the old Agent Execution entry itself is never replayed.
                self._release(usages)
                self.store.mark_retryable(
                    lease,
                    error_code="projection_destination_missing",
                )
                return ProcessResult(
                    state="failed_retryable",
                    effect_id=effect.effect_id,
                    error_code="projection_destination_missing",
                )
            self.store.mark_reconciling(
                lease,
                error_code="projection_delivery_ambiguous",
            )
            return ProcessResult(
                state="reconciling",
                effect_id=effect.effect_id,
                error_code="projection_delivery_ambiguous",
            )

        try:
            delivery = self.disclosure.deliver(
                effect=effect,
                attempt_no=lease.attempt_no,
                snapshot=snapshot,
                destination=self.destination,
            )
        except ProjectionAuthorizationUnavailable:
            self._release(usages)
            self.store.mark_terminal(
                lease,
                error_code="projection_authorization_unavailable",
            )
            return ProcessResult(
                state="failed_terminal",
                effect_id=effect.effect_id,
                error_code="projection_authorization_unavailable",
            )
        except ProjectionDeliveryNotStarted:
            self._release(usages)
            self.store.mark_retryable(
                lease,
                error_code="projection_delivery_not_started",
            )
            return ProcessResult(
                state="failed_retryable",
                effect_id=effect.effect_id,
                error_code="projection_delivery_not_started",
            )
        except ProjectionDeliveryAmbiguous:
            self.store.mark_reconciling(
                lease,
                error_code="projection_delivery_ambiguous",
            )
            return ProcessResult(
                state="reconciling",
                effect_id=effect.effect_id,
                error_code="projection_delivery_ambiguous",
            )
        except Exception:
            # Once the transport owns the call, an unclassified failure is
            # conservatively ambiguous rather than automatically replayed.
            self.store.mark_reconciling(
                lease,
                error_code="projection_transport_unknown",
            )
            return ProcessResult(
                state="reconciling",
                effect_id=effect.effect_id,
                error_code="projection_transport_unknown",
            )
        return self._complete_upsert(lease, snapshot, delivery, usages)

    def _complete_upsert(self, lease, snapshot, delivery, usages) -> ProcessResult:
        prior = self.store.complete_upsert(
            lease,
            snapshot=snapshot,
            delivery=delivery,
            dependency_usages=usages,
        )
        self._acknowledge(usages)
        if prior is not None and prior.state is ReceiptState.PRESENT:
            self._release(prior.dependency_usages)
        return ProcessResult(state="delivered", effect_id=lease.effect.effect_id)

    def _process_remove(
        self,
        lease: ProjectionLease,
        *,
        moment: str,
    ) -> ProcessResult:
        effect = lease.effect
        prior = self.store.receipt(effect.spec.claim_id, effect.spec.policy_id)
        document_id = (
            prior.document_id
            if prior is not None
            else self.destination.document_id(effect.spec.claim_id, effect.spec.policy_id)
        )
        try:
            observation = self.destination.inspect(
                document_id,
                effect.spec.claim_generation,
            )
        except Exception:
            observation = None

        if observation is not None and observation.state is DestinationObservationState.ABSENT:
            return self._complete_remove(
                lease,
                prior=prior,
                document_id=document_id,
                observed_at=observation.observed_at,
            )
        if lease.reconcile_existing and (
            observation is None
            or observation.state is DestinationObservationState.UNKNOWN
        ):
            self.store.mark_reconciling(
                lease,
                error_code="projection_removal_ambiguous",
            )
            return ProcessResult(
                state="reconciling",
                effect_id=effect.effect_id,
                error_code="projection_removal_ambiguous",
            )
        try:
            destination_receipt = self.destination.remove(document_id)
        except Exception:
            # DELETE is idempotent, but an unknown response is inspected before
            # another call so the worker does not churn a destination outage.
            self.store.mark_reconciling(
                lease,
                error_code="projection_removal_ambiguous",
            )
            return ProcessResult(
                state="reconciling",
                effect_id=effect.effect_id,
                error_code="projection_removal_ambiguous",
            )
        return self._complete_remove(
            lease,
            prior=prior,
            document_id=document_id,
            observed_at=destination_receipt.acknowledged_at,
        )

    def _complete_remove(
        self,
        lease: ProjectionLease,
        *,
        prior: ProjectionReceipt | None,
        document_id: str,
        observed_at: str,
    ) -> ProcessResult:
        persisted_prior = self.store.complete_remove(
            lease,
            document_id=document_id,
            observed_at=observed_at,
        )
        selected = persisted_prior or prior
        if selected is not None:
            self._release(selected.dependency_usages)
        self.reconcile_source_cleanup()
        return ProcessResult(state="delivered", effect_id=lease.effect.effect_id)

    def _supersede_from_truth(
        self, lease: ProjectionLease, *, moment: str
    ) -> ProcessResult:
        try:
            desired = self.truth.desired_for_claim(
                lease.effect.spec.claim_id,
                lease.effect.spec.policy_id,
                at=moment,
            )
            if desired.request_sha256 == lease.effect.spec.request_sha256:
                self.store.mark_terminal(lease, error_code="projection_ineligible")
                return ProcessResult(
                    state="failed_terminal",
                    effect_id=lease.effect.effect_id,
                    error_code="projection_ineligible",
                )
            self.store.enqueue(desired)
        except Exception:
            self.store.mark_retryable(
                lease,
                error_code="truth_desired_state_unavailable",
            )
            return ProcessResult(
                state="failed_retryable",
                effect_id=lease.effect.effect_id,
                error_code="truth_desired_state_unavailable",
            )
        self._release_attempt_dependencies(lease)
        self.store.mark_superseded(lease)
        return ProcessResult(state="superseded", effect_id=lease.effect.effect_id)

    def _release_attempt_dependencies(self, lease: ProjectionLease) -> None:
        try:
            self._release(self.store.attempt_usages(lease))
        except Exception:
            # The durable dependency row remains available to the accounting
            # reconciler; projection state must not be inferred from cleanup.
            return

    def _acknowledge(self, usages) -> None:
        if not usages:
            return
        try:
            self.dependencies.acknowledge(usages)
        except Exception:
            return
        self.store.mark_usages_acknowledged(usages)

    def _release(self, usages) -> None:
        if not usages:
            return
        try:
            self.dependencies.release(usages)
        except Exception:
            return
        self.store.mark_usages_released(usages)

    def reconcile_dependency_accounting(self) -> int:
        repaired = 0
        for usage, active in self.store.pending_dependency_accounting():
            try:
                if active:
                    self.dependencies.acknowledge((usage,))
                    self.store.mark_usages_acknowledged((usage,))
                else:
                    self.dependencies.release((usage,))
                    self.store.mark_usages_released((usage,))
            except Exception:
                continue
            repaired += 1
        return repaired

    def reconcile_source_cleanup(self) -> int:
        repaired = 0
        for cleanup_id, source_ref, authorization_ref, reason_code in (
            self.store.pending_source_cleanup()
        ):
            try:
                self.disclosure.redact_captured_source(
                    source_ref,
                    authorization_ref=authorization_ref,
                    reason_code=reason_code,
                )
            except Exception:
                continue
            self.store.complete_source_cleanup(cleanup_id)
            repaired += 1
        return repaired

    def reconcile_truth(self, *, at: str | None = None) -> ReconciliationReport:
        """Repair missed wake-ups from authoritative current Truth desires."""

        moment = at or utc_now()
        enqueued = 0
        unchanged = 0
        destination_repairs = 0
        seen: set[tuple[str, str]] = set()
        for desired in self.truth.iter_desired(at=moment):
            seen.add((desired.claim_id, desired.policy_id))
            current = self.store.current_effect(desired.claim_id, desired.policy_id)
            if current is not None and current.spec.request_sha256 == desired.request_sha256:
                unchanged += 1
                if (
                    desired.desired_state is DesiredProjectionState.UPSERT
                    and current.state is OutboxState.DELIVERED
                ):
                    receipt = self.store.receipt(desired.claim_id, desired.policy_id)
                    drifted = receipt is None or receipt.state is not ReceiptState.PRESENT
                    if not drifted and receipt is not None:
                        try:
                            observed = self.destination.inspect(
                                receipt.document_id,
                                desired.claim_generation,
                            )
                        except Exception:
                            observed = None
                        drifted = observed is not None and observed.state in {
                            DestinationObservationState.ABSENT,
                            DestinationObservationState.PRESENT_OTHER,
                        }
                    if drifted and self.store.requeue_delivered(
                        current.effect_id,
                        error_code="projection_destination_drift",
                        at=moment,
                    ):
                        destination_repairs += 1
                continue
            self.store.enqueue(desired)
            enqueued += 1

        # A live receipt that disappears from an enumeration still receives an
        # explicit Truth decision; omission alone is never interpreted as a
        # deletion command.
        for receipt in self.store.list_receipts():
            key = (receipt.claim_id, receipt.policy_id)
            if receipt.state is not ReceiptState.PRESENT or key in seen:
                continue
            desired = self.truth.desired_for_claim(
                receipt.claim_id,
                receipt.policy_id,
                at=moment,
            )
            current = self.store.current_effect(receipt.claim_id, receipt.policy_id)
            if current is not None and current.spec.request_sha256 == desired.request_sha256:
                unchanged += 1
                continue
            self.store.enqueue(desired)
            enqueued += 1

        return ReconciliationReport(
            enqueued=enqueued,
            unchanged=unchanged,
            destination_repairs=destination_repairs,
            dependency_repairs=self.reconcile_dependency_accounting(),
            source_cleanups=self.reconcile_source_cleanup(),
        )

    def source_redaction_attention(
        self,
        usage_id: str,
        *,
        at: str | None = None,
    ) -> tuple[ProjectionIntentSpec, ...]:
        """Map a Sources redaction usage to explicit current Truth desires.

        The method only enqueues what Truth says now.  It does not let a Sources
        event become claim lifecycle authority.
        """

        moment = at or utc_now()
        enqueued: list[ProjectionIntentSpec] = []
        for claim_id, policy_id in self.store.claims_for_usage(usage_id):
            desired = self.truth.desired_for_claim(claim_id, policy_id, at=moment)
            current = self.store.current_effect(claim_id, policy_id)
            if current is None or current.spec.request_sha256 != desired.request_sha256:
                self.store.enqueue(desired)
            enqueued.append(desired)
        return tuple(enqueued)
