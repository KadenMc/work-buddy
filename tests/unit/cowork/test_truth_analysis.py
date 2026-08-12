from __future__ import annotations

import base64
import json

import pytest

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.cowork import (
    truth_analysis,
    truth_analysis_research,
    truth_analysis_runtime,
    truth_surface,
)
from work_buddy.cowork.proposal_applicability import CurrentProjection
from work_buddy.security.actors import ActorRef
from work_buddy.security.local_identity import (
    HUMAN_AUTHORITY_ASSURANCE,
    HUMAN_AUTHORITY_BASIS,
    HumanAuthorityContext,
    LocalPrincipal,
)
from work_buddy.sources import SourceStore
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import sha256_bytes, sha256_text
from work_buddy.truth.store import AcquisitionOrigin

from .conftest import AGENT, DOC_BODY, DOC_QUOTE, HUMAN, NOW


@pytest.fixture(autouse=True)
def _isolated_analysis_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        truth_analysis_runtime,
        "_DB_PATH",
        tmp_path / "truth-analysis-runtime.db",
    )


@pytest.fixture
def analysis_env(seeded, monkeypatch, tmp_path):
    monkeypatch.setattr(
        truth_analysis,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    source_store = SourceStore.create(
        tmp_path / "truth-analysis-sources",
        authority_id="truth-analysis-source-authority",
    )
    monkeypatch.setattr(
        truth_analysis,
        "_open_truth_source_store",
        lambda: source_store,
    )
    current_projection = {"text": DOC_BODY}

    def _load_current_projection(
        store,
        document,
        *,
        structured_head_sha256,
        conn=None,
    ):
        del conn
        text = current_projection["text"]
        return (
            CurrentProjection(
                text=text,
                projection_sha256=sha256_text(text),
                structured_head_sha256=structured_head_sha256,
                snapshot_sha256=document.ydoc_snapshot_sha256,
                generation_sha256=documents.current_ydoc_generation(
                    store, document.id
                ),
                binding_id="fixture-current-projection",
            ),
            "available",
        )

    monkeypatch.setattr(
        truth_surface,
        "load_current_projection",
        _load_current_projection,
    )
    seeded["current_projection"] = current_projection
    return seeded


def _commit(**kwargs):
    """Exercise the production exact-gesture boundary in service tests."""

    kwargs.pop("actor", None)
    run = truth_analysis_runtime.get_run(kwargs["run_id"])
    assert run is not None
    actor = ActorRef(
        issuer_authority_id="test-issuer-authority",
        subject="dashboard-user",
        kind="human",
        tenant_scope_id="test-tenant-scope",
    )
    edits = kwargs.get("edits")
    existing_claim_id = kwargs.get("existing_claim_id")
    subject = truth_analysis.candidate_decision_subject(
        kwargs["run_id"], kwargs["candidate_id"]
    )
    context_sha256 = truth_analysis.candidate_decision_context_sha256(
        store_id=run.store_id,
        document_id=run.document_id,
        run_id=run.run_id,
        candidate_id=kwargs["candidate_id"],
        expected_canonical_sha256=kwargs["expected_canonical_sha256"],
        decision=kwargs["decision"],
        existing_claim_id=existing_claim_id,
        edits=edits,
    )
    kwargs["authority_context"] = HumanAuthorityContext(
        principal=LocalPrincipal(
            actor=actor,
            session_id="test-browser-session",
            origin="http://127.0.0.1:5127",
            audience="work-buddy-dashboard",
            session_expires_at=9_999_999_999.0,
            rotation_due_at=9_999_999_000.0,
        ),
        action=truth_analysis.CANDIDATE_DECISION_ACTION,
        subject_sha256=sha256_text(subject),
        context_sha256=context_sha256,
        gesture_id=f"test-gesture-{kwargs['candidate_id']}",
        assurance=HUMAN_AUTHORITY_ASSURANCE,
        basis=HUMAN_AUTHORITY_BASIS,
    )
    return (truth_analysis.commit_candidate_decision)(**kwargs)


def _selection() -> AgentExecutionSelection:
    return AgentExecutionSelection(
        provider_id="claude-code",
        model_id="sonnet",
        provider_label="Claude Code",
        model_label="Sonnet",
    )


def _capture(seeded, *, capture_id: str = "truth-analysis-capture"):
    store = seeded["store"]
    document = seeded["document"]
    state_vector = b"truth-analysis-state-vector"
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    start = DOC_BODY.index(DOC_QUOTE)
    return {
        "schema": "wb.cowork.action-snapshot/v1",
        "captureId": capture_id,
        "storeId": store.store_id,
        "documentId": document.id,
        "capturedAt": NOW,
        "editGeneration": 1,
        "ydocGenerationSha256": documents.current_ydoc_generation(
            store, document.id
        ),
        "snapshotBase64": base64.b64encode(seeded["snapshot_bytes"]).decode(
            "ascii"
        ),
        "snapshotSha256": seeded["snapshot_sha256"],
        "stateVectorBase64": base64.b64encode(state_vector).decode("ascii"),
        "stateVectorSha256": sha256_bytes(state_vector),
        "structuredHeadSha256": head,
        "projectionMarkdown": DOC_BODY,
        "projectionSha256": sha256_text(DOC_BODY),
        "target": {
            "source": "current_selection",
            "label": "Selected passage",
            "wordCount": len(DOC_QUOTE.split()),
            "proseMirrorRange": None,
            "selector": {
                "kind": "text_quote",
                "exact": DOC_QUOTE,
                "prefix": DOC_BODY[max(0, start - 20) : start],
                "suffix": DOC_BODY[
                    start + len(DOC_QUOTE) : start + len(DOC_QUOTE) + 20
                ],
                "start": start,
                "end": start + len(DOC_QUOTE),
            },
            "targetTextSha256": sha256_text(DOC_QUOTE),
        },
    }


def _prepare(seeded):
    view = truth_analysis.prepare_analysis_run(
        seeded["store"],
        document_id=seeded["document"].id,
        capture=_capture(seeded),
        selection=_selection(),
        actor=HUMAN,
        selection_validator=lambda selection: selection,
    )
    run = truth_analysis_runtime.get_run(view["analysis_run_id"])
    assert run is not None
    return run


def _worker_context(run):
    return truth_analysis.get_worker_context(
        run_id=run.run_id,
        agent_session_id=run.session_id,
    )


def _candidate(
    proposition: str,
    *,
    evidence=None,
    existing_claim_match=None,
):
    return {
        "proposition": proposition,
        "claim_kind": "fact",
        "confidence_extraction": 0.91,
        "expression": {
            "role": "paraphrase",
            "selector": {
                "exact": DOC_QUOTE,
                "start": 0,
                "end": len(DOC_QUOTE),
            },
        },
        "existing_claim_match": existing_claim_match,
        "evidence": [] if evidence is None else evidence,
        "limitations": [],
    }


def _payload(context, candidates):
    return {
        "schema": truth_analysis.ANALYSIS_OUTPUT_SCHEMA,
        "summary": "Atomic claims identified in the selected passage.",
        "limitations": ["Web search was not admitted for this run."],
        "source_coverage": context["source_coverage"],
        "candidates": candidates,
    }


def _counts(store):
    with store._read_connection() as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "claims",
                "expressions",
                "claim_links",
                "evidence",
                "evidence_spans",
            )
        }


