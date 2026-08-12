from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.agent_execution.disclosure import (
    DisclosureGateway,
    DisclosureManifestStore,
    DisclosureState,
)
from work_buddy.hindsight_projection.contracts import (
    DestinationObservation,
    DestinationObservationState,
    DestinationReceipt,
    ProjectionDeliveryAmbiguous,
    ReconciliationState,
)
from work_buddy.hindsight_projection.disclosure import (
    AgentExecutionProjectionDisclosure,
)
from work_buddy.hindsight_projection.sources_adapter import (
    CapturedProjectionSourceLifecycle,
)
from work_buddy.sources.disclosure import SourcesDisclosureService
from work_buddy.sources.store import SourceStore

from .conftest import NOW, make_snapshot, make_spec


class _Destination:
    def __init__(self, *, fail_after_write=False) -> None:
        self.fail_after_write = fail_after_write
        self.documents = {}
        self.calls = 0

    @staticmethod
    def document_id(claim_id, policy_id):
        return f"doc-{claim_id}-{policy_id}"

    def upsert(self, snapshot, exact_content):
        self.calls += 1
        document_id = self.document_id(snapshot.claim_id, snapshot.policy_id)
        self.documents[document_id] = (snapshot.claim_generation, exact_content)
        if self.fail_after_write:
            self.fail_after_write = False
            raise RuntimeError("unknown acknowledgement")
        return DestinationReceipt(document_id, snapshot.claim_generation, NOW)

    def inspect(self, document_id, expected_generation):
        value = self.documents.get(document_id)
        return DestinationObservation(
            DestinationObservationState.PRESENT_MATCH
            if value and value[0] == expected_generation
            else DestinationObservationState.ABSENT,
            document_id,
            value[0] if value else None,
            NOW,
        )

    def remove(self, document_id):
        self.documents.pop(document_id, None)
        return DestinationReceipt(document_id, "0" * 64, NOW)


@pytest.fixture
def disclosure_transport(tmp_path: Path):
    source_store = SourceStore.create(
        tmp_path / "sources",
        authority_id="authority1",
    )
    sources = SourcesDisclosureService(source_store, tenant_scope_id="tenant001")
    manifest = DisclosureManifestStore(tmp_path / "agent-execution.db")
    gateway = DisclosureGateway(manifest, sources)
    lifecycle = CapturedProjectionSourceLifecycle(
        source_store,
        actor=sources.issuer,
    )
    transport = AgentExecutionProjectionDisclosure(
        gateway=gateway,
        sources=sources,
        source_lifecycle=lifecycle,
        recipient="hindsight-local-service",
        provider_id="anthropic",
        model_id="configured-hindsight-model",
        authorization_validator=lambda _effect: None,
        producer=sources.issuer,
    )
    return transport, manifest, source_store


def test_exact_proposition_crosses_agent_execution_manifest_content_free(
    projection_store,
    disclosure_transport,
) -> None:
    transport, manifest, _source_store = disclosure_transport
    spec = make_spec()
    effect = projection_store.enqueue(spec)
    snapshot = make_snapshot(spec)
    destination = _Destination()

    receipt = transport.deliver(
        effect=effect,
        attempt_no=1,
        snapshot=snapshot,
        destination=destination,
    )

    entries = manifest.list_entries(receipt.disclosure_run_id)
    assert len(entries) == 1
    assert entries[0].state is DisclosureState.SENT
    assert entries[0].content_sha256 == snapshot.proposition_sha256
    assert entries[0].byte_length == len(snapshot.proposition_bytes)
    assert snapshot.proposition_bytes not in manifest.db_path.read_bytes()
    assert destination.documents[receipt.destination.document_id][1] == (
        snapshot.proposition_bytes
    )


def test_authorization_is_checked_before_capture_or_destination_send(
    projection_store,
    disclosure_transport,
) -> None:
    from work_buddy.hindsight_projection.contracts import (
        ProjectionAuthorizationUnavailable,
    )

    transport, manifest, _source_store = disclosure_transport
    spec = make_spec()
    effect = projection_store.enqueue(spec)
    snapshot = make_snapshot(spec)
    destination = _Destination()
    transport.authorization_validator = lambda _effect: (_ for _ in ()).throw(
        RuntimeError("revoked")
    )
    with pytest.raises(ProjectionAuthorizationUnavailable):
        transport.deliver(
            effect=effect,
            attempt_no=1,
            snapshot=snapshot,
            destination=destination,
        )
    run_id, _worker_id = transport._attempt_identity(effect.effect_id, 1)
    assert manifest.list_entries(run_id) == ()
    assert destination.calls == 0


def test_ambiguous_hindsight_ack_is_proven_by_destination_without_replay(
    projection_store,
    disclosure_transport,
) -> None:
    transport, manifest, _source_store = disclosure_transport
    spec = make_spec()
    effect = projection_store.enqueue(spec)
    snapshot = make_snapshot(spec)
    destination = _Destination(fail_after_write=True)

    with pytest.raises(ProjectionDeliveryAmbiguous):
        transport.deliver(
            effect=effect,
            attempt_no=1,
            snapshot=snapshot,
            destination=destination,
        )
    assert destination.calls == 1
    run_id, _worker = transport._attempt_identity(effect.effect_id, 1)
    assert manifest.list_entries(run_id)[0].state is DisclosureState.POSSIBLY_SENT

    # Calling delivery again cannot invoke Hindsight again.  Reconciliation
    # observes the stable generation and promotes the existing manifest entry.
    with pytest.raises(ProjectionDeliveryAmbiguous):
        transport.deliver(
            effect=effect,
            attempt_no=1,
            snapshot=snapshot,
            destination=destination,
        )
    assert destination.calls == 1
    reconciled = transport.reconcile(
        effect=effect,
        attempt_no=1,
        snapshot=snapshot,
        destination=destination,
    )
    assert reconciled.state is ReconciliationState.APPLIED
    assert manifest.list_entries(run_id)[0].state is DisclosureState.SENT
