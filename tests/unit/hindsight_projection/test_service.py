from __future__ import annotations

import hashlib

import pytest

from work_buddy.hindsight_projection.contracts import (
    DependencyUsage,
    DesiredProjectionState,
    DestinationObservation,
    DestinationObservationState,
    DestinationReceipt,
    DisclosureDeliveryReceipt,
    DisclosureReconciliation,
    ProjectionDeliveryAmbiguous,
    ProjectionAuthorizationUnavailable,
    ReceiptState,
    ReconciliationState,
    ProjectionIneligible,
)
from work_buddy.hindsight_projection.service import TruthHindsightProjectionService

from .conftest import NOW, digest, make_snapshot, make_spec


class _Truth:
    def __init__(self, spec, snapshot) -> None:
        self.spec = spec
        self.snapshot = snapshot

    def desired_for_claim(self, claim_id, policy_id, *, at):
        assert (claim_id, policy_id) == (self.spec.claim_id, self.spec.policy_id)
        return self.spec

    def resolve_snapshot(self, intent, *, at):
        return self.snapshot

    def iter_desired(self, *, at):
        return (self.spec,)


class _Destination:
    def __init__(self) -> None:
        self.documents: dict[str, tuple[str, bytes]] = {}
        self.upsert_calls = 0
        self.remove_calls = 0

    @staticmethod
    def document_id(claim_id, policy_id):
        return f"doc-{claim_id}-{policy_id}"

    def upsert(self, snapshot, exact_content):
        self.upsert_calls += 1
        assert exact_content == snapshot.proposition_bytes
        document_id = self.document_id(snapshot.claim_id, snapshot.policy_id)
        self.documents[document_id] = (snapshot.claim_generation, exact_content)
        return DestinationReceipt(document_id, snapshot.claim_generation, NOW)

    def inspect(self, document_id, expected_generation):
        current = self.documents.get(document_id)
        if current is None:
            state = DestinationObservationState.ABSENT
            generation = None
        elif current[0] == expected_generation:
            state = DestinationObservationState.PRESENT_MATCH
            generation = current[0]
        else:
            state = DestinationObservationState.PRESENT_OTHER
            generation = current[0]
        return DestinationObservation(state, document_id, generation, NOW)

    def remove(self, document_id):
        self.remove_calls += 1
        self.documents.pop(document_id, None)
        return DestinationReceipt(document_id, "0" * 64, NOW)


class _Dependencies:
    def __init__(self) -> None:
        self.acknowledged: list[str] = []
        self.released: list[str] = []

    def reserve(self, *, effect, attempt_no, snapshot):
        return tuple(
            DependencyUsage(
                usage_id="usage-" + hashlib.sha256(
                    (effect.effect_id + str(attempt_no) + dep.source_ref).encode()
                ).hexdigest()[:24],
                source_ref=dep.source_ref,
                representation_id=dep.representation_id,
                redaction_epoch=3,
            )
            for dep in snapshot.source_dependencies
        )

    def acknowledge(self, usages):
        self.acknowledged.extend(item.usage_id for item in usages)

    def release(self, usages):
        self.released.extend(item.usage_id for item in usages)