def _submit_and_view(store, run, payload):
    receipt = truth_analysis.submit_worker_output(
        run_id=run.run_id,
        agent_session_id=run.session_id,
        payload=payload,
    )
    assert receipt == {
        "ok": True,
        "schema": "wb.cowork.truth-analysis-submit-receipt/v1",
        "analysis_run_id": run.run_id,
        "status": "completed",
        "output_sha256": receipt["output_sha256"],
    }
    stored = truth_analysis_runtime.get_run(run.run_id)
    assert stored is not None
    return truth_analysis.analysis_run_view(stored, store=store)


def _stage_one(analysis_env, proposition, *, evidence=None):
    run = _prepare(analysis_env)
    context = _worker_context(run)
    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(
            context,
            [_candidate(proposition, evidence=evidence)],
        ),
    )
    return run, staged["candidates"][0]


def test_source_aware_submit_binds_every_candidate_to_complete_manifest(
    analysis_env,
    tmp_path,
):
    from work_buddy.agent_execution.disclosure import (
        DisclosureGateway,
        DisclosureManifestStore,
    )
    from work_buddy.cowork.truth_analysis_disclosure import (
        TruthAnalysisDisclosureBoundary,
    )
    from work_buddy.security.local_identity import get_default_authority
    from work_buddy.sources.disclosure import SourcesDisclosureService

    run = _prepare(analysis_env)
    sources_store = truth_analysis._open_truth_source_store()
    sources = SourcesDisclosureService(
        sources_store,
        tenant_scope_id=get_default_authority().enrolled_actor().tenant_scope_id,
    )
    manifests = DisclosureManifestStore(tmp_path / "agent-execution.db")
    boundary = TruthAnalysisDisclosureBoundary(
        DisclosureGateway(manifests, sources),
        sources,
    )
    context = truth_analysis.get_worker_context(
        run_id=run.run_id,
        agent_session_id=run.session_id,
        disclosure_boundary=boundary,
    )

    receipt = truth_analysis.submit_worker_output(
        run_id=run.run_id,
        agent_session_id=run.session_id,
        payload=_payload(
            context,
            [_candidate("AI assistance can reduce cognitive effort.")],
        ),
        disclosure_boundary=boundary,
    )

    stored = truth_analysis_runtime.get_run(run.run_id)
    assert stored is not None and stored.output is not None
    manifest = boundary.manifest_digest(run)
    assert receipt["input_manifest_sha256"] == manifest.manifest_sha256
    assert stored.output["input_manifest"] == manifest.to_dict()
    assert stored.output["candidates"][0]["input_manifest_sha256"] == (
        manifest.manifest_sha256
    )


def _existing_evidence(seeded):
    store = seeded["store"]
    claim = store.propose_claim(
        proposition="An existing source-backed claim.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    evidence_text = "The source reports a bounded supporting observation."
    evidence = store.capture_evidence(
        kind="document",
        source_locator="fixture://existing-source",
        actor=HUMAN,
        acquisition_method="paste",
        origin=AcquisitionOrigin.USER_INPUT,
        content=evidence_text,
        media_type="text/plain",
        acquired_at=NOW,
        created_at=NOW,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact=evidence_text),
        actor=HUMAN,
        created_at=NOW,
    )
    store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
        created_at=NOW,
    )
    return claim, evidence, span


def test_prepare_uses_exact_passage_and_context_hash_in_run_identity(analysis_env):
    first = _prepare(analysis_env)
    first_context = _worker_context(first)
    prepared_view = truth_analysis.analysis_run_view(first)
    truth_analysis_runtime.update_run(
        first.run_id,
        status="failed",
        error_code="superseded_fixture_run",
    )

    analysis_env["store"].propose_claim(
        proposition="Truth changed after the first frozen context.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    )
    second = _prepare(analysis_env)

    assert first_context["target"]["text"] == DOC_QUOTE
    assert first_context["target"]["text_sha256"] == sha256_text(DOC_QUOTE)
    assert first.action_snapshot_id == second.action_snapshot_id
    assert first.context_sha256 != second.context_sha256
    assert first.run_id != second.run_id
    assert prepared_view["target_choice"] == "current_selection"
    assert prepared_view["target_label"] == "Selected passage"
    assert prepared_view["captured_at"]
    assert prepared_view["structured_head_sha256"]
    assert prepared_view["projection_sha256"] == sha256_text(DOC_BODY)
    assert prepared_view["finished_at"] is None
    assert prepared_view["error"] is None
    web = next(
        item for item in first_context["source_coverage"] if item["source"] == "web"
    )
    assert web == {
        "source": "web",
        "status": "not_searched",
        "detail": "This staged run had no admitted web-search call.",
        "external_egress": False,
    }


def test_oversized_passage_is_rejected_before_model_authorization(
    analysis_env,
    monkeypatch,
):
    huge = "x" * (truth_analysis.MAX_SELECTED_PASSAGE_BYTES + 1)
    monkeypatch.setattr(
        truth_analysis,
        "action_snapshot_view",
        lambda *_args, **_kwargs: {
            "target": {
                "text": huge,
                "selector": {
                    "exact": huge,
                    "prefix": "",
                    "suffix": "",
                    "start": 0,
                    "end": len(huge),
                },
            },
            "frozen_markdown": huge,
        },
    )
    monkeypatch.setattr(
        truth_analysis,
        "record_model_call_authorization",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized target reached model authorization"
        ),
    )

    with pytest.raises(truth_analysis.TruthAnalysisError) as exc_info:
        truth_analysis.prepare_analysis_run(
            analysis_env["store"],
            document_id=analysis_env["document"].id,
            capture=_capture(analysis_env),
            selection=_selection(),
            actor=HUMAN,
            selection_validator=lambda value: value,
        )

    assert exc_info.value.code == "analysis_passage_too_large"
    assert exc_info.value.status == 413


