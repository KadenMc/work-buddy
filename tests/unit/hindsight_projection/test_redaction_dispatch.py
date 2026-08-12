from __future__ import annotations

from dataclasses import replace

from work_buddy.hindsight_projection.contracts import DesiredProjectionState
from work_buddy.hindsight_projection.redaction_dispatch import (
    HindsightProjectionRedactionDispatcher,
)
from work_buddy.security.actors import ActorRef
from work_buddy.sources.models import OutboxEffect, SourceRef

from .conftest import NOW, digest, make_spec
from .test_service import _service


class _Outbox:
    def __init__(self, effects: tuple[OutboxEffect, ...]) -> None:
        self.effects = effects
        self.completed: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str, bool]] = []

    def lease(self, worker_id, **kwargs):
        assert worker_id == "redaction-worker"
        assert kwargs["target_domain"] == "hindsight_projection"
        assert kwargs["effect_type"] == "source.redaction"
        effects, self.effects = self.effects, ()
        return effects

    def complete(self, effect_id, worker_id, *, result_ref, result_sha256):
        assert worker_id == "redaction-worker"
        assert len(result_sha256) == 64
        self.completed.append((effect_id, result_ref))

    def fail(self, effect_id, worker_id, *, error_code, retryable):
        assert worker_id == "redaction-worker"
        self.failed.append((effect_id, error_code, retryable))


def _effect(*, usage_id: str, source_ref: str) -> OutboxEffect:
    return OutboxEffect(
        effect_id="redaction-effect-0001",
        command_id=None,
        target_domain="hindsight_projection",
        effect_type="source.redaction",
        payload={
            "schema": "wb.source-redaction-effect/v1",
            "redaction_event_id": "source-redaction-event-0001",
            "source_ref": SourceRef.parse(source_ref).to_dict(),
            "usage_id": usage_id,
            "consumer_domain": "hindsight_projection",
            "consumer_id": "hindsight-consumer-0001",
            "redaction_policy": "invalidate",
            "redaction_epoch": 4,
        },
        payload_sha256=digest("redaction-payload"),
        authorization_fingerprint=digest("authorization"),
        authorization_expires_at=None,
        status="leased",
        attempts=1,
        lease_owner="redaction-worker",
        lease_until="2026-08-09T12:05:00.000Z",
        result_ref=None,
        error_code=None,
    )


def _dispatcher(projection_store, service, effect):
    dispatcher = HindsightProjectionRedactionDispatcher(
        sources=None,  # type: ignore[arg-type]
        truth_store=None,  # type: ignore[arg-type]
        projection_store=projection_store,
        projection_service=service,
        policy=object(),  # type: ignore[arg-type]
        actor=ActorRef("authority1", "redaction-service", "service", "tenant001"),
        worker_id="redaction-worker",
    )
    outbox = _Outbox((effect,))
    dispatcher.outbox = outbox  # type: ignore[assignment]
    return dispatcher, outbox


def test_redaction_effect_waits_for_truth_removal_and_dependency_cleanup(
    projection_store,
    monkeypatch,
) -> None:
    service, truth, _destination, _dependencies, _disclosure = _service(
        projection_store
    )
    projection_store.enqueue(truth.spec)
    assert service.process_next(worker_id="projection-worker", at=NOW).state == "delivered"
    receipt = projection_store.receipt(truth.spec.claim_id, truth.spec.policy_id)
    assert receipt is not None and len(receipt.dependency_usages) == 1
    usage = receipt.dependency_usages[0]
    effect = _effect(usage_id=usage.usage_id, source_ref=usage.source_ref)
    dispatcher, outbox = _dispatcher(projection_store, service, effect)

    observed: list[tuple[str, str, str]] = []

    def record_attention(_store, **kwargs):
        observed.append(
            (
                kwargs["claim_id"],
                kwargs["source_ref"],
                kwargs["redaction_event_id"],
            )
        )
        return truth.spec

    monkeypatch.setattr(
        "work_buddy.hindsight_projection.redaction_dispatch."
        "record_source_redaction_attention",
        record_attention,
    )
    truth.spec = make_spec(
        generation=digest("generation-source-redacted"),
        desired=DesiredProjectionState.REMOVE,
        reason="source_redacted",
        purge=True,
    )

    prepared = dispatcher.prepare()

    assert len(prepared) == 1
    assert observed == [
        (
            truth.spec.claim_id,
            usage.source_ref,
            "source-redaction-event-0001",
        )
    ]
    before = dispatcher.settle(prepared)
    assert before.deferred == 1
    assert outbox.failed == [
        ("redaction-effect-0001", "hindsight_redaction_pending", True)
    ]

    # A retry leases the same durable Sources effect after the ordinary Truth
    # projection worker has removed the semantic derivative and released its
    # exact dependency usage.
    assert service.process_next(worker_id="projection-worker", at=NOW).state == "delivered"
    retry_effect = replace(effect, attempts=2)
    retry_dispatcher, retry_outbox = _dispatcher(
        projection_store, service, retry_effect
    )
    retry_prepared = retry_dispatcher.prepare()
    after = retry_dispatcher.settle(retry_prepared)

    assert after.completed == 1
    assert retry_outbox.completed == [
        (
            "redaction-effect-0001",
            "hindsight-source-redaction:redaction-effect-0001",
        )
    ]


def test_invalid_redaction_payload_is_terminal_and_content_free(
    projection_store,
) -> None:
    service, _truth, _destination, _dependencies, _disclosure = _service(
        projection_store
    )
    effect = replace(
        _effect(
            usage_id="usage-redaction-0001",
            source_ref="wb-source://authority1/item/item0001",
        ),
        target_domain="another_domain",
    )
    dispatcher, outbox = _dispatcher(projection_store, service, effect)

    assert dispatcher.prepare() == ()
    assert outbox.failed == [
        ("redaction-effect-0001", "invalid_hindsight_projection", False)
    ]
    assert dispatcher.settle(()).failed == 1
