"""Sources redaction-outbox consumer for Hindsight semantic derivatives."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from work_buddy.hindsight_projection.contracts import (
    ProjectionValidationError,
    utc_now,
)
from work_buddy.hindsight_projection.service import TruthHindsightProjectionService
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore
from work_buddy.hindsight_projection.truth_reader import (
    TruthHindsightProjectionPolicy,
    record_source_redaction_attention,
)
from work_buddy.security.actors import ActorRef
from work_buddy.sources.dispatch import SourceOutbox
from work_buddy.sources.models import OutboxEffect, SourceRef
from work_buddy.sources.store import SourceStore
from work_buddy.truth.store import TruthStore


@dataclass(frozen=True, slots=True)
class PreparedRedaction:
    effect: OutboxEffect
    claim_id: str
    policy_id: str
    usage_id: str


@dataclass(frozen=True, slots=True)
class RedactionDispatchReport:
    prepared: int = 0
    completed: int = 0
    deferred: int = 0
    failed: int = 0


class HindsightProjectionRedactionDispatcher:
    """Translate Sources invalidation into Truth source-attention state.

    A Sources redaction is authoritative only about the source. It appends a
    source-usage attention event, never a claim lifecycle decision. The
    ordinary Truth projection policy then derives the explicit document
    removal. The Sources effect completes only after Hindsight removal,
    dependency release, and generated-source cleanup are all durable.
    """

    def __init__(
        self,
        *,
        sources: SourceStore,
        truth_store: TruthStore,
        projection_store: TruthHindsightProjectionStore,
        projection_service: TruthHindsightProjectionService,
        policy: TruthHindsightProjectionPolicy,
        actor: ActorRef,
        worker_id: str,
    ) -> None:
        self.sources = sources
        self.truth_store = truth_store
        self.projection_store = projection_store
        self.projection_service = projection_service
        self.policy = policy
        self.actor = actor
        self.worker_id = worker_id
        self.outbox = SourceOutbox(sources)
        self._prepare_failed = 0

    @staticmethod
    def _payload(effect: OutboxEffect) -> tuple[str, str, str, int]:
        payload = effect.payload
        if (
            effect.target_domain != "hindsight_projection"
            or effect.effect_type != "source.redaction"
            or payload.get("schema") != "wb.source-redaction-effect/v1"
            or payload.get("consumer_domain") != "hindsight_projection"
            or payload.get("redaction_policy") != "invalidate"
        ):
            raise ProjectionValidationError("source redaction effect is invalid")
        usage_id = payload.get("usage_id")
        event_id = payload.get("redaction_event_id")
        epoch = payload.get("redaction_epoch")
        source_value = payload.get("source_ref")
        if not isinstance(usage_id, str) or not usage_id:
            raise ProjectionValidationError("source redaction usage_id is invalid")
        if not isinstance(event_id, str) or not event_id:
            raise ProjectionValidationError("source redaction event id is invalid")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ProjectionValidationError("source redaction epoch is invalid")
        if not isinstance(source_value, Mapping):
            raise ProjectionValidationError("source redaction reference is invalid")
        return usage_id, event_id, SourceRef.from_dict(source_value).uri, epoch

    def prepare(self, *, limit: int = 20) -> tuple[PreparedRedaction, ...]:
        self._prepare_failed = 0
        leased = self.outbox.lease(
            self.worker_id,
            limit=limit,
            lease_seconds=120,
            target_domain="hindsight_projection",
            effect_type="source.redaction",
        )
        prepared: list[PreparedRedaction] = []
        for effect in leased:
            try:
                usage_id, event_id, source_ref, epoch = self._payload(effect)
                targets = self.projection_store.dependency_targets_for_usage(usage_id)
                if not targets:
                    # Sources commits the reservation before Truth can record
                    # the cross-store usage receipt. A redaction racing that
                    # narrow window must remain retryable, never become a
                    # terminally dropped invalidation.
                    raise RuntimeError("projection dependency receipt is pending")
                if len(targets) != 1:
                    raise ProjectionValidationError(
                        "source redaction usage has multiple projection targets"
                    )
                claim_id, policy_id, expected_ref, representation_id, _active = targets[0]
                if source_ref != expected_ref:
                    raise ProjectionValidationError(
                        "source redaction reference does not match projection usage"
                    )
                if not self.projection_store.source_redaction_settled(
                    claim_id=claim_id,
                    policy_id=policy_id,
                    usage_id=usage_id,
                ):
                    record_source_redaction_attention(
                        self.truth_store,
                        claim_id=claim_id,
                        source_ref=source_ref,
                        representation_id=representation_id,
                        redaction_event_id=event_id,
                        redaction_epoch=epoch,
                        actor=self.actor,
                        at=utc_now(),
                        policy=self.policy,
                    )
                    # Enqueue the policy identity actually carried by the
                    # derivative usage as well as any current policy identity.
                    self.projection_service.source_redaction_attention(usage_id)
                prepared.append(
                    PreparedRedaction(effect, claim_id, policy_id, usage_id)
                )
            except ProjectionValidationError as exc:
                self._prepare_failed += 1
                self.outbox.fail(
                    effect.effect_id,
                    self.worker_id,
                    error_code=exc.error_code,
                    retryable=False,
                )
            except Exception:
                self._prepare_failed += 1
                self.outbox.fail(
                    effect.effect_id,
                    self.worker_id,
                    error_code="hindsight_redaction_prepare_failed",
                    retryable=True,
                )
        return tuple(prepared)

    def settle(
        self,
        prepared: tuple[PreparedRedaction, ...],
    ) -> RedactionDispatchReport:
        completed = deferred = 0
        for item in prepared:
            if self.projection_store.source_redaction_settled(
                claim_id=item.claim_id,
                policy_id=item.policy_id,
                usage_id=item.usage_id,
            ):
                result_ref = f"hindsight-source-redaction:{item.effect.effect_id}"
                self.outbox.complete(
                    item.effect.effect_id,
                    self.worker_id,
                    result_ref=result_ref,
                    result_sha256=hashlib.sha256(
                        result_ref.encode("utf-8")
                    ).hexdigest(),
                )
                completed += 1
            else:
                self.outbox.fail(
                    item.effect.effect_id,
                    self.worker_id,
                    error_code="hindsight_redaction_pending",
                    retryable=True,
                )
                deferred += 1
        return RedactionDispatchReport(
            prepared=len(prepared),
            completed=completed,
            deferred=deferred,
            failed=self._prepare_failed,
        )


__all__ = [
    "HindsightProjectionRedactionDispatcher",
    "PreparedRedaction",
    "RedactionDispatchReport",
]