def test_existing_truth_and_full_worker_context_are_byte_bounded(
    analysis_env,
    monkeypatch,
):
    claims = [
        {
            "claim_id": f"claim-{index:04d}",
            "proposition": f"{index}:" + ("z" * 5_000),
            "claim_kind": "fact",
            "canonical_sha256": f"{index:064x}",
            "scope": "store",
            "base_status": "proposed",
            "needs_review": False,
            "is_fact": False,
            "redacted": False,
        }
        for index in range(200)
    ]
    monkeypatch.setattr(
        truth_surface,
        "truth_list",
        lambda *_args, **_kwargs: {"claims": claims, "total": len(claims)},
    )
    bounded = truth_analysis._existing_truth_context(
        analysis_env["store"], analysis_env["document"]
    )
    context = truth_analysis._worker_context_contract(
        run_id="a" * 32,
        document_id=analysis_env["document"].id,
        target_text="x" * truth_analysis.MAX_SELECTED_PASSAGE_BYTES,
        target_sha256=sha256_text("x" * truth_analysis.MAX_SELECTED_PASSAGE_BYTES),
        target_selector={"exact": "x", "start": 0, "end": 1},
        existing_truth=bounded,
        source_coverage=truth_analysis._authoritative_source_coverage(bounded),
        allowed_claim_kinds=analysis_env["store"].profile.allowed_claim_kinds,
    )

    assert bounded["claim_total"] == 200
    assert bounded["claim_count_supplied"] < 200
    assert bounded["claims_truncated"] is True
    assert bounded["serialized_bytes"] == len(
        json.dumps(
            bounded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    assert bounded["serialized_bytes"] <= (
        truth_analysis.MAX_EXISTING_TRUTH_CONTEXT_BYTES
    )
    assert context["context_limits"]["serialized_bytes"] <= (
        truth_analysis.MAX_WORKER_CONTEXT_BYTES
    )
    assert context["existing_truth"]["claims_truncated"] is True


def test_failed_launch_never_claims_worker_searched_supplied_context(analysis_env):
    run = _prepare(analysis_env)
    failed = truth_analysis_runtime.update_run(
        run.run_id,
        status="failed",
        error_code="fixture_launch_failure",
        error="The worker never launched.",
    )
    view = truth_analysis.analysis_run_view(failed, store=analysis_env["store"])
    coverage = {item["source"]: item for item in view["source_coverage"]}

    assert coverage["selected_passage"]["status"] == "supplied"
    assert coverage["existing_truth"]["status"] == "supplied"
    assert coverage["web"]["status"] == "not_searched"
    assert all(item["external_egress"] is False for item in coverage.values())


def test_worker_output_contract_exposes_every_required_wire_shape(analysis_env):
    run = _prepare(analysis_env)
    context = _worker_context(run)
    contract = context["output_schema"]

    assert contract["schema_literal"] == truth_analysis.ANALYSIS_OUTPUT_SCHEMA
    assert set(contract["root"]["required"]) == {
        "schema",
        "source_coverage",
        "candidates",
    }
    assert {"summary", "limitations"} <= set(contract["root"]["optional"])
    coverage = contract["root"]["properties"]["source_coverage"]
    assert coverage["exact_sources"] == [
        "selected_passage",
        "existing_truth",
        "web",
    ]
    assert set(coverage["item"]["required"]) == {
        "source",
        "status",
        "detail",
        "external_egress",
    }
    assert set(coverage["item"]["properties"]["status"]["enum"]) == {
        "searched",
        "supplied",
        "partial",
        "not_searched",
        "unavailable",
        "failed",
    }
    candidate = contract["candidate"]
    assert set(candidate["required"]) == {
        "proposition",
        "claim_kind",
        "confidence_extraction",
        "expression",
    }
    assert set(candidate["properties"]["expression"]["required"]) == {
        "role",
        "selector",
    }
    assert candidate["properties"]["expression"]["properties"]["selector"][
        "required"
    ] == ["exact"]
    assert set(contract["evidence_variants"]) == {
        "truth_span",
        "web_fetch",
        "passage_citation",
    }
    assert "span_id" in contract["evidence_variants"]["truth_span"]["required"]
    assert {"fetch_id", "selector"} <= set(
        contract["evidence_variants"]["web_fetch"]["required"]
    )
    assert contract["evidence_variants"]["web_fetch"]["properties"]["selector"][
        "required"
    ] == ["exact"]
    assert set(
        contract["evidence_variants"]["passage_citation"]["properties"][
            "relationship"
        ]["enum"]
    ) == {"mentions", "inconclusive"}
    assert contract["submission_template"]["schema"] == (
        truth_analysis.ANALYSIS_OUTPUT_SCHEMA
    )


def test_new_passage_waits_until_current_analysis_candidates_are_decided(
    analysis_env,
):
    first = _prepare(analysis_env)
    context = _worker_context(first)
    completed = _submit_and_view(
        analysis_env["store"],
        first,
        _payload(context, [_candidate("A pending staged candidate.")]),
    )
    candidate = completed["candidates"][0]

    with pytest.raises(
        truth_analysis.TruthAnalysisError,
        match="Finish reviewing the current Truth analysis",
    ) as blocked:
        truth_analysis.prepare_analysis_run(
            analysis_env["store"],
            document_id=analysis_env["document"].id,
            capture=_capture(analysis_env, capture_id="another-passage-capture"),
            selection=_selection(),
            actor=HUMAN,
            selection_validator=lambda selection: selection,
        )
    assert blocked.value.code == "analysis_review_pending"
    assert blocked.value.details["analysis_run_id"] == first.run_id
    assert blocked.value.details["pending_candidates"] == 1

    _commit(
        run_id=first.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="dismiss",
        actor=HUMAN,
    )
    second = truth_analysis.prepare_analysis_run(
        analysis_env["store"],
        document_id=analysis_env["document"].id,
        capture=_capture(analysis_env, capture_id="another-passage-capture"),
        selection=_selection(),
        actor=HUMAN,
        selection_validator=lambda selection: selection,
    )

    assert second["analysis_run_id"] != first.run_id
    assert truth_analysis_runtime.runs_for_document(
        analysis_env["store"].store_id,
        analysis_env["document"].id,
    )[-1].run_id == second["analysis_run_id"]


def test_worker_binding_and_coverage_cannot_overstate_external_search(analysis_env):
    run = _prepare(analysis_env)
    context = _worker_context(run)
    with pytest.raises(
        truth_analysis.TruthAnalysisError,
        match="not authorized",
    ):
        truth_analysis.get_worker_context(
            run_id=run.run_id,
            agent_session_id="another-worker",
        )
    exaggerated = [dict(item) for item in context["source_coverage"]]
    web = next(item for item in exaggerated if item["source"] == "web")
    web["status"] = "searched"
    web["external_egress"] = True
    with pytest.raises(truth_analysis.TruthAnalysisError, match="overstates"):
        truth_analysis.submit_worker_output(
            run_id=run.run_id,
            agent_session_id=run.session_id,
            payload={
                **_payload(context, []),
                "source_coverage": exaggerated,
            },
        )


def test_web_coverage_reports_mixed_failure_truncation_and_fetch_egress(
    analysis_env,
):
    run = _prepare(analysis_env)
    hit_id = "6" * 32
    truth_analysis_runtime.record_search_receipt(
        run_id=run.run_id,
        query="bounded evidence",
        status="completed",
        hits=[
            {
                "hit_id": hit_id,
                "title": "Bounded evidence source",
                "url": "https://example.com/source",
                "snippet": "Search lead only.",
                "provider": "fixture",
                "lead_only": True,
            }
        ],
        external_egress=False,
        max_searches=truth_analysis.MAX_WEB_SEARCHES,
    )
    truth_analysis_runtime.record_search_receipt(
        run_id=run.run_id,
        query="failed evidence query",
        status="failed",
        hits=[],
        external_egress=False,
        error="The bounded search was unavailable.",
        max_searches=truth_analysis.MAX_WEB_SEARCHES,
    )
    fetched = truth_analysis_research.fetch(
        run_id=run.run_id,
        hit_id=hit_id,
        agent_session_id=run.session_id,
        resolver=lambda *_args: ("93.184.216.34",),
        requester=lambda *_args: truth_analysis_research.ResearchHttpResponse(
            200,
            {"Content-Type": "text/plain; charset=utf-8"},
            b"Source evidence " * 5_000,
        ),
    )
    assert fetched.receipt.acquisition_metadata["text_truncated"] is True

    context = _worker_context(run)
    web = next(
        item for item in context["source_coverage"] if item["source"] == "web"
    )
    assert web["status"] == "partial"
    assert web["external_egress"] is True
    assert "1 queries failed" in web["detail"]
    assert "1 captured source texts were truncated" in web["detail"]
    assert context["web_tools"]["max_fetch_bytes"] == (
        truth_analysis_research.MAX_RESPONSE_BYTES
    )
    assert context["web_tools"]["max_captured_text_bytes"] == (
        truth_analysis_research.MAX_CAPTURED_TEXT_BYTES
    )
    assert "acquisition_metadata.text_truncated" in (
        context["web_tools"]["truncation_metadata"]
    )
    quote = "Source evidence"
    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(
            context,
            [
                _candidate(
                    "The fetched source contains the reported evidence.",
                    evidence=[
                        {
                            "source_kind": "web_fetch",
                            "fetch_id": fetched.receipt.fetch_id,
                            "selector": {
                                "exact": quote,
                                "start": 0,
                                "end": len(quote),
                            },
                            "relationship": "supports",
                        }
                    ],
                )
            ],
        ),
    )
    capture = staged["candidates"][0]["evidence"][0]["integrity"]["capture"]
    acquisition = fetched.receipt.acquisition_metadata
    assert capture == {
        "text_truncated": True,
        "captured_text_bytes": acquisition["captured_text_bytes"],
        "extracted_text_bytes": acquisition["extracted_text_bytes"],
        "captured_text_sha256": fetched.receipt.text_sha256,
        "full_extracted_text_sha256": acquisition[
            "full_extracted_text_sha256"
        ],
        "maximum_captured_text_bytes": (
            truth_analysis_research.MAX_CAPTURED_TEXT_BYTES
        ),
    }


def test_typed_submit_stages_candidates_without_ledger_mutation(analysis_env):
    _, _, span = _existing_evidence(analysis_env)
    run = _prepare(analysis_env)
    context = _worker_context(run)
    before = _counts(analysis_env["store"])

    completed = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(
            context,
            [
                _candidate(
                    "A newly staged atomic claim.",
                    evidence=[
                        {
                            "source_kind": "truth_span",
                            "span_id": span.id,
                            "relationship": "partially_supports",
                            "rationale": "The source covers part of the proposition.",
                        },
                        {
                            "source_kind": "truth_span",
                            "span_id": span.id,
                            "relationship": "contradicts",
                            "rationale": "A deliberately separate assessment.",
                        },
                    ],
                )
            ],
        ),
    )

    assert _counts(analysis_env["store"]) == before
    candidate = completed["candidates"][0]
    assert candidate["status"] == "pending"
    assert len(candidate["evidence"]) == 2
    assert all(item["evidence_candidate_id"] for item in candidate["evidence"])
    assert len({item["evidence_candidate_id"] for item in candidate["evidence"]}) == 2
    assert {
        item["relationship"]: item["attachable"] for item in candidate["evidence"]
    } == {"partially_supports": True, "contradicts": False}
    assert completed["finished_at"]


