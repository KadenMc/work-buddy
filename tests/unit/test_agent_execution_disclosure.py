from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from work_buddy.agent_execution.disclosure import (
    DisclosureDirection,
    DisclosureGateway,
    DisclosureIdempotencyConflict,
    DisclosureManifestStore,
    DisclosurePreflight,
    DisclosureReplayBlocked,
    DisclosureReservationMismatch,
    DisclosureRunConflict,
    DisclosureSelector,
    DisclosureSourceError,
    DisclosureState,
    DisclosureStateConflict,
    DisclosureValidationError,
    SourceDisclosureReservation,
    candidate_manifest_digest,
    create_source_bound_run,
    outbound_preflight_with_current_inputs,
)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class _Sources:
    def __init__(self) -> None:
        self.reserve_calls: list[tuple[DisclosurePreflight, str]] = []
        self.ack_calls: list[tuple[str, str, DisclosureState, str]] = []
        self.fail_ack = False
        self.mismatch = False

    def reserve_disclosure(
        self,
        preflight: DisclosurePreflight,
        *,
        reservation_idempotency_key: str,
    ) -> SourceDisclosureReservation:
        self.reserve_calls.append((preflight, reservation_idempotency_key))
        return SourceDisclosureReservation(
            reservation_id=(
                "reservation:"
                + hashlib.sha256(
                    reservation_idempotency_key.encode("utf-8")
                ).hexdigest()[:16]
            ),
            redaction_epoch=7,
            content_sha256=("0" * 64 if self.mismatch else preflight.content_sha256),
            byte_length=preflight.byte_length,
        )

    def acknowledge_disclosure(
        self,
        *,
        reservation_id: str,
        manifest_entry_id: str,
        outcome: DisclosureState,
        acknowledgement_idempotency_key: str,
    ) -> None:
        self.ack_calls.append(
            (
                reservation_id,
                manifest_entry_id,
                outcome,
                acknowledgement_idempotency_key,
            )
        )
        if self.fail_ack:
            raise RuntimeError("private provider failure detail")


@pytest.fixture
def disclosure(tmp_path: Path) -> tuple[DisclosureManifestStore, _Sources, DisclosureGateway]:
    store = DisclosureManifestStore(tmp_path / "agent-execution.db")
    sources = _Sources()
    return store, sources, DisclosureGateway(store, sources)


def _preflight(
    *,
    content: bytes = b"source passage",
    run_id: str = "run-1",
    worker_session_id: str = "worker-1",
    tool_call_id: str = "truth-get-1",
    idempotency_key: str = "disclosure-1",
    direction: DisclosureDirection = DisclosureDirection.INBOUND_TO_MODEL,
    source_ref: str = "wb-source://authority/item-1",
    input_manifest_sha256: str | None = None,
) -> DisclosurePreflight:
    return DisclosurePreflight(
        run_id=run_id,
        worker_session_id=worker_session_id,
        tool_call_id=tool_call_id,
        idempotency_key=idempotency_key,
        direction=direction,
        source_ref=source_ref,
        representation_id="representation-1",
        selector=DisclosureSelector(
            kind="range",
            unit="utf8_byte",
            start=0,
            end=len(content),
            selector_sha256=_digest(b"selector coordinates"),
        ),
        content_sha256=_digest(content),
        byte_length=len(content),
        recipient="account-backed-model",
        provider_id="claude-code",
        model_id="sonnet",
        authorization_ref="authorization:truth-analysis",
        purpose="truth_analysis",
        input_manifest_sha256=input_manifest_sha256,
    )


