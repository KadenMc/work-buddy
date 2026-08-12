from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from work_buddy.agent_execution.disclosure import (
    DisclosureDirection,
    DisclosureGateway,
    DisclosureManifestStore,
    DisclosureReplayBlocked,
    DisclosureSourceError,
    DisclosureState,
    SourceAcknowledgementState,
)
from work_buddy.cowork import truth_analysis_runtime as runtime
from work_buddy.cowork import truth_analysis, truth_analysis_research
from work_buddy.cowork.truth_analysis_disclosure import (
    TruthAnalysisDisclosureBoundary,
    account_worker_context,
)
from work_buddy.sources.disclosure import SourcesDisclosureService
from work_buddy.sources.models import ActorRef, AttributionAssertion, SourceRef
from work_buddy.sources.redact import redact_source
from work_buddy.sources.store import SourceStore
from work_buddy.truth.identity import sha256_text
from work_buddy.websearch.models import SearchHit


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_DB_PATH", tmp_path / "truth-analysis.db")


def _run():
    created = runtime.create_run(
        run_id="a" * 32,
        store_id="store-alpha",
        document_id="document-alpha",
        action_snapshot_id="b" * 32,
        selection={
            "provider_id": "claude-code",
            "model_id": "sonnet",
            "provider_label": "Claude Code",
            "model_label": "Sonnet",
        },
        authorization_receipt_id="c" * 32,
        context_sha256="d" * 64,
        request={"schema": "wb.cowork.truth-analysis-request/v1"},
        session_id="truth-analysis-worker-1",
        at="2026-08-09T12:00:00+00:00",
    )
    return runtime.update_run(created.run_id, status="running", pid=42)


@pytest.fixture
def boundary(tmp_path: Path) -> TruthAnalysisDisclosureBoundary:
    sources_store = SourceStore.create(
        tmp_path / "sources",
        authority_id="source-authority-1",
    )
    sources = SourcesDisclosureService(
        sources_store,
        tenant_scope_id="tenant-scope-1",
    )
    manifests = DisclosureManifestStore(tmp_path / "agent-execution.db")
    return TruthAnalysisDisclosureBoundary(
        DisclosureGateway(manifests, sources),
        sources,
    )


def _source_attribution(
    boundary: TruthAnalysisDisclosureBoundary,
    *,
    source_ref: str,
    representation_id: str,
) -> tuple[tuple[AttributionAssertion, ...], ActorRef]:
    assert isinstance(boundary.sources, SourcesDisclosureService)
    store = boundary.sources.store
    parsed_ref = SourceRef.parse(source_ref)
    conn = store.connect()
    try:
        attributions = store.current_attributions(conn, parsed_ref)
        producer = ActorRef.from_dict(
            json.loads(
                store._representation_row(
                    conn,
                    parsed_ref,
                    representation_id,
                )["producer_ref_json"]
            )
        )
    finally:
        conn.close()
    return attributions, producer


def _origin_source(
    boundary: TruthAnalysisDisclosureBoundary,
    *,
    content: str = "private selected passage",
) -> SourceRef:
    assert isinstance(boundary.sources, SourcesDisclosureService)
    item = boundary.sources.store.capture_source(
        content=content,
        source_role="document_selection",
        tenant_scope_id=boundary.sources.tenant_scope_id,
        originating_surface="cowork_test",
        producer=boundary.sources.issuer,
    )
    return item.source_ref


def test_context_sources_are_separate_ordered_and_agent_db_has_no_raw_content(
    boundary: TruthAnalysisDisclosureBoundary,
) -> None:
    run = _run()
    context = {
        "target": {
            "kind": "text_quote",
            "text": "private selected passage",
            "text_sha256": "1" * 64,
        },
        "existing_truth": {
            "claims": [{"proposition": "private existing claim"}]
        },
    }

    origin_ref = _origin_source(boundary)
    account_worker_context(
        boundary,
        run,
        context,
        target_derivation_ref=origin_ref.uri,
    )

    entries = boundary.gateway.store.list_entries(run.run_id)
    assert [entry.sequence_no for entry in entries] == [1, 2]
    assert all(
        entry.direction is DisclosureDirection.INBOUND_TO_MODEL
        and entry.state is DisclosureState.POSSIBLY_SENT
        for entry in entries
    )
    assert boundary.manifest_digest(run).entry_count == 2
    manifest_bytes = boundary.gateway.store.db_path.read_bytes()
    assert b"private selected passage" not in manifest_bytes
    assert b"private existing claim" not in manifest_bytes

    selection_attributions, selection_producer = _source_attribution(
        boundary,
        source_ref=entries[0].source_ref,
        representation_id=entries[0].representation_id,
    )
    selection_author = next(
        item for item in selection_attributions if item.role == "author"
    )
    assert selection_author.state == "unknown"
    assert selection_author.actor is None
    assert selection_producer.kind == "service"
    assert all(
        item.actor is None or item.actor.kind != "agent_run"
        for item in selection_attributions
    )

    # A crash after the local handoff has an ambiguous delivery outcome. The
    # same sensitive response is never automatically returned again.
    with pytest.raises(DisclosureReplayBlocked):
        account_worker_context(
            boundary,
            run,
            context,
            target_derivation_ref=origin_ref.uri,
        )
    assert len(boundary.gateway.store.list_entries(run.run_id)) == 2

    boundary.bind_output(
        run,
        output_ref="truth-analysis-output:context-received",
        idempotency_key="truth-analysis-output:context-received",
    )
    acknowledged = boundary.gateway.store.list_entries(run.run_id)
    assert all(entry.state is DisclosureState.SENT for entry in acknowledged)
    assert all(
        entry.source_acknowledgement
        is SourceAcknowledgementState.ACKNOWLEDGED
        for entry in acknowledged
    )