def test_submit_ack_stays_compact_for_large_valid_staged_output(analysis_env):
    run = _prepare(analysis_env)
    context = _worker_context(run)
    candidates = [
        _candidate(f"Candidate {index}: " + (chr(97 + index) * 3_200))
        for index in range(18)
    ]

    receipt = truth_analysis.submit_worker_output(
        run_id=run.run_id,
        agent_session_id=run.session_id,
        payload=_payload(context, candidates),
    )
    stored = truth_analysis_runtime.get_run(run.run_id)

    assert len(json.dumps(receipt).encode("utf-8")) < 1_000
    assert receipt["schema"] == "wb.cowork.truth-analysis-submit-receipt/v1"
    assert receipt["output_sha256"]
    assert stored is not None and stored.output is not None
    assert len(
        json.dumps(
            stored.output,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ) <= truth_analysis.MAX_NORMALIZED_OUTPUT_BYTES
    assert len(stored.output["candidates"]) == 18


def test_frontend_shaped_decision_attaches_only_selected_support(analysis_env):
    _, _, span = _existing_evidence(analysis_env)
    run = _prepare(analysis_env)
    context = _worker_context(run)
    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(
            context,
            [
                _candidate(
                    "Selected support becomes attached.",
                    evidence=[
                        {
                            "source_kind": "truth_span",
                            "span_id": span.id,
                            "relationship": "partially_supports",
                        },
                        {
                            "source_kind": "truth_span",
                            "span_id": span.id,
                            "relationship": "contradicts",
                        },
                    ],
                ),
                _candidate(
                    "Empty evidence selection attaches nothing.",
                    evidence=[
                        {
                            "source_kind": "truth_span",
                            "span_id": span.id,
                            "relationship": "supports",
                        }
                    ],
                ),
                _candidate(
                    "Omitted evidence selection also attaches nothing.",
                    evidence=[
                        {
                            "source_kind": "truth_span",
                            "span_id": span.id,
                            "relationship": "supports",
                        }
                    ],
                ),
            ],
        ),
    )
    first, second, third = staged["candidates"]
    partial = next(
        item
        for item in first["evidence"]
        if item["relationship"] == "partially_supports"
    )
    contradicts = next(
        item for item in first["evidence"] if item["relationship"] == "contradicts"
    )

    with pytest.raises(truth_analysis.TruthAnalysisError, match="cannot be attached"):
        _commit(
            run_id=run.run_id,
            candidate_id=first["candidate_id"],
            expected_canonical_sha256=first["canonical_sha256"],
            decision="save_as_proposed",
            actor=HUMAN,
            edits={"evidence_candidate_ids": [contradicts["evidence_candidate_id"]]},
        )
    with pytest.raises(truth_analysis.TruthAnalysisError, match="unstaged evidence"):
        _commit(
            run_id=run.run_id,
            candidate_id=second["candidate_id"],
            expected_canonical_sha256=second["canonical_sha256"],
            decision="save_as_proposed",
            actor=HUMAN,
            edits={"evidence_candidate_ids": ["0" * 32]},
        )

    saved = _commit(
        run_id=run.run_id,
        candidate_id=first["candidate_id"],
        expected_canonical_sha256=first["canonical_sha256"],
        decision="save_as_proposed",
        actor=HUMAN,
        edits={"evidence_candidate_ids": [partial["evidence_candidate_id"]]},
    )
    saved_without_evidence = _commit(
        run_id=run.run_id,
        candidate_id=second["candidate_id"],
        expected_canonical_sha256=second["canonical_sha256"],
        decision="save_as_proposed",
        actor=HUMAN,
        edits={"evidence_candidate_ids": []},
    )
    saved_without_edits = _commit(
        run_id=run.run_id,
        candidate_id=third["candidate_id"],
        expected_canonical_sha256=third["canonical_sha256"],
        decision="save_as_proposed",
        actor=HUMAN,
    )

    assert saved["candidate_status"] == "saved"
    assert saved["claim_id"]
    assert saved["expression_id"]
    assert len(saved["result"]["support_link_ids"]) == 1
    assert saved_without_evidence["result"]["support_link_ids"] == []
    assert saved_without_edits["result"]["support_link_ids"] == []
    with analysis_env["store"]._read_connection() as conn:
        first_support = conn.execute(
            "SELECT COUNT(*) FROM claim_links WHERE from_claim_id = ? "
            "AND link_type = 'evidence_relation' "
            "AND json_extract(role_json, '$.evidential_effect') "
            "IN ('supports', 'partially_supports')",
            (saved["claim_id"],),
        ).fetchone()[0]
        second_support = conn.execute(
            "SELECT COUNT(*) FROM claim_links WHERE from_claim_id = ? "
            "AND link_type = 'evidence_relation' "
            "AND json_extract(role_json, '$.evidential_effect') "
            "IN ('supports', 'partially_supports')",
            (saved_without_evidence["claim_id"],),
        ).fetchone()[0]
    assert first_support == 1
    assert second_support == 0