class _Disclosure:
    def __init__(self, *, ambiguous_once=False) -> None:
        self.ambiguous_once = ambiguous_once
        self.deliver_calls = 0
        self.reconcile_calls = 0
        self.redacted: list[str] = []

    @staticmethod
    def _receipt(effect, snapshot, destination, destination_receipt):
        return DisclosureDeliveryReceipt(
            destination=destination_receipt,
            captured_source_ref="wb-source://authority1/item/derived01",
            captured_representation_id="representation-derived01",
            content_sha256=snapshot.proposition_sha256,
            byte_length=len(snapshot.proposition_bytes),
            disclosure_run_id="run-0001",
            disclosure_entry_id="entry-0001",
            disclosure_manifest_sha256=digest("manifest"),
        )

    def deliver(self, *, effect, attempt_no, snapshot, destination):
        self.deliver_calls += 1
        destination_receipt = destination.upsert(snapshot, snapshot.proposition_bytes)
        if self.ambiguous_once:
            self.ambiguous_once = False
            raise ProjectionDeliveryAmbiguous()
        return self._receipt(effect, snapshot, destination, destination_receipt)

    def reconcile(self, *, effect, attempt_no, snapshot, destination):
        self.reconcile_calls += 1
        document_id = destination.document_id(snapshot.claim_id, snapshot.policy_id)
        observation = destination.inspect(document_id, snapshot.claim_generation)
        if observation.state is DestinationObservationState.PRESENT_MATCH:
            destination_receipt = DestinationReceipt(
                document_id, snapshot.claim_generation, observation.observed_at
            )
            return DisclosureReconciliation(
                ReconciliationState.APPLIED,
                self._receipt(effect, snapshot, destination, destination_receipt),
            )
        return DisclosureReconciliation(ReconciliationState.AMBIGUOUS)

    def redact_captured_source(self, source_ref, *, authorization_ref, reason_code):
        self.redacted.append(source_ref)


def _service(projection_store, *, ambiguous=False):
    spec = make_spec()
    snapshot = make_snapshot(spec)
    truth = _Truth(spec, snapshot)
    destination = _Destination()
    dependencies = _Dependencies()
    disclosure = _Disclosure(ambiguous_once=ambiguous)
    service = TruthHindsightProjectionService(
        store=projection_store,
        truth=truth,
        destination=destination,
        disclosure=disclosure,
        dependencies=dependencies,
    )
    return service, truth, destination, dependencies, disclosure


def test_only_current_confirmed_snapshot_projects_with_portable_receipt(
    projection_store,
) -> None:
    service, truth, destination, dependencies, disclosure = _service(projection_store)
    effect = projection_store.enqueue(truth.spec)

    result = service.process_next(worker_id="worker-0001", at=NOW)

    assert result.state == "delivered"
    assert disclosure.deliver_calls == 1
    assert destination.documents[
        destination.document_id(truth.spec.claim_id, truth.spec.policy_id)
    ][1] == truth.snapshot.proposition_bytes
    receipt = projection_store.receipt(truth.spec.claim_id, truth.spec.policy_id)
    assert receipt is not None
    assert receipt.state is ReceiptState.PRESENT
    assert receipt.claim_generation == truth.spec.claim_generation
    assert receipt.lifecycle_status == "confirmed"
    assert receipt.applicability_scope == {"kind": "global"}
    assert receipt.projection_method == "hindsight_llm_retain_v1"
    assert receipt.captured_source_ref.startswith("wb-source://")
    assert dependencies.acknowledged
    assert truth.snapshot.proposition.encode() not in projection_store.db_path.read_bytes()


def test_challenge_removes_derivative_releases_dependencies_and_keeps_truth_authority(
    projection_store,
) -> None:
    service, truth, destination, dependencies, disclosure = _service(projection_store)
    projection_store.enqueue(truth.spec)
    assert service.process_next(worker_id="worker-0001", at=NOW).state == "delivered"

    remove = make_spec(
        generation=digest("generation-challenged"),
        desired=DesiredProjectionState.REMOVE,
        reason="claim_challenged",
    )
    truth.spec = remove
    projection_store.enqueue(remove)
    result = service.process_next(worker_id="worker-0001", at=NOW)

    assert result.state == "delivered"
    assert destination.documents == {}
    receipt = projection_store.receipt(remove.claim_id, remove.policy_id)
    assert receipt is not None and receipt.state is ReceiptState.ABSENT
    assert dependencies.released
    assert disclosure.redacted == []


def test_redaction_removal_durably_cleans_derived_projection_source(
    projection_store,
) -> None:
    service, truth, _destination, _dependencies, disclosure = _service(projection_store)
    projection_store.enqueue(truth.spec)
    service.process_next(worker_id="worker-0001", at=NOW)
    remove = make_spec(
        generation=digest("generation-redacted"),
        desired=DesiredProjectionState.REMOVE,
        reason="claim_redacted",
        purge=True,
    )
    truth.spec = remove
    projection_store.enqueue(remove)

    assert service.process_next(worker_id="worker-0001", at=NOW).state == "delivered"
    assert disclosure.redacted == ["wb-source://authority1/item/derived01"]
    assert projection_store.pending_source_cleanup() == ()