@dataclass(frozen=True)
class _ProviderResult:
    external_egress: bool


def test_outbound_query_is_write_ahead_bound_to_inputs_and_output_is_bound(
    boundary: TruthAnalysisDisclosureBoundary,
) -> None:
    run = _run()
    boundary.account_inbound(
        run,
        payload={"target": "bounded source"},
        source_role="document_selection",
        tool_call_id="context-1",
        idempotency_key="context-disclosure-1",
    )
    observed: list[DisclosureState] = []

    def provider_call() -> _ProviderResult:
        observed.append(boundary.gateway.store.list_entries(run.run_id)[-1].state)
        return _ProviderResult(external_egress=True)

    result = boundary.execute_outbound(
        run,
        exact_content=b"model produced private query",
        source_role="agent_output",
        tool_call_id="search-1",
        idempotency_key="search-query-1",
        recipient="web_search_provider",
        provider_id="websearch",
        call=provider_call,
        external_egress=lambda item: item.external_egress,
    )

    entry = boundary.gateway.store.list_entries(run.run_id)[-1]
    assert result.external_egress is True
    assert observed == [DisclosureState.POSSIBLY_SENT]
    assert entry.direction is DisclosureDirection.OUTBOUND_TO_PROVIDER
    assert entry.state is DisclosureState.SENT
    assert entry.input_manifest_sha256 == boundary.gateway.store.input_manifest_digest(
        run.run_id
    ).manifest_sha256

    binding = boundary.bind_output(
        run,
        output_ref="truth-candidate:candidate-1",
        idempotency_key="truth-candidate-bind-1",
    )
    assert binding.manifest_sha256 == boundary.manifest_digest(run).manifest_sha256


def test_provider_disabled_is_reconciled_not_sent_and_ambiguous_call_never_replays(
    boundary: TruthAnalysisDisclosureBoundary,
) -> None:
    run = _run()
    disabled = boundary.execute_outbound(
        run,
        exact_content=b"query not released",
        source_role="agent_output",
        tool_call_id="search-disabled",
        idempotency_key="search-disabled-1",
        recipient="web_search_provider",
        provider_id="websearch",
        call=lambda: _ProviderResult(external_egress=False),
        external_egress=lambda item: item.external_egress,
    )
    assert disabled.external_egress is False
    first = boundary.gateway.store.list_entries(run.run_id)[0]
    assert first.state is DisclosureState.NOT_SENT
    assert first.send_attempted is True
    assert first.reconciled_at is not None

    calls = 0

    def uncertain() -> _ProviderResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider outcome unknown")

    with pytest.raises(RuntimeError, match="outcome unknown"):
        boundary.execute_outbound(
            run,
            exact_content=b"ambiguous query",
            source_role="agent_output",
            tool_call_id="search-ambiguous",
            idempotency_key="search-ambiguous-1",
            recipient="web_search_provider",
            provider_id="websearch",
            call=uncertain,
            external_egress=lambda item: item.external_egress,
        )
    with pytest.raises(DisclosureReplayBlocked):
        boundary.execute_outbound(
            run,
            exact_content=b"ambiguous query",
            source_role="agent_output",
            tool_call_id="search-ambiguous",
            idempotency_key="search-ambiguous-1",
            recipient="web_search_provider",
            provider_id="websearch",
            call=uncertain,
            external_egress=lambda item: item.external_egress,
        )
    assert calls == 1