def test_exact_match_requires_explicit_connect_existing(analysis_env):
    _, _, span = _existing_evidence(analysis_env)
    existing = analysis_env["store"].propose_claim(
        proposition="The selected passage has an existing equivalent claim.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    run = _prepare(analysis_env)
    context = _worker_context(run)
    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(
            context,
            [
                _candidate(
                    existing.proposition,
                    existing_claim_match=None,
                    evidence=[
                        {
                            "source_kind": "truth_span",
                            "span_id": span.id,
                            "relationship": "supports",
                        }
                    ],
                )
            ],
        ),
    )
    candidate = staged["candidates"][0]
    assert candidate["existing_claim_match"] == {
        "claim_id": existing.id,
        "proposition": existing.proposition,
        "claim_kind": existing.claim_kind,
        "status": "proposed",
        "relationship": "exact",
        "confidence": 1.0,
        "rationale": "Server-detected identical proposition and claim kind.",
    }
    before_claims = _counts(analysis_env["store"])["claims"]
    with pytest.raises(
        truth_analysis.TruthAnalysisError,
        match="Connect this passage",
    ):
        _commit(
            run_id=run.run_id,
            candidate_id=candidate["candidate_id"],
            expected_canonical_sha256=candidate["canonical_sha256"],
            decision="save_as_proposed",
            actor=HUMAN,
            edits={"evidence_candidate_ids": []},
        )
    connected = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="connect_existing",
        existing_claim_id=existing.id,
        actor=HUMAN,
    )
    assert _counts(analysis_env["store"])["claims"] == before_claims
    assert connected["candidate_status"] == "saved"
    assert connected["claim_id"] == existing.id
    assert connected["expression_id"]
    assert connected["result"]["support_link_ids"] == []


def test_connect_existing_attaches_only_explicit_selected_support(analysis_env):
    _, _, span = _existing_evidence(analysis_env)
    existing = analysis_env["store"].propose_claim(
        proposition="The selected passage has an existing equivalent claim.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    run = _prepare(analysis_env)
    context = _worker_context(run)
    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(
            context,
            [
                _candidate(
                    existing.proposition,
                    evidence=[
                        {
                            "source_kind": "truth_span",
                            "span_id": span.id,
                            "relationship": "supports",
                        }
                    ],
                )
            ],
        ),
    )
    candidate = staged["candidates"][0]
    evidence_id = candidate["evidence"][0]["evidence_candidate_id"]

    connected = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="connect_existing",
        existing_claim_id=existing.id,
        actor=HUMAN,
        edits={
            "proposition": candidate["proposition"],
            "claim_kind": candidate["claim_kind"],
            "expression_role": candidate["expression"]["role"],
            "evidence_candidate_ids": [evidence_id],
        },
    )

    assert connected["claim_id"] == existing.id
    assert len(connected["result"]["support_link_ids"]) == 1
    with analysis_env["store"]._read_connection() as conn:
        linked_span = conn.execute(
            "SELECT to_ref FROM claim_links WHERE from_claim_id = ? "
            "AND link_type = 'evidence_relation' "
            "AND json_extract(role_json, '$.evidential_effect') "
            "IN ('supports', 'partially_supports')",
            (existing.id,),
        ).fetchone()[0]
    assert linked_span == span.id


