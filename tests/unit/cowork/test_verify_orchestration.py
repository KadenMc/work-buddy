"""Job orchestration invariants for Co-work Verify and explicit Co-think."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from work_buddy.agent_execution.models import (
    AgentExecutionSelection,
    AgentSpawnOutcome,
    AgentSpawnRequest,
)
from work_buddy.cowork import verify_orchestration, verify_runtime
from work_buddy.cowork.execution_identity import CoworkVerifyRole
from work_buddy.cowork.verify import (
    ActionSnapshot,
    CoworkCoordinationStatusEvent,
    CriterionActivation,
    ResultRelation,
    RoutingDisposition,
    cothink_items,
    seed_terminology_exact_match,
    surfaced_results,
)
from work_buddy.cowork.verify_configuration import (
    set_document_criterion_enabled,
)
from work_buddy.cowork.verify_coordination import portable_coordination_jobs
from work_buddy.cowork.verify_inspection import verify_run_detail
from work_buddy.cowork.verify_projection import cothink_outcome_projection
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_orchestration import (
    VerifyOrchestrationError,
    get_worker_job,
    resume_submitted_job,
    run_status_projection,
    start_cothink,
    start_verify_run,
    submit_worker_job,
)
from work_buddy.cowork.verify_runtime import (
    claim_job_launch,
    create_job,
    get_job,
    jobs_for_document,
    jobs_for_run,
    redact_job_output,
    update_job,
)
from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import (
    canonical_json,
    new_id,
    sha256_bytes,
    sha256_text,
)
from work_buddy.truth.export import export_store, import_store

from .conftest import HUMAN, NOW


BODY = (
    "# Throwaway orchestration fixture\n\n"
    "The draft still says Co-work scope even though document target is preferred.\n"
)
CLEAN_BODY = (
    "# Throwaway clean orchestration fixture\n\n"
    "The draft consistently uses document target.\n"
)
SELECTION = AgentExecutionSelection(
    provider_id="codex",
    model_id="gpt-5.6-sol",
    provider_label="Codex",
    model_label="GPT-5.6 Sol",
)
AGENT = Actor(
    "agent_run",
    "throwaway-prior-proposal-agent",
    {
        "model": "test-model",
        "harness": "pytest",
        "surface": "cowork",
        "session_id": "throwaway-prior-proposal-session",
    },
)


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str):
        return ()


def _ready_document(
    store_ctx: dict[str, Any],
    *,
    body: str = BODY,
):
    store = store_ctx["store"]
    path = "docs/throwaway-verify-orchestration.md"
    file_path = store_ctx["root"] / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    projection = body.encode("utf-8")
    file_path.write_bytes(projection)
    snapshot = b"YDOC-VERIFY-ORCHESTRATION:" + sha256_bytes(projection).encode(
        "ascii"
    )
    snapshot_sha256 = ydoc_store.write_snapshot(store, snapshot=snapshot)
    document = documents.register_document(
        store,
        path=path,
        title="Throwaway Verify orchestration fixture",
        document_class="co_authored",
        content_sha256=sha256_bytes(projection),
        ydoc_snapshot_sha256=snapshot_sha256,
        actor=HUMAN,
        at=NOW,
    )
    return document, projection, snapshot


def _capture(
    store_ctx: dict[str, Any],
    *,
    target_text: str | None = None,
    body: str = BODY,
) -> dict[str, Any]:
    document, projection, snapshot = _ready_document(store_ctx, body=body)
    store = store_ctx["store"]
    snapshot_sha256 = sha256_bytes(snapshot)
    state_vector = b"throwaway-state-vector"
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=snapshot_sha256,
    )
    generation = documents.current_ydoc_generation(store, document.id)
    projection_text = projection.decode("utf-8")
    if target_text is None:
        selector: dict[str, Any] = {"kind": "document"}
        exact_target = projection_text
        label = "Whole document"
    else:
        start = projection_text.index(target_text)
        selector = {
            "kind": "text_quote",
            "exact": target_text,
            "prefix": projection_text[max(0, start - 20) : start],
            "suffix": projection_text[
                start + len(target_text) : start + len(target_text) + 20
            ],
            "start": start,
            "end": start + len(target_text),
        }
        exact_target = target_text
        label = "Selected passage"
    return {
        "schema": "wb.cowork.action-snapshot/v1",
        "captureId": "throwaway-capture",
        "storeId": store.store_id,
        "documentId": document.id,
        "capturedAt": NOW,
        "editGeneration": 1,
        "ydocGenerationSha256": generation,
        "snapshotBase64": base64.b64encode(snapshot).decode("ascii"),
        "snapshotSha256": snapshot_sha256,
        "stateVectorBase64": base64.b64encode(state_vector).decode("ascii"),
        "stateVectorSha256": sha256_bytes(state_vector),
        "structuredHeadSha256": head,
        "projectionMarkdown": projection_text,
        "projectionSha256": sha256_bytes(projection),
        "target": {
            "source": "whole_document" if target_text is None else "working_target",
            "label": label,
            "wordCount": len(exact_target.split()),
            "proseMirrorRange": None,
            "selector": selector,
            "targetTextSha256": sha256_text(exact_target),
        },
    }


@pytest.fixture
def orchestration_ctx(
    store_ctx: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    runtime_path = (
        tmp_path
        / "missing-runtime"
        / "agents"
        / "throwaway-cowork-verify-jobs.db"
    )
    monkeypatch.setattr(verify_runtime, "_DB_PATH", runtime_path)
    registry = store_ctx["registry"]
    monkeypatch.setattr(
        verify_orchestration,
        "TruthStoreRegistry",
        lambda: registry,
    )
    spawned: list[AgentSpawnRequest] = []

    def _spawn(request: AgentSpawnRequest) -> AgentSpawnOutcome:
        spawned.append(request)
        return AgentSpawnOutcome(
            status="ok",
            selection=request.selection,
            pid=700 + len(spawned),
            session_id=request.session_id,
        )

    return {
        **store_ctx,
        "runtime_path": runtime_path,
        "spawn": _spawn,
        "spawned": spawned,
    }


def _start_verify(
    ctx: dict[str, Any],
    *,
    target_text: str | None = None,
    body: str = BODY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    capture = _capture(ctx, target_text=target_text, body=body)
    result = start_verify_run(
        ctx["store"],
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Use established terminology.",
        protected_intent="Preserve the author's substantive meaning.",
        validate_selection=lambda selection: selection,
        spawn_detached=ctx["spawn"],
    )
    return capture, result


def _submit_initial_coordinator(
    ctx: dict[str, Any],
    start: dict[str, Any],
    *,
    decision: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    coordinator = get_job(start["job_id"])
    assert coordinator is not None
    worker = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )
    result_id = worker["context"]["normalized_results"][0][
        "evaluation_result_id"
    ]
    payload = {
        "decisions": [
            {
                "evaluation_result_id": result_id,
                "decision": decision,
                "rationale": "The finding matters in whole-document context.",
            }
        ],
        "summary": "Coordinator completed forest-level triage.",
    }
    response = submit_worker_job(
        job_id=coordinator.job_id,
        payload=payload,
        agent_session_id=coordinator.session_id,
        spawn_detached=ctx["spawn"],
    )
    return payload, response


def _submit_reviser(
    ctx: dict[str, Any],
    handoff: dict[str, Any],
    *,
    replacement: str = "document target",
) -> tuple[dict[str, Any], dict[str, Any]]:
    reviser = get_job(handoff["next_job_id"])
    assert reviser is not None
    worker = get_worker_job(
        job_id=reviser.job_id,
        agent_session_id=reviser.session_id,
    )
    result_id = worker["context"]["policy"]["requested_revision_result_ids"][0]
    payload = {
        "candidates": [
            {
                "evaluation_result_id": result_id,
                "replacement": replacement,
                "rationale": "Use the audited preferred term.",
                "tldr": "Replace the non-preferred label.",
            }
        ]
    }
    response = submit_worker_job(
        job_id=reviser.job_id,
        payload=payload,
        agent_session_id=reviser.session_id,
        spawn_detached=ctx["spawn"],
    )
    return payload, response


def test_runtime_persists_binding_and_enforces_forward_immutable_state(
    orchestration_ctx: dict[str, Any],
):
    job = create_job(
        job_id="runtime-job",
        store_id=orchestration_ctx["store_id"],
        document_id="document-id",
        evaluation_run_id="run-id",
        action_snapshot_id="action-id",
        plan_snapshot_id="plan-id",
        role=CoworkVerifyRole.REVISER,
        selection=SELECTION.to_dict(),
        authorization_receipt_id="receipt-id",
        context_sha256="a" * 64,
        request={"user_goal": "Throwaway goal"},
        session_id="runtime-job-cowork-verify-reviser",
        at=NOW,
    )
    assert get_job(job.job_id) == job
    assert [item.job_id for item in jobs_for_run(job.store_id, "run-id")] == [
        job.job_id
    ]
    assert [
        item.job_id for item in jobs_for_document(job.store_id, "document-id")
    ] == [job.job_id]

    launching, claimed = claim_job_launch(job.job_id)
    assert claimed is True
    duplicate_claim, claimed_again = claim_job_launch(job.job_id)
    assert claimed_again is False
    assert duplicate_claim == launching
    update_job(job.job_id, status="running", pid=722)
    output = {"candidates": []}
    submitted = update_job(
        job.job_id,
        status="submitted",
        output=output,
        output_sha256=sha256_text(canonical_json(output)),
    )
    assert submitted.pid == 722
    assert submitted.output == output
    with pytest.raises(ValueError, match="immutable"):
        update_job(
            job.job_id,
            status="submitted",
            output={"candidates": [{"different": True}]},
        )
    completed = update_job(job.job_id, status="completed")
    with pytest.raises(ValueError, match="transition"):
        update_job(job.job_id, status="running")
    redacted = redact_job_output(completed.job_id)
    assert redacted.output is None
    assert redacted.output_sha256 == submitted.output_sha256


def test_runtime_read_helpers_do_not_create_host_state(
    orchestration_ctx: dict[str, Any],
):
    runtime_path = orchestration_ctx["runtime_path"]
    assert not runtime_path.parent.exists()

    assert get_job("missing-job") is None
    assert jobs_for_run("missing-store", "missing-run") == ()
    assert jobs_for_document("missing-store", "missing-document") == ()

    assert not runtime_path.parent.exists()
    assert not runtime_path.exists()


def test_submitted_coordinator_projection_resumes_without_another_model_call(
    orchestration_ctx: dict[str, Any],
):
    _capture_value, started = _start_verify(orchestration_ctx)
    coordinator = get_job(started["job_id"])
    assert coordinator is not None
    context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]
    output = {
        "decisions": [
            {
                "evaluation_result_id": context["normalized_results"][0][
                    "evaluation_result_id"
                ],
                "decision": "retain",
                "rationale": "The finding does not warrant user attention.",
            }
        ],
        "summary": "Coordinator completed forest-level triage.",
    }
    update_job(
        coordinator.job_id,
        status="submitted",
        output=output,
        output_sha256=sha256_text(canonical_json(output)),
    )
    spawn_count = len(orchestration_ctx["spawned"])

    resumed = resume_submitted_job(
        coordinator.job_id,
        spawn_detached=orchestration_ctx["spawn"],
    )
    replay = resume_submitted_job(
        coordinator.job_id,
        spawn_detached=orchestration_ctx["spawn"],
    )

    assert resumed is not None and resumed["status"] == "completed"
    assert replay is not None and replay["status"] == "completed"
    assert replay["replayed"] is True
    assert get_job(coordinator.job_id).status == "completed"
    assert len(orchestration_ctx["spawned"]) == spawn_count
    dispositions = verify_store.list_records(
        orchestration_ctx["store"],
        RoutingDisposition,
    )
    assert len(dispositions) == 1
    assert dispositions[0].decision == "suppress"
    status_events = verify_store.list_records(
        orchestration_ctx["store"],
        CoworkCoordinationStatusEvent,
        where="coordination_job_id = ?",
        params=(coordinator.job_id,),
    )
    statuses = [event.status for event in status_events]
    assert statuses == [
        "prepared",
        "launching",
        "running",
        "submitted",
        "completed",
    ]


def test_exact_capture_freezes_server_validated_version_and_target(
    orchestration_ctx: dict[str, Any],
):
    capture, started = _start_verify(
        orchestration_ctx,
        target_text="Co-work scope",
    )
    action = verify_store.get_record(
        orchestration_ctx["store"],
        ActionSnapshot,
        started["action_snapshot_id"],
    )
    assert action is not None
    assert action.structured_head_sha256 == capture["structuredHeadSha256"]
    assert action.ydoc_generation_sha256 == capture["ydocGenerationSha256"]
    assert action.target_kind == "text_quote"
    assert orchestration_ctx["store"].resolve_blob_path(
        f"blobs/{action.target_blob_sha256}"
    ).read_text(encoding="utf-8") == "Co-work scope"

    tampered = deepcopy(capture)
    tampered["projectionMarkdown"] += "\nInjected after hashing."
    with pytest.raises(VerifyOrchestrationError, match="projection bytes"):
        start_verify_run(
            orchestration_ctx["store"],
            document_id=capture["documentId"],
            capture=tampered,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve meaning.",
            validate_selection=lambda selection: selection,
            spawn_detached=orchestration_ctx["spawn"],
        )


def test_disabled_seeded_criterion_fails_before_evaluation_or_model_launch(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx)
    store = orchestration_ctx["store"]
    set_document_criterion_enabled(
        store,
        document_id=capture["documentId"],
        criterion_key="terminology_exact_match",
        enabled=False,
        actor=HUMAN,
    )

    with pytest.raises(VerifyOrchestrationError, match="no active available"):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Do not run a disabled criterion.",
            protected_intent="Honor the effective configuration.",
            validate_selection=lambda selection: selection,
            spawn_detached=orchestration_ctx["spawn"],
        )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 0
    assert orchestration_ctx["spawned"] == []
    assert not orchestration_ctx["runtime_path"].exists()


def test_browser_capture_time_is_telemetry_not_prior_decision_authority(
    orchestration_ctx: dict[str, Any],
):
    first_capture, first_started = _start_verify(orchestration_ctx)
    _submit_initial_coordinator(
        orchestration_ctx,
        first_started,
        decision="retain",
    )
    disposition = verify_store.list_records(
        orchestration_ctx["store"],
        RoutingDisposition,
    )[0]

    second_capture = deepcopy(first_capture)
    second_capture["captureId"] = "throwaway-server-time-capture"
    second_capture["capturedAt"] = "1900-01-01T00:00:00.000+00:00"
    second_started = start_verify_run(
        orchestration_ctx["store"],
        document_id=second_capture["documentId"],
        capture=second_capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Use established terminology.",
        protected_intent="Preserve the author's substantive meaning.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )
    action = verify_store.get_record(
        orchestration_ctx["store"],
        ActionSnapshot,
        second_started["action_snapshot_id"],
    )
    assert action is not None
    assert action.created_at != second_capture["capturedAt"]
    assert (
        json.loads(action.context_boundary_json)["client_captured_at"]
        == second_capture["capturedAt"]
    )
    second_job = get_job(second_started["job_id"])
    assert second_job is not None
    context = get_worker_job(
        job_id=second_job.job_id,
        agent_session_id=second_job.session_id,
    )["context"]
    assert [item["id"] for item in context["prior_dispositions"]] == [
        disposition.id
    ]


def test_required_blocked_criterion_fails_closed_before_launch(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx)
    store = orchestration_ctx["store"]
    seeded = seed_terminology_exact_match(store)
    scope = {
        "kind": "document",
        "document_id": capture["documentId"],
    }
    payload = {
        "criterion_definition_version_id": seeded.criterion.id,
        "criterion_check_binding_id": seeded.binding.id,
        "scope": scope,
        "is_enabled": False,
        "is_required": True,
        "origin": "system",
    }
    activation = CriterionActivation(
        id=new_id(),
        criterion_definition_version_id=seeded.criterion.id,
        criterion_check_binding_id=seeded.binding.id,
        scope_json=canonical_json(scope),
        is_enabled=0,
        is_required=1,
        origin="system",
        canonical_sha256=sha256_text(canonical_json(payload)),
        created_at=NOW,
        created_by_kind="system",
        created_by_ref="verify-policy-test",
        created_by_meta_json=None,
    )
    verify_store.insert_record(store, activation)

    with pytest.raises(VerifyOrchestrationError, match="required.*blocked"):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Fail closed on required policy.",
            protected_intent="Do not bypass a required check.",
            validate_selection=lambda selection: selection,
            spawn_detached=orchestration_ctx["spawn"],
        )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 0
    assert orchestration_ctx["spawned"] == []
    assert not orchestration_ctx["runtime_path"].exists()


def test_proposal_bound_recheck_requires_a_durable_recheck_intent(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx)
    store = orchestration_ctx["store"]
    offset = BODY.index("Co-work scope")
    prior = proposals.propose_edit(
        store,
        document_id=capture["documentId"],
        base_content_sha256=capture["projectionSha256"],
        base_structured_head_sha256=capture["structuredHeadSha256"],
        selector=CompositeSelector(
            exact="Co-work scope",
            start=offset,
            end=offset + len("Co-work scope"),
        ),
        quote_exact="Co-work scope",
        replacement="document target",
        rationale="Prior throwaway correction.",
        tldr="Use preferred terminology.",
        actor=AGENT,
        at=NOW,
    )
    with pytest.raises(
        VerifyOrchestrationError,
        match="exact committed-sitting recheck_intent_id",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Recheck the proposed terminology correction.",
            protected_intent="Preserve substantive meaning.",
            recheck_of_proposal_ids=[prior.id],
            validate_selection=lambda selection: selection,
            spawn_detached=orchestration_ctx["spawn"],
        )

    assert verify_store.list_records(store, ResultRelation) == ()
    assert orchestration_ctx["spawned"] == []

    other_body = "# Throwaway other document\n\nOther sentence.\n"
    other_path = "docs/throwaway-other-recheck.md"
    (orchestration_ctx["root"] / other_path).write_text(
        other_body,
        encoding="utf-8",
    )
    other_snapshot = b"YDOC-OTHER-RECHECK"
    other_snapshot_sha256 = ydoc_store.write_snapshot(
        store,
        snapshot=other_snapshot,
    )
    other_document = documents.register_document(
        store,
        path=other_path,
        title="Throwaway other recheck fixture",
        document_class="co_authored",
        content_sha256=sha256_text(other_body),
        ydoc_snapshot_sha256=other_snapshot_sha256,
        actor=HUMAN,
        at=NOW,
    )
    other_head = ydoc_store.current_structured_head(
        store,
        document_id=other_document.id,
        snapshot_sha256=other_snapshot_sha256,
    )
    other_offset = other_body.index("Other sentence.")
    other_proposal = proposals.propose_edit(
        store,
        document_id=other_document.id,
        base_content_sha256=sha256_text(other_body),
        base_structured_head_sha256=other_head,
        selector=CompositeSelector(
            exact="Other sentence.",
            start=other_offset,
            end=other_offset + len("Other sentence."),
        ),
        quote_exact="Other sentence.",
        replacement="Another sentence.",
        rationale="Throwaway mismatch proposal.",
        tldr="Change other document.",
        actor=AGENT,
        at=NOW,
    )
    with pytest.raises(VerifyOrchestrationError, match="another document"):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Attempt an invalid cross-document recheck.",
            protected_intent="Preserve substantive meaning.",
            recheck_of_proposal_ids=[other_proposal.id],
            validate_selection=lambda selection: selection,
            spawn_detached=orchestration_ctx["spawn"],
        )


def test_initial_coordinator_retains_immaterial_finding_without_reviser(
    orchestration_ctx: dict[str, Any],
):
    capture, started = _start_verify(orchestration_ctx)
    store = orchestration_ctx["store"]
    coordinator = get_job(started["job_id"])
    assert coordinator is not None
    assert coordinator.role is CoworkVerifyRole.COORDINATOR
    context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]
    assert context["document"]["frozen_markdown"] == BODY
    assert context["user_goal"] == "Use established terminology."
    assert context["protected_intent"] == (
        "Preserve the author's substantive meaning."
    )
    assert context["policy"]["coordinator_stage"] == "initial"
    assert context["candidate_revision"]["status"] == "not_requested"
    assert surfaced_results(store, document_id=capture["documentId"]) == ()
    history = run_status_projection(store, document_id=capture["documentId"])
    assert history[0]["result_count"] == 1
    assert history[0]["surfaced_result_count"] == 0

    _, completed = _submit_initial_coordinator(
        orchestration_ctx,
        started,
        decision="retain",
    )
    assert "next_job_id" not in completed
    assert surfaced_results(store, document_id=capture["documentId"]) == ()
    jobs = jobs_for_run(store.store_id, started["run_id"])
    assert [job.role for job in jobs] == [CoworkVerifyRole.COORDINATOR]
    dispositions = verify_store.list_records(store, RoutingDisposition)
    assert len(dispositions) == 1
    assert dispositions[0].decision == "suppress"
    assert (
        dispositions[0].policy_snapshot_sha256
        == context["policy"]["effective_policy_sha256"]
    )


def test_material_finding_flows_coordinator_reviser_coordinator_to_proposal(
    orchestration_ctx: dict[str, Any],
):
    capture, started = _start_verify(orchestration_ctx)
    initial = get_job(started["job_id"])
    assert initial is not None
    initial_context = get_worker_job(
        job_id=initial.job_id,
        agent_session_id=initial.session_id,
    )["context"]
    result_id = initial_context["normalized_results"][0][
        "evaluation_result_id"
    ]
    with pytest.raises(
        VerifyOrchestrationError,
        match="unsupported coordinator decision",
    ):
        submit_worker_job(
            job_id=initial.job_id,
            payload={
                "decisions": [
                    {
                        "evaluation_result_id": result_id,
                        "decision": "route_to_correction",
                        "rationale": "Attempt to bypass the reviser.",
                    }
                ],
                "summary": "Invalid first-pass routing.",
            },
            agent_session_id=initial.session_id,
        )

    initial_payload, first_handoff = _submit_initial_coordinator(
        orchestration_ctx,
        started,
        decision="request_revision",
    )
    replayed_first_pass = submit_worker_job(
        job_id=initial.job_id,
        payload=initial_payload,
        agent_session_id=initial.session_id,
        spawn_detached=orchestration_ctx["spawn"],
    )
    assert replayed_first_pass["replayed"] is True
    assert replayed_first_pass["next_job_id"] == first_handoff["next_job_id"]
    spawn_count = len(orchestration_ctx["spawned"])
    replayed_run = start_verify_run(
        orchestration_ctx["store"],
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Use established terminology.",
        protected_intent="Preserve the author's substantive meaning.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )
    assert replayed_run["replayed"] is True
    assert replayed_run["job_id"] == first_handoff["next_job_id"]
    assert replayed_run["stage"] == "drafting_correction"
    assert len(orchestration_ctx["spawned"]) == spawn_count
    assert surfaced_results(
        orchestration_ctx["store"],
        document_id=capture["documentId"],
    ) == ()

    reviser_payload, second_handoff = _submit_reviser(
        orchestration_ctx,
        first_handoff,
    )
    reviser = get_job(first_handoff["next_job_id"])
    assert reviser is not None
    replayed_handoff = submit_worker_job(
        job_id=reviser.job_id,
        payload=reviser_payload,
        agent_session_id=reviser.session_id,
        spawn_detached=orchestration_ctx["spawn"],
    )
    assert replayed_handoff["replayed"] is True
    assert replayed_handoff["next_job_id"] == second_handoff["next_job_id"]

    coordinator = get_job(second_handoff["next_job_id"])
    assert coordinator is not None
    context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]
    assert context["document"]["frozen_markdown"] == BODY
    assert context["policy"]["coordinator_stage"] == "post_revision"
    assert context["candidate_revision"]["status"] == "available"
    assert context["candidate_revision"]["candidates"][0][
        "replacement"
    ] == "document target"
    assert context["candidate_revision"]["affected_evaluations"][0][
        "status"
    ] == "passed"
    result_id = context["normalized_results"][0]["evaluation_result_id"]
    with pytest.raises(
        VerifyOrchestrationError,
        match="unsupported coordinator decision",
    ):
        submit_worker_job(
            job_id=coordinator.job_id,
            payload={
                "decisions": [
                    {
                        "evaluation_result_id": result_id,
                        "decision": "request_revision",
                        "rationale": "Attempt to start an unbounded loop.",
                    }
                ],
                "summary": "Invalid second-pass routing.",
            },
            agent_session_id=coordinator.session_id,
        )
    decision = {
        "decisions": [
            {
                "evaluation_result_id": result_id,
                "decision": "route_to_correction",
                "rationale": "The replacement preserves intent and improves terminology.",
            }
        ],
        "summary": "Route the exact terminology correction for human review.",
    }
    first = submit_worker_job(
        job_id=coordinator.job_id,
        payload=decision,
        agent_session_id=coordinator.session_id,
    )
    replay = submit_worker_job(
        job_id=coordinator.job_id,
        payload=decision,
        agent_session_id=coordinator.session_id,
    )
    assert replay["replayed"] is True
    assert replay["proposal_ids"] == first["proposal_ids"]
    assert len(first["proposal_ids"]) == 1

    proposal = proposals.get_proposal(
        orchestration_ctx["store"],
        first["proposal_ids"][0],
    )
    assert proposal.document_id == capture["documentId"]
    assert proposal.quote_exact == "Co-work scope"
    assert proposal.replacement == "document target"
    assert proposal.base_structured_head_sha256 == capture["structuredHeadSha256"]
    jobs = jobs_for_run(
        orchestration_ctx["store"].store_id,
        started["run_id"],
    )
    assert [job.role for job in jobs] == [
        CoworkVerifyRole.COORDINATOR,
        CoworkVerifyRole.REVISER,
        CoworkVerifyRole.COORDINATOR,
    ]
    assert get_job(reviser.job_id).output is None
    with orchestration_ctx["store"].connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM routing_dispositions"
            ).fetchone()[0]
            == 1
        )


def test_post_revision_coordinator_cannot_route_a_failed_candidate_recheck(
    orchestration_ctx: dict[str, Any],
):
    _capture_value, started = _start_verify(orchestration_ctx)
    _initial_payload, first_handoff = _submit_initial_coordinator(
        orchestration_ctx,
        started,
        decision="request_revision",
    )
    _candidate_payload, second_handoff = _submit_reviser(
        orchestration_ctx,
        first_handoff,
        replacement="Co-work scope again",
    )
    coordinator = get_job(second_handoff["next_job_id"])
    assert coordinator is not None
    context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]
    affected = context["candidate_revision"]["affected_evaluations"]
    assert len(affected) == 1
    assert affected[0]["status"] == "failed"
    assert affected[0]["match_count"] == 1
    result_id = context["normalized_results"][0]["evaluation_result_id"]

    with pytest.raises(
        VerifyOrchestrationError,
        match="passing deterministic affected-region re-evaluation",
    ):
        submit_worker_job(
            job_id=coordinator.job_id,
            payload={
                "decisions": [
                    {
                        "evaluation_result_id": result_id,
                        "decision": "route_to_correction",
                        "rationale": "Attempt to route a regressing candidate.",
                    }
                ],
                "summary": "The candidate still violates the active check.",
            },
            agent_session_id=coordinator.session_id,
        )

    completed = submit_worker_job(
        job_id=coordinator.job_id,
        payload={
            "decisions": [
                {
                    "evaluation_result_id": result_id,
                    "decision": "surface",
                    "rationale": "The candidate regressed; surface the finding.",
                }
            ],
            "summary": "Do not route the failed candidate.",
        },
        agent_session_id=coordinator.session_id,
    )
    assert completed["status"] == "completed"
    reviser = get_job(first_handoff["next_job_id"])
    assert reviser is not None and reviser.output is None
    portable_post = next(
        item
        for item in portable_coordination_jobs(
            orchestration_ctx["store"],
            evaluation_run_id=started["run_id"],
        )
        if item["job_id"] == coordinator.job_id
    )
    proof = portable_post["request_summary"]["candidate_evaluations"]
    assert proof[0]["status"] == "failed"
    assert proof[0]["candidate_sha256"] == sha256_text(
        "Co-work scope again"
    )
    with orchestration_ctx["store"].connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "forbidden",
    ["defer", "surface", "request_revision"],
)
def test_clean_conforming_result_is_quietly_retained(
    orchestration_ctx: dict[str, Any],
    forbidden: str,
):
    capture, started = _start_verify(
        orchestration_ctx,
        body=CLEAN_BODY,
    )
    coordinator = get_job(started["job_id"])
    assert coordinator is not None
    context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]
    result = context["normalized_results"][0]
    assert result["result_kind"] == "conforming"

    invalid = {
        "decisions": [
            {
                "evaluation_result_id": result["evaluation_result_id"],
                "decision": forbidden,
                "rationale": "Attempt to expose a clean result.",
            }
        ],
        "summary": "Invalid clean-result routing.",
    }
    with pytest.raises(
        VerifyOrchestrationError,
        match="conforming results must be quietly retained",
    ):
        submit_worker_job(
            job_id=coordinator.job_id,
            payload=invalid,
            agent_session_id=coordinator.session_id,
        )

    completed = submit_worker_job(
        job_id=coordinator.job_id,
        payload={
            "decisions": [
                {
                    "evaluation_result_id": result["evaluation_result_id"],
                    "decision": "retain",
                    "rationale": "The configured check found no violation.",
                }
            ],
            "summary": "No user attention is warranted.",
        },
        agent_session_id=coordinator.session_id,
    )
    assert "next_job_id" not in completed
    assert surfaced_results(
        orchestration_ctx["store"],
        document_id=capture["documentId"],
    ) == ()
    dispositions = verify_store.list_records(
        orchestration_ctx["store"],
        RoutingDisposition,
    )
    assert len(dispositions) == 1
    assert dispositions[0].decision == "suppress"
    assert dispositions[0].policy_snapshot_sha256 is not None


def test_cothink_is_explicit_non_evidential_and_replay_safe(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx, target_text="Co-work scope")
    started = start_cothink(
        orchestration_ctx["store"],
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        purpose="Challenge the framing without declaring a defect.",
        protected_intent="Keep the distinction between evidence and provocation.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )
    job = get_job(started["job_id"])
    assert job is not None
    context = get_worker_job(
        job_id=job.job_id,
        agent_session_id=job.session_id,
    )["context"]
    assert context["policy"]["co_think_is_non_evidential"] is True
    assert context["normalized_results"] == []

    payload = {
        "outcome": "perspective",
        "content": "Could the local label encode a distinction worth defining?",
        "rationale": "Invite reflection without presenting this as evidence.",
    }
    first = submit_worker_job(
        job_id=job.job_id,
        payload=payload,
        agent_session_id=job.session_id,
    )
    replay = submit_worker_job(
        job_id=job.job_id,
        payload=payload,
        agent_session_id=job.session_id,
    )
    assert replay["replayed"] is True
    assert replay["cothink_item_id"] == first["cothink_item_id"]
    projected = cothink_items(
        orchestration_ctx["store"],
        document_id=capture["documentId"],
    )
    assert len(projected) == 1
    assert projected[0]["id"] == first["cothink_item_id"]
    assert surfaced_results(
        orchestration_ctx["store"],
        document_id=capture["documentId"],
    ) == ()


def test_cothink_none_replay_normalizes_optional_content(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx)
    started = start_cothink(
        orchestration_ctx["store"],
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        purpose="Look for a useful alternative perspective.",
        protected_intent="Do not manufacture a provocation.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )
    job = get_job(started["job_id"])
    assert job is not None
    payload = {
        "outcome": "none",
        "rationale": "No useful perspective was found.",
    }
    first = submit_worker_job(
        job_id=job.job_id,
        payload=payload,
        agent_session_id=job.session_id,
    )
    replay = submit_worker_job(
        job_id=job.job_id,
        payload=payload,
        agent_session_id=job.session_id,
    )
    assert first["cothink_item_id"] is None
    assert replay["replayed"] is True
    assert replay["cothink_item_id"] is None


def test_portable_coordination_history_survives_without_runtime_database(
    orchestration_ctx: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    capture, started = _start_verify(orchestration_ctx)
    _submit_initial_coordinator(
        orchestration_ctx,
        started,
        decision="retain",
    )
    cothink_started = start_cothink(
        orchestration_ctx["store"],
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        purpose="Look for one useful alternative perspective.",
        protected_intent="Do not manufacture a provocation.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )
    cothink_job = get_job(cothink_started["job_id"])
    assert cothink_job is not None
    submit_worker_job(
        job_id=cothink_job.job_id,
        payload={
            "outcome": "none",
            "rationale": "No useful perspective was found.",
        },
        agent_session_id=cothink_job.session_id,
    )

    exported = export_store(
        orchestration_ctx["store"],
        tmp_path / "portable-coordination.jsonl",
    )
    exported_text = exported.path.read_text(encoding="utf-8")
    assert '"record_type":"cowork_coordination_job"' in exported_text
    assert (
        '"record_type":"cowork_coordination_status_event"'
        in exported_text
    )
    target = tmp_path / "portable-coordination-target"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store

    # Prove the projections do not accidentally join the imported store to
    # source-machine runtime rows carrying the same permanent store id.
    monkeypatch.setattr(
        verify_runtime,
        "_DB_PATH",
        tmp_path / "absent-runtime" / "verify-jobs.db",
    )
    history = run_status_projection(
        restored,
        document_id=capture["documentId"],
    )
    assert len(history) == 1
    assert history[0]["status"] == "completed"
    assert history[0]["coordination_status"] == "completed"
    assert history[0]["provider_id"] == SELECTION.provider_id
    assert history[0]["model_id"] == SELECTION.model_id
    assert history[0]["purpose"] == "Use established terminology."

    detail = verify_run_detail(
        restored,
        document_id=capture["documentId"],
        run_id=started["run_id"],
    )
    coordination = detail["coordination"][0]
    assert coordination["status"] == "completed"
    assert coordination["outcome_kind"] == "routing_completed"
    assert (
        coordination["request_summary"]["user_goal"]
        == "Use established terminology."
    )
    assert (
        coordination["request_summary"]["protected_intent"]
        == "Preserve the author's substantive meaning."
    )
    assert coordination["request_summary"]["effective_configuration"]
    assert coordination["request_summary"]["prior_disposition_ids"] == []
    assert coordination["candidate_lineage"]["output_sha256"]
    assert "Coordinator completed forest-level triage." not in str(
        coordination
    )
    assert (
        "The finding matters in whole-document context."
        not in str(coordination)
    )

    restored_document = documents.get_document(
        restored,
        capture["documentId"],
    )
    outcomes = cothink_outcome_projection(restored, restored_document)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "completed_no_useful_item"
    assert (
        outcomes[0]["rationale"]
        == "No useful alternative perspective was found."
    )


def test_candidate_re_evaluation_proof_survives_redaction_and_export(
    orchestration_ctx: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    capture, started = _start_verify(orchestration_ctx)
    _initial_payload, first_handoff = _submit_initial_coordinator(
        orchestration_ctx,
        started,
        decision="request_revision",
    )
    _candidate_payload, second_handoff = _submit_reviser(
        orchestration_ctx,
        first_handoff,
    )
    coordinator = get_job(second_handoff["next_job_id"])
    assert coordinator is not None
    context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]
    result_id = context["normalized_results"][0]["evaluation_result_id"]
    submit_worker_job(
        job_id=coordinator.job_id,
        payload={
            "decisions": [
                {
                    "evaluation_result_id": result_id,
                    "decision": "route_to_correction",
                    "rationale": "The deterministically checked candidate fits.",
                }
            ],
            "summary": "Route the checked correction for human review.",
        },
        agent_session_id=coordinator.session_id,
    )
    reviser = get_job(first_handoff["next_job_id"])
    assert reviser is not None and reviser.output is None

    exported = export_store(
        orchestration_ctx["store"],
        tmp_path / "candidate-proof.jsonl",
    )
    target = tmp_path / "candidate-proof-target"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store
    monkeypatch.setattr(
        verify_runtime,
        "_DB_PATH",
        tmp_path / "absent-candidate-runtime" / "verify-jobs.db",
    )

    detail = verify_run_detail(
        restored,
        document_id=capture["documentId"],
        run_id=started["run_id"],
    )
    post_revision = next(
        item
        for item in detail["coordination"]
        if item["role"] == CoworkVerifyRole.COORDINATOR.value
        and item["request_summary"]["coordinator_stage"] == "post_revision"
    )
    proof = post_revision["candidate_lineage"]["affected_evaluations"]
    assert len(proof) == 1
    assert proof[0]["evaluation_result_id"] == result_id
    assert proof[0]["status"] == "passed"
    assert proof[0]["coverage"] == (
        "changed_region_with_term_length_boundaries"
    )
    assert proof[0]["candidate_sha256"] == sha256_text("document target")
