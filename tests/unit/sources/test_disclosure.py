from __future__ import annotations

import json

import pytest

from work_buddy.agent_execution.disclosure import (
    DisclosureDirection,
    DisclosurePreflight,
    DisclosureSelector,
    DisclosureState,
)
from work_buddy.sources import (
    ActorRef,
    AttributionAssertion,
    SourceRef,
    SourcesDisclosureService,
)
from work_buddy.sources.errors import SourceIdempotencyConflict


def test_dynamic_disclosure_capture_reserve_and_acknowledge(
    source_store, tenant_id: str
) -> None:
    issuer = ActorRef(
        source_store.authority_id,
        "agent-execution-service",
        "service",
        tenant_id,
    )
    adapter = SourcesDisclosureService(
        source_store, tenant_scope_id=tenant_id, issuer=issuer
    )
    captured = adapter.capture_for_disclosure(
        exact_content=b"outbound search query",
        source_role="agent_output",
        run_id="run-00000001",
        tool_call_id="tool-call-00000001",
        idempotency_key="capture-00000001",
        direction=DisclosureDirection.OUTBOUND_TO_PROVIDER,
        purpose="web_search",
        authorization_ref="authorization-00000001",
        recipient="search-provider",
        provider_id="search-provider",
        model_id="model-00000001",
    )
    repeated = adapter.capture_for_disclosure(
        exact_content=b"outbound search query",
        source_role="agent_output",
        run_id="run-00000001",
        tool_call_id="tool-call-00000001",
        idempotency_key="capture-00000001",
        direction=DisclosureDirection.OUTBOUND_TO_PROVIDER,
        purpose="web_search",
        authorization_ref="authorization-00000001",
        recipient="search-provider",
        provider_id="search-provider",
        model_id="model-00000001",
    )
    assert repeated == captured
    captured_ref = SourceRef.parse(captured.source_ref)
    conn = source_store.connect()
    try:
        attributions = source_store.current_attributions(conn, captured_ref)
        producer_row = source_store._representation_row(
            conn, captured_ref, captured.representation_id
        )
    finally:
        conn.close()
    authors = [item for item in attributions if item.role == "author"]
    assert len(authors) == 1
    assert authors[0].actor is not None
    assert authors[0].actor.kind == "agent_run"
    assert ActorRef.from_dict(json.loads(producer_row["producer_ref_json"])) == authors[
        0
    ].actor
    preflight = DisclosurePreflight(
        run_id="run-00000001",
        worker_session_id="worker-00000001",
        tool_call_id="tool-call-00000001",
        idempotency_key="manifest-00000001",
        direction=DisclosureDirection.OUTBOUND_TO_PROVIDER,
        source_ref=captured.source_ref,
        representation_id=captured.representation_id,
        selector=DisclosureSelector(kind="whole"),
        content_sha256=captured.content_sha256,
        byte_length=captured.byte_length,
        recipient="search-provider",
        provider_id="search-provider",
        model_id="model-00000001",
        authorization_ref="authorization-00000001",
        purpose="web_search",
    )
    reservation = adapter.reserve_disclosure(
        preflight, reservation_idempotency_key="reservation-00000001"
    )
    assert reservation.content_sha256 == captured.content_sha256
    adapter.acknowledge_disclosure(
        reservation_id=reservation.reservation_id,
        manifest_entry_id="manifest-entry-00000001",
        outcome=DisclosureState.SENT,
        acknowledgement_idempotency_key="acknowledgement-00000001",
    )
    adapter.acknowledge_disclosure(
        reservation_id=reservation.reservation_id,
        manifest_entry_id="manifest-entry-00000001",
        outcome=DisclosureState.SENT,
        acknowledgement_idempotency_key="acknowledgement-00000001",
    )
    with pytest.raises(SourceIdempotencyConflict):
        adapter.acknowledge_disclosure(
            reservation_id=reservation.reservation_id,
            manifest_entry_id="different-entry-00000001",
            outcome=DisclosureState.SENT,
            acknowledgement_idempotency_key="acknowledgement-00000001",
        )