def test_truth_search_and_fetch_account_both_provider_and_worker_directions(
    boundary: TruthAnalysisDisclosureBoundary,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    search_calls = 0

    def search_provider(query: str, **_kwargs: object) -> list[SearchHit]:
        nonlocal search_calls
        search_calls += 1
        assert query == "bounded evidence query"
        return [
            SearchHit(
                title="Admitted source",
                url="https://example.com/source",
                snippet="Bounded lead snippet",
                provider="fixture-search",
            )
        ]

    from work_buddy.websearch import router

    monkeypatch.setattr(router, "search", search_provider)
    search = truth_analysis.search_web(
        run_id=run.run_id,
        query="  bounded   evidence query ",
        agent_session_id=run.session_id,
        disclosure_boundary=boundary,
    )
    with pytest.raises(truth_analysis.TruthAnalysisError) as replay_error:
        truth_analysis.search_web(
            run_id=run.run_id,
            query="bounded evidence query",
            agent_session_id=run.session_id,
            disclosure_boundary=boundary,
        )

    assert search_calls == 1
    assert replay_error.value.code == "disclosure_idempotency_conflict"
    assert [entry.direction for entry in boundary.gateway.store.list_entries(run.run_id)] == [
        DisclosureDirection.OUTBOUND_TO_PROVIDER,
        DisclosureDirection.INBOUND_TO_MODEL,
    ]

    page_text = "Exact fetched evidence returned to the worker."
    monkeypatch.setattr(
        truth_analysis_research,
        "_fetch_public_text",
        lambda url, **_kwargs: truth_analysis_research._FetchedPage(
            requested_url=url,
            source_url=url,
            text=page_text,
            media_type="text/plain",
            http_status=200,
            extractor="fixture",
            redirect_chain=(),
            bytes_received=len(page_text.encode("utf-8")),
            extracted_text_bytes=len(page_text.encode("utf-8")),
            captured_text_bytes=len(page_text.encode("utf-8")),
            full_text_sha256=sha256_text(page_text),
            text_truncated=False,
        ),
    )
    fetched = truth_analysis.fetch_search_hit(
        run_id=run.run_id,
        hit_id=search["hits"][0]["hit_id"],
        agent_session_id=run.session_id,
        disclosure_boundary=boundary,
    )

    assert fetched["exact_text"] == page_text
    entries = boundary.gateway.store.list_entries(run.run_id)
    assert [entry.direction for entry in entries] == [
        DisclosureDirection.OUTBOUND_TO_PROVIDER,
        DisclosureDirection.INBOUND_TO_MODEL,
        DisclosureDirection.OUTBOUND_TO_PROVIDER,
        DisclosureDirection.INBOUND_TO_MODEL,
    ]
    boundary.bind_output(
        run,
        output_ref="truth-analysis-output:research-received",
        idempotency_key="truth-analysis-output:research-received",
    )
    entries = boundary.gateway.store.list_entries(run.run_id)
    assert all(entry.state is DisclosureState.SENT for entry in entries)
    assert page_text.encode("utf-8") not in boundary.gateway.store.db_path.read_bytes()
    fetched_attributions, fetched_producer = _source_attribution(
        boundary,
        source_ref=entries[-1].source_ref,
        representation_id=entries[-1].representation_id,
    )
    fetched_author = next(
        item for item in fetched_attributions if item.role == "author"
    )
    assert fetched_author.state == "unknown"
    assert fetched_author.actor is None
    assert fetched_producer.kind == "service"
    assert all(
        item.actor is None or item.actor.kind != "agent_run"
        for item in fetched_attributions
    )


def test_redacted_origin_invalidates_exact_context_copy_before_output(
    boundary: TruthAnalysisDisclosureBoundary,
) -> None:
    run = _run()
    origin_ref = _origin_source(boundary, content="erasure-sensitive passage")
    boundary.sources.store.grant_access(
        source_ref=origin_ref,
        principal=boundary.sources.issuer,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint="f" * 64,
    )
    boundary.account_inbound(
        run,
        payload={"text": "erasure-sensitive passage"},
        source_role="document_selection",
        tool_call_id="truth-analysis-job-get:target",
        idempotency_key="truth-analysis-context:redaction-race",
        derivation_ref=origin_ref.uri,
    )
    entry = boundary.gateway.store.list_entries(run.run_id)[0]
    derived_ref = SourceRef.parse(entry.source_ref)

    redact_source(
        boundary.sources.store,
        source_ref=origin_ref,
        actor=boundary.sources.issuer,
        authorization_fingerprint="f" * 64,
        reason_code="user_requested",
    )

    assert boundary.sources.store.get_item(derived_ref).lifecycle_state == "redacted"
    with pytest.raises(DisclosureSourceError):
        boundary.bind_output(
            run,
            output_ref="truth-analysis-output:must-not-bind",
            idempotency_key="truth-analysis-output:must-not-bind",
        )