def test_preflight_is_ordered_durable_idempotent_and_content_free(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, sources, gateway = disclosure
    private = b"do not persist this exact private sentence"
    first_request = _preflight(content=private)

    first = gateway.preflight(first_request)
    replay = gateway.preflight(first_request)
    second = gateway.preflight(
        _preflight(
            content=b"another passage",
            tool_call_id="truth-search-1",
            idempotency_key="disclosure-2",
            source_ref="wb-source://authority/item-2",
        )
    )

    assert first.id == replay.id
    assert first.sequence_no == 1
    assert second.sequence_no == 2
    assert len(sources.reserve_calls) == 2
    assert [entry.id for entry in store.list_entries("run-1")] == [
        first.id,
        second.id,
    ]

    # Reopen the service to prove the manifest is durable.  The raw bytes were
    # never accepted by the store/gateway and do not occur in the SQLite file.
    reopened = DisclosureManifestStore(store.db_path)
    assert reopened.get_entry(first.id) == first
    assert private not in store.db_path.read_bytes()


def test_idempotency_and_run_ownership_fail_closed(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    _store, sources, gateway = disclosure
    request = _preflight()
    gateway.preflight(request)

    with pytest.raises(DisclosureIdempotencyConflict):
        gateway.preflight(replace(request, source_ref="wb-source://authority/other"))
    assert len(sources.reserve_calls) == 1

    with pytest.raises(DisclosureRunConflict):
        gateway.preflight(
            _preflight(
                worker_session_id="different-worker",
                idempotency_key="disclosure-2",
            )
        )


def test_sources_must_reserve_the_exact_digest_and_bound(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, sources, gateway = disclosure
    sources.mismatch = True

    with pytest.raises(DisclosureReservationMismatch):
        gateway.preflight(_preflight())

    assert store.list_entries("run-1") == ()


def test_write_ahead_state_blocks_automatic_replay_after_crash(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, sources, gateway = disclosure
    calls = 0

    def crash_during_handoff() -> None:
        nonlocal calls
        calls += 1
        assert store.list_entries("run-1")[0].state is DisclosureState.POSSIBLY_SENT
        raise RuntimeError("transport result is unknown")

    with pytest.raises(RuntimeError, match="transport result is unknown"):
        gateway.execute_handoff(_preflight(), crash_during_handoff)

    entry = store.list_entries("run-1")[0]
    assert entry.state is DisclosureState.POSSIBLY_SENT
    assert entry.send_attempted is True
    assert store.list_recovery()[0].reason == "ambiguous_send"
    assert sources.ack_calls == []

    with pytest.raises(DisclosureReplayBlocked):
        gateway.execute_handoff(_preflight(), crash_during_handoff)
    assert calls == 1


def test_success_is_marked_sent_before_source_ack_and_ack_can_recover(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, sources, gateway = disclosure
    observed: list[DisclosureState] = []
    sources.fail_ack = True

    with pytest.raises(DisclosureSourceError):
        gateway.execute_handoff(
            _preflight(),
            lambda: observed.append(store.list_entries("run-1")[0].state),
        )

    entry = store.list_entries("run-1")[0]
    assert observed == [DisclosureState.POSSIBLY_SENT]
    assert entry.state is DisclosureState.SENT
    assert entry.source_ack_error_code == "source_ack_failed"
    assert store.list_recovery()[0].reason == "source_ack_pending"

    sources.fail_ack = False
    recovered = gateway.reconcile_acknowledgement(entry.id)
    assert recovered.state is DisclosureState.SENT
    assert recovered.source_acknowledgement.value == "acknowledged"
    assert store.list_recovery() == ()
    assert len(sources.ack_calls) == 2


def test_ambiguous_send_can_be_proven_not_sent_but_not_replayed(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, sources, gateway = disclosure
    entry = gateway.preflight(_preflight())
    gateway.mark_possibly_sent(entry.id)

    reconciled = gateway.reconcile(
        entry.id,
        proven_outcome=DisclosureState.NOT_SENT,
    )

    assert reconciled.state is DisclosureState.NOT_SENT
    assert reconciled.send_attempted is True
    assert reconciled.source_acknowledgement.value == "acknowledged"
    assert sources.ack_calls[-1][2] is DisclosureState.NOT_SENT
    with pytest.raises(DisclosureReplayBlocked):
        gateway.mark_possibly_sent(entry.id)
    assert store.list_recovery() == ()


def test_manifest_digest_and_output_binding_are_stable_across_acknowledgement(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, _sources, gateway = disclosure
    first = gateway.preflight(_preflight())
    assert candidate_manifest_digest(store, run_id="run-1")["entry_count"] == 0

    gateway.mark_possibly_sent(first.id)
    before_ack = store.manifest_digest("run-1")
    binding = store.bind_output_manifest(
        run_id="run-1",
        output_ref="truth-candidate:candidate-1",
        idempotency_key="bind-candidate-1",
    )
    gateway.mark_sent(first.id)
    after_ack = store.manifest_digest("run-1")

    assert before_ack == after_ack
    assert binding.manifest_sha256 == before_ack.manifest_sha256
    assert binding.entry_count == 1

    second = gateway.preflight(
        _preflight(
            content=b"later source",
            idempotency_key="disclosure-2",
            tool_call_id="truth-fetch-2",
            source_ref="wb-source://authority/item-2",
        )
    )
    gateway.mark_possibly_sent(second.id)
    assert store.manifest_digest("run-1").manifest_sha256 != binding.manifest_sha256
    assert (
        store.bind_output_manifest(
            run_id="run-1",
            output_ref="truth-candidate:candidate-1",
            idempotency_key="bind-candidate-1",
        )
        == binding
    )


def test_outbound_disclosure_is_bound_to_current_inbound_manifest(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, _sources, gateway = disclosure
    inbound = gateway.preflight(_preflight())
    gateway.mark_possibly_sent(inbound.id)
    gateway.mark_sent(inbound.id)

    incomplete = _preflight(
        content=b"model-produced search query",
        tool_call_id="web-search-1",
        idempotency_key="outbound-1",
        direction=DisclosureDirection.OUTBOUND_TO_PROVIDER,
        source_ref="wb-source://authority/derived-query-1",
    )
    with pytest.raises(DisclosureValidationError):
        gateway.preflight(incomplete)

    outbound = outbound_preflight_with_current_inputs(store, incomplete)
    entry = gateway.preflight(outbound)

    assert entry.direction is DisclosureDirection.OUTBOUND_TO_PROVIDER
    assert entry.input_manifest_sha256 == store.input_manifest_digest(
        "run-1"
    ).manifest_sha256


def test_selector_cannot_embed_exact_quote_text() -> None:
    with pytest.raises(TypeError):
        DisclosureSelector(  # type: ignore[call-arg]
            kind="quote",
            exact="private source bytes",
        )

    with pytest.raises(DisclosureValidationError):
        DisclosureSelector(kind="whole", selector_ref="quote:private")


def test_reconciliation_rejects_an_unproven_or_conflicting_outcome(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    _store, _sources, gateway = disclosure
    entry = gateway.preflight(_preflight())
    gateway.mark_possibly_sent(entry.id)

    with pytest.raises(DisclosureValidationError):
        gateway.reconcile(entry.id, proven_outcome=DisclosureState.POSSIBLY_SENT)
    sent = gateway.reconcile(entry.id, proven_outcome=DisclosureState.SENT)
    assert sent.state is DisclosureState.SENT
    with pytest.raises(DisclosureStateConflict):
        gateway.reconcile(entry.id, proven_outcome=DisclosureState.NOT_SENT)


def test_source_bound_run_exposes_the_domain_processor_vertical_slice(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, _sources, gateway = disclosure
    run = create_source_bound_run(
        gateway,
        run_id="journal-smart-run-1",
        worker_session_id="journal-smart-worker-1",
        recipient="account-backed-model",
        provider_id="claude-code",
        model_id="sonnet",
        authorization_ref="authorization:journal-smart",
        purpose="journal_smart_processing",
    )

    prepared = run.prepare_inbound_handoff(
        tool_call_id="journal-input-1",
        idempotency_key="journal-disclosure-1",
        source_ref="wb-source://authority/input-1",
        representation_id="representation-1",
        selector=DisclosureSelector(kind="whole"),
        content_sha256=_digest(b"captured elsewhere"),
        byte_length=len(b"captured elsewhere"),
    )
    assert prepared.state is DisclosureState.POSSIBLY_SENT

    sent = run.mark_sent(prepared.id)
    binding = run.bind_output(
        output_ref="journal-effect:effect-1",
        idempotency_key="journal-bind-output-1",
    )

    assert sent.state is DisclosureState.SENT
    assert binding.manifest_sha256 == run.digest().manifest_sha256
    reopened = store.get_run("journal-smart-run-1")
    assert reopened is not None
    assert reopened.run_id == run.manifest.run_id
    assert reopened.worker_session_id == run.manifest.worker_session_id


def test_source_bound_run_resolves_bytes_in_memory_without_manifest_storage(
    disclosure: tuple[DisclosureManifestStore, _Sources, DisclosureGateway],
) -> None:
    store, _sources, gateway = disclosure
    run = create_source_bound_run(
        gateway,
        run_id="journal-smart-run-2",
        worker_session_id="journal-smart-worker-2",
        recipient="account-backed-model",
        provider_id="claude-code",
        model_id="sonnet",
        authorization_ref="authorization:journal-smart",
        purpose="journal_smart_processing",
    )
    exact = b"private journal input resolved from Sources"
    observed: list[DisclosureState] = []

    result, entry = run.execute_resolved_inbound(
        tool_call_id="journal-input-2",
        idempotency_key="journal-disclosure-2",
        source_ref="wb-source://authority/input-2",
        representation_id="representation-2",
        selector=DisclosureSelector(kind="whole"),
        content_sha256=_digest(exact),
        byte_length=len(exact),
        resolve_content=lambda: exact,
        handoff=lambda content: (
            observed.append(store.list_entries(run.run_id)[0].state),
            content.decode("utf-8").upper(),
        )[1],
    )

    assert observed == [DisclosureState.POSSIBLY_SENT]
    assert entry.state is DisclosureState.SENT
    assert result == exact.decode("utf-8").upper()
    assert exact not in store.db_path.read_bytes()