def test_materially_edited_exact_match_can_be_saved_as_new_proposal(analysis_env):
    existing = analysis_env["store"].propose_claim(
        proposition="The selected passage has an existing claim.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    run = _prepare(analysis_env)
    context = _worker_context(run)
    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(context, [_candidate(existing.proposition)]),
    )
    candidate = staged["candidates"][0]

    saved = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="save_as_proposed",
        actor=HUMAN,
        edits={
            "proposition": "The human materially revised this into a distinct claim.",
            "claim_kind": "fact",
            "expression_role": "paraphrase",
            "evidence_candidate_ids": [],
        },
    )

    assert saved["claim_id"] != existing.id
    assert analysis_env["store"].get_claim(saved["claim_id"]).proposition == (
        "The human materially revised this into a distinct claim."
    )


def test_live_exact_claim_created_after_staging_rebases_to_connect(analysis_env):
    proposition = "A live exact claim appeared after the worker staged its candidate."
    run, candidate = _stage_one(analysis_env, proposition)
    existing = analysis_env["store"].propose_claim(
        proposition=proposition,
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim

    refreshed = truth_analysis.analysis_run_view(
        truth_analysis_runtime.get_run(run.run_id),
        store=analysis_env["store"],
    )
    refreshed_candidate = refreshed["candidates"][0]
    assert refreshed_candidate["existing_claim_match"]["claim_id"] == existing.id
    assert refreshed_candidate["existing_claim_match"]["relationship"] == "exact"
    with pytest.raises(
        truth_analysis.TruthAnalysisError,
        match="Connect this passage",
    ):
        _commit(
            run_id=run.run_id,
            candidate_id=candidate["candidate_id"],
            expected_canonical_sha256=candidate["canonical_sha256"],
            decision="save_as_proposed",
            actor=HUMAN,
            edits={"evidence_candidate_ids": []},
        )
    connected = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="connect_existing",
        existing_claim_id=existing.id,
        actor=HUMAN,
        edits={"evidence_candidate_ids": []},
    )

    assert connected["claim_id"] == existing.id
    assert connected["candidate_status"] == "saved"


def test_duplicate_staged_claims_are_collapsed_before_human_review(analysis_env):
    run = _prepare(analysis_env)
    context = _worker_context(run)
    proposition = "One atomic claim must appear only once in review."

    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(context, [_candidate(proposition), _candidate(proposition)]),
    )

    assert len(staged["candidates"]) == 1
    assert "removed 1 duplicate staged candidate" in staged["limitations"][-1]
    candidate = staged["candidates"][0]
    dismissed = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="dismiss",
        actor=HUMAN,
    )
    assert dismissed["candidate_status"] == "dismissed"
    assert truth_analysis._unresolved_review_count(
        truth_analysis_runtime.get_run(run.run_id)
    ) == 0


def test_candidate_commit_rolls_back_claim_expression_and_decision_on_support_failure(
    analysis_env,
    monkeypatch,
):
    _, _, span = _existing_evidence(analysis_env)
    run, candidate = _stage_one(
        analysis_env,
        "This candidate must roll back as one canonical mutation.",
        evidence=[
            {
                "source_kind": "truth_span",
                "span_id": span.id,
                "relationship": "supports",
            }
        ],
    )
    evidence_id = candidate["evidence"][0]["evidence_candidate_id"]
    before = _counts(analysis_env["store"])

    original_add_link = type(analysis_env["store"]).add_link
    failed = False

    def _fail_support_write(bound_store, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated support write failure")
        return original_add_link(bound_store, **kwargs)

    monkeypatch.setattr(type(analysis_env["store"]), "add_link", _fail_support_write)
    with pytest.raises(RuntimeError, match="simulated support write failure"):
        _commit(
            run_id=run.run_id,
            candidate_id=candidate["candidate_id"],
            expected_canonical_sha256=candidate["canonical_sha256"],
            decision="save_as_proposed",
            actor=HUMAN,
            edits={"evidence_candidate_ids": [evidence_id]},
        )

    assert _counts(analysis_env["store"]) == before
    assert truth_analysis_runtime.candidate_decisions_for_run(run.run_id) == ()
    retried = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="save_as_proposed",
        actor=HUMAN,
        edits={"evidence_candidate_ids": []},
    )
    assert retried["candidate_status"] == "saved"
    assert retried["result"]["support_link_ids"] == []