def test_non_agent_capture_preserves_known_author_or_records_unknown(
    source_store, tenant_id: str, human: ActorRef
) -> None:
    adapter = SourcesDisclosureService(source_store, tenant_scope_id=tenant_id)
    selected = adapter.capture_for_disclosure(
        exact_content=b"selected document passage",
        source_role="document_selection",
        run_id="run-00000002",
        tool_call_id="selection-00000001",
        idempotency_key="capture-selection-00000001",
        direction=DisclosureDirection.INBOUND_TO_MODEL,
        purpose="document_review",
        authorization_ref="authorization-00000002",
        recipient="agent-model",
        provider_id="model-provider",
        model_id="model-00000002",
    )
    fetched = adapter.capture_for_disclosure(
        exact_content=b"fetched passage with known author",
        source_role="fetched_passage",
        run_id="run-00000002",
        tool_call_id="fetch-00000001",
        idempotency_key="capture-fetch-00000001",
        direction=DisclosureDirection.INBOUND_TO_MODEL,
        purpose="document_review",
        authorization_ref="authorization-00000002",
        recipient="agent-model",
        provider_id="model-provider",
        model_id="model-00000002",
        source_attributions=(
            AttributionAssertion(
                role="author",
                actor=human,
                basis="provider_metadata",
                assurance="provider_asserted",
                asserted_by=adapter.issuer,
            ),
        ),
    )

    conn = source_store.connect()
    try:
        selected_ref = SourceRef.parse(selected.source_ref)
        selected_attributions = source_store.current_attributions(conn, selected_ref)
        selected_producer = ActorRef.from_dict(
            json.loads(
                source_store._representation_row(
                    conn, selected_ref, selected.representation_id
                )["producer_ref_json"]
            )
        )
        fetched_ref = SourceRef.parse(fetched.source_ref)
        fetched_attributions = source_store.current_attributions(conn, fetched_ref)
        fetched_producer = ActorRef.from_dict(
            json.loads(
                source_store._representation_row(
                    conn, fetched_ref, fetched.representation_id
                )["producer_ref_json"]
            )
        )
    finally:
        conn.close()

    selected_author = next(
        item for item in selected_attributions if item.role == "author"
    )
    assert selected_author.state == "unknown"
    assert selected_author.actor is None
    assert selected_producer == adapter.issuer
    fetched_author = next(item for item in fetched_attributions if item.role == "author")
    assert fetched_author.actor == human
    assert fetched_producer == adapter.issuer
    assert all(
        item.actor is None or item.actor.kind != "agent_run"
        for item in (*selected_attributions, *fetched_attributions)
    )


def test_existing_journal_source_can_be_granted_then_reserved_without_recapture(
    source_store, tenant_id: str, human: ActorRef, service: ActorRef
) -> None:
    item = source_store.capture_source(
        content="journal entry already retained by Sources",
        source_role="human_input",
        tenant_scope_id=tenant_id,
        originating_surface="journal",
        attributions=(
            AttributionAssertion(
                role="author",
                actor=human,
                basis="direct_entry",
                assurance="authenticated_human",
                asserted_by=service,
            ),
        ),
        producer=service,
    )
    representation = source_store.get_representation(item.primary_representation_id)
    assert representation is not None
    conn = source_store.connect()
    try:
        source_count = conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0]
    finally:
        conn.close()

    adapter = SourcesDisclosureService(source_store, tenant_scope_id=tenant_id)
    binding = adapter.grant_existing_source_for_disclosure(
        source_ref=item.source_ref.uri,
        representation_id=representation.representation_id,
        run_id="journal-smart-run-00000001",
        direction=DisclosureDirection.INBOUND_TO_MODEL,
        purpose="journal_smart_processing",
        authorization_ref="journal-authorization-00000001",
        recipient="agent-model",
        provider_id="model-provider",
        model_id="model-00000003",
        tool_call_id="journal-smart-input-00000001",
    )
    assert binding.source_ref == item.source_ref
    assert binding.content_boundary == {
        "representation_id": representation.representation_id,
        "max_bytes": representation.byte_length,
    }
    assert binding.scope["provider_id"] == "model-provider"
    preflight = DisclosurePreflight(
        run_id="journal-smart-run-00000001",
        worker_session_id="journal-worker-00000001",
        tool_call_id="journal-smart-input-00000001",
        idempotency_key="journal-manifest-00000001",
        direction=DisclosureDirection.INBOUND_TO_MODEL,
        source_ref=item.source_ref.uri,
        representation_id=representation.representation_id,
        selector=DisclosureSelector(kind="whole"),
        content_sha256=representation.content_sha256,
        byte_length=representation.byte_length,
        recipient="agent-model",
        provider_id="model-provider",
        model_id="model-00000003",
        authorization_ref="journal-authorization-00000001",
        purpose="journal_smart_processing",
    )
    reservation = adapter.reserve_disclosure(
        preflight,
        reservation_idempotency_key="journal-reservation-00000001",
    )
    assert reservation.content_sha256 == representation.content_sha256
    conn = source_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == source_count
    finally:
        conn.close()