def test_ambiguous_ack_is_inspected_and_not_replayed(projection_store) -> None:
    service, truth, destination, _dependencies, disclosure = _service(
        projection_store, ambiguous=True
    )
    projection_store.enqueue(truth.spec)
    first = service.process_next(worker_id="worker-0001", at=NOW)
    assert first.state == "reconciling"
    assert disclosure.deliver_calls == 1

    second = service.process_next(worker_id="worker-0002", at=NOW)
    assert second.state == "delivered"
    assert disclosure.deliver_calls == 1
    assert disclosure.reconcile_calls == 1
    assert destination.upsert_calls == 1


def test_revoked_authorization_pauses_without_destination_send(projection_store) -> None:
    service, truth, destination, dependencies, disclosure = _service(projection_store)
    projection_store.enqueue(truth.spec)

    def denied(**_kwargs):
        raise ProjectionAuthorizationUnavailable()

    disclosure.deliver = denied
    result = service.process_next(worker_id="worker-authorization", at=NOW)
    assert result.state == "failed_terminal"
    assert result.error_code == "projection_authorization_unavailable"
    assert destination.upsert_calls == 0
    assert dependencies.released


def test_reconciliation_repairs_missed_wakeup_and_lifecycle_invalidation(
    projection_store,
) -> None:
    service, truth, _destination, _dependencies, _disclosure = _service(projection_store)
    report = service.reconcile_truth(at=NOW)
    assert report.enqueued == 1
    service.process_next(worker_id="worker-0001", at=NOW)

    truth.spec = make_spec(
        generation=digest("generation-expired"),
        desired=DesiredProjectionState.REMOVE,
        reason="claim_expired",
    )
    report = service.reconcile_truth(at=NOW)
    assert report.enqueued == 1
    assert service.process_next(worker_id="worker-0001", at=NOW).state == "delivered"


def test_reconciliation_repairs_a_missing_live_hindsight_projection(
    projection_store,
) -> None:
    service, truth, destination, _dependencies, disclosure = _service(projection_store)
    projection_store.enqueue(truth.spec)
    service.process_next(worker_id="worker-0001", at=NOW)
    destination.documents.clear()

    report = service.reconcile_truth(at=NOW)

    assert report.destination_repairs == 1
    assert service.process_next(worker_id="worker-0001", at=NOW).state == "delivered"
    assert disclosure.deliver_calls == 2


def test_source_redaction_usage_maps_back_through_truth_before_removal(
    projection_store,
) -> None:
    service, truth, _destination, _dependencies, _disclosure = _service(projection_store)
    projection_store.enqueue(truth.spec)
    service.process_next(worker_id="worker-0001", at=NOW)
    receipt = projection_store.receipt(truth.spec.claim_id, truth.spec.policy_id)
    assert receipt is not None and receipt.dependency_usages

    truth.spec = make_spec(
        generation=digest("generation-source-redacted"),
        desired=DesiredProjectionState.REMOVE,
        reason="source_redacted",
        purge=True,
    )
    desires = service.source_redaction_attention(
        receipt.dependency_usages[0].usage_id,
        at=NOW,
    )

    assert desires == (truth.spec,)
    assert service.process_next(worker_id="worker-0001", at=NOW).state == "delivered"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lifecycle_status", "proposed"),
        ("lifecycle_status", "challenged"),
        ("current", False),
        ("policy_eligible", False),
        ("source_state", "redacted"),
        ("valid_to", "2026-08-09T11:59:59.000Z"),
    ],
)
def test_projection_snapshot_fails_closed_when_not_current_confirmed_and_clean(
    field,
    value,
) -> None:
    spec = make_spec()
    snapshot = make_snapshot(spec)
    values = {
        name: getattr(snapshot, name)
        for name in snapshot.__dataclass_fields__
    }
    values[field] = value
    changed = snapshot.__class__(**values)
    with pytest.raises(ProjectionIneligible):
        changed.validate_for(spec, at=NOW)