def test_save_recovers_after_runtime_receipt_failure_without_duplicate_ledger_writes(
    analysis_env,
    monkeypatch,
):
    run, candidate = _stage_one(
        analysis_env,
        "A canonical candidate survives a receipt database failure.",
    )
    before = _counts(analysis_env["store"])
    original_record = truth_analysis_runtime.record_candidate_decision
    attempts = 0

    def _fail_once(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated runtime receipt failure")
        return original_record(**kwargs)

    monkeypatch.setattr(
        truth_analysis_runtime,
        "record_candidate_decision",
        _fail_once,
    )
    decision = {
        "run_id": run.run_id,
        "candidate_id": candidate["candidate_id"],
        "expected_canonical_sha256": candidate["canonical_sha256"],
        "decision": "save_as_proposed",
        "actor": HUMAN,
        "edits": {"evidence_candidate_ids": []},
    }
    with pytest.raises(RuntimeError, match="runtime receipt failure"):
        _commit(**decision)

    committed = _counts(analysis_env["store"])
    assert committed["claims"] == before["claims"] + 1
    assert committed["expressions"] == before["expressions"] + 1
    assert truth_analysis_runtime.candidate_decisions_for_run(run.run_id) == ()
    with analysis_env["store"]._read_connection() as conn:
        committed_expression_id = conn.execute(
            "SELECT e.id FROM expressions e JOIN claims c ON c.id = e.claim_ref "
            "WHERE e.claim_ref_kind = 'local' AND c.proposition = ?",
            ("A canonical candidate survives a receipt database failure.",),
        ).fetchone()[0]
    with pytest.raises(
        truth_analysis.TruthAnalysisError,
        match="already has another decision",
    ):
        _commit(
            **{
                **decision,
                "edits": {
                    "proposition": "A different retry must not create a second claim.",
                    "claim_kind": "fact",
                    "expression_role": "paraphrase",
                    "evidence_candidate_ids": [],
                },
            }
        )

    analysis_env["current_projection"]["text"] = (
        "An unrelated preface was added after the ledger commit.\n\n" + DOC_BODY
    )
    recovered = _commit(**decision)
    replay = _commit(**decision)

    assert _counts(analysis_env["store"]) == committed
    assert recovered["result"]["claim_created"] is True
    assert recovered["result"]["expression_created"] is True
    assert recovered["expression_id"] == committed_expression_id
    assert recovered["replayed"] is False
    assert replay["replayed"] is True
    assert replay["claim_id"] == recovered["claim_id"]
    assert replay["expression_id"] == recovered["expression_id"]


def test_connect_existing_recovers_after_runtime_receipt_failure(
    analysis_env,
    monkeypatch,
):
    existing = analysis_env["store"].propose_claim(
        proposition="The selected passage already has this exact claim.",
        claim_kind="fact",
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim
    run = _prepare(analysis_env)
    context = _worker_context(run)
    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(context, [_candidate(existing.proposition)]),
    )
    candidate = staged["candidates"][0]
    before = _counts(analysis_env["store"])
    original_record = truth_analysis_runtime.record_candidate_decision
    attempts = 0

    def _fail_once(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated runtime receipt failure")
        return original_record(**kwargs)

    monkeypatch.setattr(
        truth_analysis_runtime,
        "record_candidate_decision",
        _fail_once,
    )
    decision = {
        "run_id": run.run_id,
        "candidate_id": candidate["candidate_id"],
        "expected_canonical_sha256": candidate["canonical_sha256"],
        "decision": "connect_existing",
        "existing_claim_id": existing.id,
        "actor": HUMAN,
        "edits": {"evidence_candidate_ids": []},
    }
    with pytest.raises(RuntimeError, match="runtime receipt failure"):
        _commit(**decision)

    committed = _counts(analysis_env["store"])
    assert committed["claims"] == before["claims"]
    assert committed["expressions"] == before["expressions"] + 1
    assert truth_analysis_runtime.candidate_decisions_for_run(run.run_id) == ()

    analysis_env["current_projection"]["text"] = (
        "An unrelated preface was added after the ledger commit.\n\n" + DOC_BODY
    )
    recovered = _commit(**decision)
    replay = _commit(**decision)

    assert _counts(analysis_env["store"]) == committed
    assert recovered["claim_id"] == existing.id
    assert recovered["replayed"] is False
    assert replay["replayed"] is True
    assert replay["expression_id"] == recovered["expression_id"]


def test_unrelated_document_edit_reanchors_candidate_without_positional_guessing(
    analysis_env,
):
    run, candidate = _stage_one(
        analysis_env,
        "A candidate remains usable after an unrelated edit.",
    )
    analysis_env["current_projection"]["text"] = (
        "An unrelated new preface.\n\n" + DOC_BODY
    )

    saved = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="save_as_proposed",
        actor=HUMAN,
        edits={"evidence_candidate_ids": []},
    )

    assert saved["candidate_status"] == "saved"
    with analysis_env["store"]._read_connection() as conn:
        selector_json = conn.execute(
            "SELECT s.selector_json FROM expressions e "
            "JOIN document_spans s ON s.id = e.document_span_id "
            "WHERE e.id = ?",
            (saved["expression_id"],),
        ).fetchone()["selector_json"]
    selector = CompositeSelector.from_json(selector_json)
    assert selector.start == (
        "An unrelated new preface.\n\n" + DOC_BODY
    ).index(DOC_QUOTE)


def test_saved_claim_and_connection_expose_ai_preparation_and_human_addition(
    analysis_env,
):
    run, candidate = _stage_one(
        analysis_env,
        "AI prepared this candidate for an explicit human decision.",
    )
    saved = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="save_as_proposed",
        actor=HUMAN,
        edits={"evidence_candidate_ids": []},
    )
    listing = truth_surface.truth_list(
        analysis_env["store"],
        analysis_env["document"],
        view="document",
        filter_name="all",
        read_only=True,
    )
    projected = next(
        item for item in listing["claims"] if item["claim_id"] == saved["claim_id"]
    )
    expected_preparer = {
        "kind": "agent_run",
        "surface": "cowork_truth_analysis",
        "analysis_run_id": run.run_id,
        "candidate_id": candidate["candidate_id"],
        "provider_id": "claude-code",
        "model_id": "sonnet",
        "actor_ref": ActorRef(
            issuer_authority_id="test-issuer-authority",
            subject=run.session_id,
            kind="agent_run",
            tenant_scope_id="test-tenant-scope",
        ).to_dict(),
        "basis": "staged_candidate",
        "assurance": "run_bound",
    }
    expected_human = ActorRef(
        issuer_authority_id="test-issuer-authority",
        subject="dashboard-user",
        kind="human",
        tenant_scope_id="test-tenant-scope",
    )

    assert projected["created_by"] == {
        "kind": "agent_run",
        "ref": run.session_id,
    }
    assert projected["provenance"] == {
        "classification": "attributed",
        "prepared_by": expected_preparer,
        "added_by": {
            "kind": "human",
            "ref": "dashboard-user",
            "at": projected["provenance"]["added_by"]["at"],
            "actor_ref": expected_human.to_dict(),
            "basis": HUMAN_AUTHORITY_BASIS,
            "assurance": HUMAN_AUTHORITY_ASSURANCE,
        },
    }
    connection = projected["document_connections"][0]
    assert connection["provenance"] == {
        "classification": "attributed",
        "prepared_by": expected_preparer,
        "added_by": {
            "kind": "human",
            "ref": "dashboard-user",
            "at": connection["provenance"]["added_by"]["at"],
            "actor_ref": expected_human.to_dict(),
            "basis": HUMAN_AUTHORITY_BASIS,
            "assurance": HUMAN_AUTHORITY_ASSURANCE,
        },
    }


@pytest.mark.parametrize(
    "changed_projection",
    [
        DOC_BODY.replace(DOC_QUOTE, "The selected passage was removed."),
        DOC_BODY + "\n" + DOC_BODY,
    ],
)
def test_changed_or_ambiguous_passage_fails_safe_reanchor(
    analysis_env,
    changed_projection,
):
    run, candidate = _stage_one(
        analysis_env,
        "A candidate whose expression must still be uniquely placeable.",
    )
    analysis_env["current_projection"]["text"] = changed_projection
    before = _counts(analysis_env["store"])

    with pytest.raises(
        truth_surface.TruthSurfaceError,
        match="no longer uniquely present",
    ) as exc_info:
        _commit(
            run_id=run.run_id,
            candidate_id=candidate["candidate_id"],
            expected_canonical_sha256=candidate["canonical_sha256"],
            decision="save_as_proposed",
            actor=HUMAN,
            edits={"evidence_candidate_ids": []},
        )

    assert exc_info.value.code == "selection_not_verifiable"
    assert exc_info.value.retryable is True
    assert _counts(analysis_env["store"]) == before
    assert truth_analysis_runtime.candidate_decisions_for_run(run.run_id) == ()
    dismissed = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="dismiss",
        actor=HUMAN,
    )
    assert dismissed["candidate_status"] == "dismissed"
    refreshed = truth_analysis.analysis_run_view(
        truth_analysis_runtime.get_run(run.run_id),
        store=analysis_env["store"],
    )
    assert refreshed["candidates"][0]["status"] == "dismissed"


def test_passage_citation_cannot_launder_support_into_attachable_evidence(
    analysis_env,
):
    run = _prepare(analysis_env)
    context = _worker_context(run)
    with pytest.raises(truth_analysis.TruthAnalysisError, match="passage citation"):
        truth_analysis.submit_worker_output(
            run_id=run.run_id,
            agent_session_id=run.session_id,
            payload=_payload(
                context,
                [
                    _candidate(
                        "An unattested citation marker is not supporting evidence.",
                        evidence=[
                            {
                                "source_kind": "passage_citation",
                                "quote": DOC_QUOTE,
                                "relationship": "supports",
                            }
                        ],
                    )
                ],
            ),
        )


def test_selected_web_fetch_becomes_quarantined_canonical_support(analysis_env):
    run = _prepare(analysis_env)
    source_text = (
        "A controlled study reports that the bounded intervention reduced "
        "measured workload in its study sample."
    )
    hit_id = "7" * 32
    truth_analysis_runtime.record_search_receipt(
        run_id=run.run_id,
        query="controlled intervention measured workload study",
        status="completed",
        hits=[
            {
                "hit_id": hit_id,
                "title": "Controlled workload study",
                "url": "https://example.test/study",
                "snippet": "The intervention reduced measured workload.",
                "provider": "fake",
                "raw_text": source_text,
                "raw_text_truncated": False,
            }
        ],
        external_egress=True,
        max_searches=truth_analysis.MAX_WEB_SEARCHES,
    )
    fetch, _ = truth_analysis_runtime.record_fetch_receipt(
        run_id=run.run_id,
        hit_id=hit_id,
        status="completed",
        url="https://example.test/study",
        canonical_url="https://example.test/study",
        title="Controlled workload study",
        text=source_text,
        content_sha256=sha256_text(source_text),
        extractor="fake",
        external_egress=False,
        max_fetches=truth_analysis.MAX_WEB_FETCHES,
    )
    context = _worker_context(run)
    web_coverage = next(
        item for item in context["source_coverage"] if item["source"] == "web"
    )
    assert web_coverage["status"] == "searched"
    assert web_coverage["external_egress"] is True

    staged = _submit_and_view(
        analysis_env["store"],
        run,
        _payload(
            context,
            [
                _candidate(
                    "The intervention reduced measured workload.",
                    evidence=[
                        {
                            "source_kind": "web_fetch",
                            "fetch_id": fetch.fetch_id,
                            "quote": "the bounded intervention reduced measured workload",
                            "relationship": "supports",
                            "rationale": "The captured study text directly reports the outcome.",
                        }
                    ],
                )
            ],
        ),
    )
    candidate = staged["candidates"][0]
    web_evidence = candidate["evidence"][0]
    assert web_evidence == {
        **web_evidence,
        "source_kind": "web_fetch",
        "fetch_id": fetch.fetch_id,
        "source_locator": "https://example.test/study",
        "source_title": "Controlled workload study",
        "trust_class": "external_quarantined",
        "attachable": True,
    }

    saved = _commit(
        run_id=run.run_id,
        candidate_id=candidate["candidate_id"],
        expected_canonical_sha256=candidate["canonical_sha256"],
        decision="save_as_proposed",
        actor=HUMAN,
        edits={
            "evidence_candidate_ids": [web_evidence["evidence_candidate_id"]]
        },
    )

    assert len(saved["result"]["support_link_ids"]) == 1
    with analysis_env["store"]._read_connection() as conn:
        row = conn.execute(
            "SELECT e.*, l.created_by_kind AS link_actor_kind, "
            "l.created_by_ref AS link_actor_ref FROM claim_links l "
            "JOIN evidence_spans s ON s.id = l.to_ref "
            "JOIN evidence e ON e.id = s.evidence_id "
            "WHERE l.id = ?",
            (saved["result"]["support_link_ids"][0],),
        ).fetchone()
    assert row["kind"] == "web"
    assert row["trust_class"] == "external_quarantined"
    assert row["source_locator"] == "https://example.test/study"
    assert row["acquired_by_kind"] == "agent_run"
    assert row["acquired_by_ref"] == run.session_id
    assert row["link_actor_kind"] == "human"
    assert json.loads(row["link_actor_ref"]) == ActorRef(
        issuer_authority_id="test-issuer-authority",
        subject="dashboard-user",
        kind="human",
        tenant_scope_id="test-tenant-scope",
    ).to_dict()
    assert analysis_env["store"].read_evidence_text(row["id"]) == source_text


def test_unsupported_evidence_relationship_is_rejected(analysis_env):
    _, _, span = _existing_evidence(analysis_env)
    run = _prepare(analysis_env)
    context = _worker_context(run)
    with pytest.raises(truth_analysis.TruthAnalysisError, match="relationship"):
        truth_analysis.submit_worker_output(
            run_id=run.run_id,
            agent_session_id=run.session_id,
            payload=_payload(
                context,
                [
                    _candidate(
                        "A candidate with an invalid evidence assessment.",
                        evidence=[
                            {
                                "source_kind": "truth_span",
                                "span_id": span.id,
                                "relationship": "proves",
                            }
                        ],
                    )
                ],
            ),
        )
