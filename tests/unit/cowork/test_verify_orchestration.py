"""Job orchestration invariants for Co-work Verify and explicit Co-think."""

from __future__ import annotations

import base64
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
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
    CheckDefinitionVersion,
    CheckExecution,
    CoworkCoordinationJob,
    CoworkCoordinationStatusEvent,
    CriterionActivation,
    CriterionCheckBinding,
    CriterionDefinitionVersion,
    EvaluationPlanSnapshot,
    EvaluationResult,
    EvaluationRun,
    ModelCallAuthorizationReceipt,
    ResultRelation,
    RoutingDisposition,
    VerifyInvariantViolation,
    cothink_items,
    record_model_call_authorization,
    seed_terminology_exact_match,
    surfaced_results,
)
from work_buddy.cowork.verify_configuration import (
    create_user_verification_check,
    set_document_criterion_enabled,
)
from work_buddy.cowork.verify_coordination import (
    portable_coordination_jobs,
    record_coordination_status,
    sanitized_request_summary,
)
from work_buddy.cowork.verify_inspection import verify_run_detail
from work_buddy.cowork.verify_projection import (
    cothink_outcome_projection,
    result_projection,
)
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


def test_target_reference_granularity_is_trust_bound_and_legacy_block_safe():
    reference = {
        "schema": "wb.cowork.document-target/v1",
        "storeId": "store-1",
        "documentId": "doc-1",
        "kind": "text_range",
        "relative": {
            "startBase64": "AA==",
            "endBase64": "AQ==",
        },
        "quote": {
            "exact": "exact text",
            "prefix": "",
            "suffix": "",
        },
        "label": "Working on",
        "headingPath": ["Section"],
        "startBlockId": "block-a",
        "endBlockId": "block-b",
        "createdAt": NOW,
        "updatedAt": NOW,
    }

    legacy, legacy_digest = verify_orchestration._target_reference(
        reference,
        store_id="store-1",
        document_id="doc-1",
    )
    block, block_digest = verify_orchestration._target_reference(
        {**reference, "granularity": "block"},
        store_id="store-1",
        document_id="doc-1",
    )
    character, character_digest = verify_orchestration._target_reference(
        {**reference, "granularity": "character"},
        store_id="store-1",
        document_id="doc-1",
    )

    assert legacy is not None and legacy["granularity"] == "block"
    assert legacy["label"] == "Working on"
    assert legacy["headingPath"] == ["Section"]
    assert legacy["startBlockId"] == "block-a"
    assert block is not None and block["granularity"] == "block"
    assert character is not None and character["granularity"] == "character"
    assert legacy_digest == block_digest
    assert character_digest != block_digest

    with pytest.raises(
        VerifyOrchestrationError,
        match="granularity must be character or block",
    ):
        verify_orchestration._target_reference(
            {**reference, "granularity": "paragraph"},
            store_id="store-1",
            document_id="doc-1",
        )


def test_character_target_reference_digest_matches_browser_contract():
    reference = {
        "schema": "wb.cowork.document-target/v1",
        "storeId": "store-a",
        "documentId": "doc-a",
        "kind": "text_range",
        "granularity": "character",
        "relative": {
            "startBase64": "AQID",
            "endBase64": "BAUG",
        },
        "quote": {
            "exact": "  alpha   beta ",
            "prefix": "before\nline",
            "suffix": " after ",
        },
        "label": "Selected passage",
        "headingPath": ["Methods"],
        "createdAt": NOW,
        "updatedAt": NOW,
    }

    _normalized, digest = verify_orchestration._target_reference(
        reference,
        store_id="store-a",
        document_id="doc-a",
    )

    assert (
        digest
        == "46809563029c5d6b664bd3a0610af6da73b0b53afed1c7bf8b3fd5f56683b611"
    )


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


def _add_terminology_criterion(
    store,
    *,
    document_id: str,
    stable_key: str,
    non_preferred: str,
    preferred: str,
    admitted: bool,
):
    seeded = seed_terminology_exact_match(store)
    criterion_payload = {
        "stable_key": stable_key,
        "version": 1,
        "title": f"{stable_key} criterion",
        "description": f"Prefer {preferred}.",
        "criterion_kind": "terminology",
        "origin": "system",
        "configuration_schema": {"type": "object"},
    }
    criterion = CriterionDefinitionVersion(
        id=new_id(),
        stable_key=stable_key,
        version=1,
        title=criterion_payload["title"],
        description=criterion_payload["description"],
        criterion_kind="terminology",
        origin="system",
        configuration_schema_json=canonical_json(
            criterion_payload["configuration_schema"]
        ),
        canonical_sha256=sha256_text(canonical_json(criterion_payload)),
        created_at=NOW,
        created_by_kind="system",
        created_by_ref="multi-check-test",
        created_by_meta_json=None,
    )
    check = seeded.check
    created_check = None
    if not admitted:
        check_payload = {
            "stable_key": f"{stable_key}_check",
            "version": 1,
            "title": "Unadmitted test check",
            "mechanism": "deterministic",
            # Borrowing an admitted ref is not admission: the registry pins
            # the exact immutable check-definition identity and hash.
            "executor_ref": seeded.check.executor_ref,
            "supported_criterion_kinds": ["terminology"],
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "limitations": ["No admitted implementation."],
            "origin": "system",
        }
        created_check = CheckDefinitionVersion(
            id=new_id(),
            stable_key=check_payload["stable_key"],
            version=1,
            title=check_payload["title"],
            mechanism="deterministic",
            executor_ref=check_payload["executor_ref"],
            supported_criterion_kinds_json=canonical_json(
                check_payload["supported_criterion_kinds"]
            ),
            input_schema_json=canonical_json(
                check_payload["input_schema"]
            ),
            output_schema_json=canonical_json(
                check_payload["output_schema"]
            ),
            limitations_json=canonical_json(
                check_payload["limitations"]
            ),
            origin="system",
            canonical_sha256=sha256_text(canonical_json(check_payload)),
            created_at=NOW,
            created_by_kind="system",
            created_by_ref="multi-check-test",
            created_by_meta_json=None,
        )
        check = created_check
    configuration = {
        "terms": [
            {
                "non_preferred": non_preferred,
                "preferred": preferred,
            }
        ]
    }
    binding_payload = {
        "criterion_definition_version_id": criterion.id,
        "check_definition_version_id": check.id,
        "configuration": configuration,
    }
    binding = CriterionCheckBinding(
        id=new_id(),
        criterion_definition_version_id=criterion.id,
        check_definition_version_id=check.id,
        configuration_json=canonical_json(configuration),
        canonical_sha256=sha256_text(canonical_json(binding_payload)),
        created_at=NOW,
        created_by_kind="system",
        created_by_ref="multi-check-test",
        created_by_meta_json=None,
    )
    scope = {"kind": "document", "document_id": document_id}
    activation_payload = {
        "criterion_definition_version_id": criterion.id,
        "criterion_check_binding_id": binding.id,
        "scope": scope,
        "is_enabled": True,
        "is_required": False,
        "origin": "system",
    }
    activation = CriterionActivation(
        id=new_id(),
        criterion_definition_version_id=criterion.id,
        criterion_check_binding_id=binding.id,
        scope_json=canonical_json(scope),
        is_enabled=1,
        is_required=0,
        origin="system",
        canonical_sha256=sha256_text(canonical_json(activation_payload)),
        created_at=NOW,
        created_by_kind="system",
        created_by_ref="multi-check-test",
        created_by_meta_json=None,
    )
    records = [criterion]
    if created_check is not None:
        records.append(created_check)
    records.extend([binding, activation])
    with store.write_transaction() as conn:
        for record in records:
            verify_store.insert_record(store, record, conn=conn)
    return criterion, check, binding, activation


def _downgrade_runtime_job_to_legacy(
    ctx: dict[str, Any],
    job_id: str,
):
    """Rewrite one test job as a valid pre-execution-disclosure durable job."""

    job = get_job(job_id)
    assert job is not None
    request = deepcopy(dict(job.request))
    request.pop("recheck_target_confirmation", None)
    configuration = deepcopy(dict(request["effective_configuration"]))
    configuration.pop("execution_plan")
    coordination = dict(configuration["coordination"])
    coordination.pop("deprecated", None)
    coordination.pop("authoritative_projection", None)
    coordination.pop("cost_ceiling_semantics", None)
    configuration["coordination"] = coordination
    request["effective_configuration"] = configuration
    configuration_sha256 = sha256_text(canonical_json(configuration))
    request["effective_configuration_sha256"] = configuration_sha256
    request["effective_policy_sha256"] = (
        verify_orchestration._effective_policy_sha256(
            effective_configuration_sha256=configuration_sha256,
            active_criterion_ids=request["active_criterion_ids"],
        )
    )
    provisional = replace(job, request=request)
    context_sha256 = sha256_text(
        canonical_json(
            verify_orchestration._build_job_context(
                ctx["store"],
                provisional,
            )
        )
    )
    receipt = record_model_call_authorization(
        ctx["store"],
        action_snapshot_id=job.action_snapshot_id,
        plan_snapshot_id=job.plan_snapshot_id,
        provider=str(job.selection["provider_id"]),
        model=str(job.selection["model_id"]),
        context_sha256=context_sha256,
        content_boundary={
            "role": job.role.value,
            "job_id": job.job_id,
            "document": "complete_permitted_frozen_projection",
            "action_snapshot_id": job.action_snapshot_id,
            "authority_context": (
                verify_orchestration._authorization_authority_context(
                    request
                )
            ),
        },
        egress_class="account_backed_agent",
        cost_ceiling_usd=2.0,
        retry_limit=0,
        expires_at="2099-01-01T00:00:00.000+00:00",
        actor=HUMAN,
        at=job.created_at,
    )
    with sqlite3.connect(ctx["runtime_path"]) as conn:
        conn.execute(
            """
            UPDATE cowork_verify_jobs
            SET request_json = ?, context_sha256 = ?,
                authorization_receipt_id = ?
            WHERE job_id = ?
            """,
            (
                canonical_json(request),
                context_sha256,
                receipt.id,
                job.job_id,
            ),
        )
    downgraded = get_job(job.job_id)
    assert downgraded is not None
    return downgraded


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


def test_two_admitted_criteria_freeze_and_run_each_selected_binding(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx)
    store = orchestration_ctx["store"]
    seeded = seed_terminology_exact_match(store)
    second_criterion, second_check, second_binding, second_activation = (
        _add_terminology_criterion(
            store,
            document_id=capture["documentId"],
            stable_key="terminology_fixture_heading",
            non_preferred="Throwaway",
            preferred="Disposable",
            admitted=True,
        )
    )

    started = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Apply both admitted terminology policies.",
        protected_intent="Preserve the document's meaning.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )

    assert started["result_count"] == 2
    plan = verify_store.get_record(
        store,
        EvaluationPlanSnapshot,
        get_job(started["job_id"]).plan_snapshot_id,
    )
    assert plan is not None
    frozen_checks = json.loads(plan.plan_json)["checks"]
    assert {
        (
            item["criterion_definition_version_id"],
            item["check_definition_version_id"],
            item["criterion_check_binding_id"],
            item["criterion_activation_id"],
        )
        for item in frozen_checks
    } == {
        (
            seeded.criterion.id,
            seeded.check.id,
            seeded.binding.id,
            seeded.activation.id,
        ),
        (
            second_criterion.id,
            second_check.id,
            second_binding.id,
            second_activation.id,
        ),
    }
    executions = verify_store.list_records(
        store,
        CheckExecution,
        where="source.evaluation_run_id = ?",
        params=(started["run_id"],),
    )
    results = verify_store.list_records(
        store,
        EvaluationResult,
        where="source.evaluation_run_id = ?",
        params=(started["run_id"],),
    )
    assert len(executions) == 2
    assert {item.criterion_check_binding_id for item in executions} == {
        seeded.binding.id,
        second_binding.id,
    }
    assert len(results) == 2
    assert {item.criterion_definition_version_id for item in results} == {
        seeded.criterion.id,
        second_criterion.id,
    }
    execution_by_id = {item.id: item for item in executions}
    assert {
        execution_by_id[item.check_execution_id].criterion_check_binding_id
        for item in results
    } == {seeded.binding.id, second_binding.id}

    job = get_job(started["job_id"])
    assert job is not None
    context = get_worker_job(
        job_id=job.job_id,
        agent_session_id=job.session_id,
    )["context"]
    assert all(
        "criterion_definition_version_id" in item
        and "check_execution_id" in item
        for item in context["normalized_results"]
    )
    assert all(
        "criterion_check_binding_id" in item
        and "check_execution_id" in item
        for item in context["checks"]
    )

    replayed = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Apply both admitted terminology policies.",
        protected_intent="Preserve the document's meaning.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )
    assert replayed["replayed"] is True
    assert replayed["run_id"] == started["run_id"]
    assert len(
        verify_store.list_records(
            store,
            CheckExecution,
            where="source.evaluation_run_id = ?",
            params=(started["run_id"],),
        )
    ) == 2


def test_enabled_unadmitted_binding_fails_closed_before_run_or_launch(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx)
    store = orchestration_ctx["store"]
    _add_terminology_criterion(
        store,
        document_id=capture["documentId"],
        stable_key="unadmitted_fixture_check",
        non_preferred="Throwaway",
        preferred="Disposable",
        admitted=False,
    )

    with pytest.raises(
        VerifyOrchestrationError,
        match="unsupported or unadmitted bindings",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Do not bypass an unadmitted selection.",
            protected_intent="Fail closed before any model launch.",
            validate_selection=lambda selection: selection,
            spawn_detached=orchestration_ctx["spawn"],
        )

    assert (
        verify_store.list_records(store, EvaluationPlanSnapshot) == ()
    )
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


def test_verify_execution_disclosure_matches_exact_codex_authorization(
    orchestration_ctx: dict[str, Any],
):
    _, started = _start_verify(orchestration_ctx)
    store = orchestration_ctx["store"]
    plan = started["execution_plan"]

    assert plan["coordination"]["selection"] == {
        "mode": "explicit_at_run_start",
        **SELECTION.to_dict(),
    }
    assert plan["coordination"]["content_boundary"] == (
        "entire_frozen_document"
    )
    assert plan["coordination"]["fallback"] == {
        "provider_model_fallback": False,
        "failure_mode": "fail_closed",
    }
    assert plan["coordination"]["cost_control"] == {
        "provider_id": "codex",
        "enforcement_class": "unavailable",
        "ceiling_usd_per_worker_session": None,
        "basis": "codex_worker_has_no_budget_enforcement",
    }

    job = get_job(started["job_id"])
    assert job is not None
    assert (
        job.request["effective_configuration"]["execution_plan"]
        == plan
    )
    receipt = verify_store.get_record(
        store,
        ModelCallAuthorizationReceipt,
        job.authorization_receipt_id,
    )
    assert receipt is not None
    assert receipt.provider == SELECTION.provider_id
    assert receipt.model == SELECTION.model_id
    assert receipt.egress_class == "account_backed_agent"
    assert receipt.retry_limit == 0
    # Dispatch still passes its bounded launch-budget request. The disclosure
    # truthfully does not present this as a Codex-enforced ceiling.
    assert receipt.cost_ceiling_usd == 2.0
    detail = verify_run_detail(
        store,
        document_id=job.document_id,
        run_id=started["run_id"],
    )
    historical_plan = detail["coordination"][0]["execution_plan"]
    assert historical_plan == plan
    assert historical_plan["coordination"]["cost_control"][
        "enforcement_class"
    ] == "unavailable"


def test_pre_disclosure_job_replays_with_validated_synthesized_plan(
    orchestration_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    original_record = verify_orchestration.record_coordination_status
    monkeypatch.setattr(
        verify_orchestration,
        "record_coordination_status",
        lambda *_args, **_kwargs: None,
    )
    capture, started = _start_verify(orchestration_ctx)
    monkeypatch.setattr(
        verify_orchestration,
        "record_coordination_status",
        original_record,
    )
    legacy = _downgrade_runtime_job_to_legacy(
        orchestration_ctx,
        started["job_id"],
    )
    assert (
        legacy.request["effective_configuration"].get("execution_plan")
        is None
    )
    spawn_count = len(orchestration_ctx["spawned"])

    replay = start_verify_run(
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

    assert replay["replayed"] is True
    assert replay["execution_plan"]["coordination"]["selection"] == {
        "mode": "explicit_at_run_start",
        **SELECTION.to_dict(),
    }
    assert len(orchestration_ctx["spawned"]) == spawn_count


def test_pre_disclosure_job_synthesizes_bound_plan_for_both_children(
    orchestration_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    original_record = verify_orchestration.record_coordination_status
    monkeypatch.setattr(
        verify_orchestration,
        "record_coordination_status",
        lambda *_args, **_kwargs: None,
    )
    _, started = _start_verify(orchestration_ctx)
    monkeypatch.setattr(
        verify_orchestration,
        "record_coordination_status",
        original_record,
    )
    legacy = _downgrade_runtime_job_to_legacy(
        orchestration_ctx,
        started["job_id"],
    )
    record_coordination_status(orchestration_ctx["store"], legacy)
    result_ids = [
        result.id
        for result in verify_store.list_records(
            orchestration_ctx["store"],
            verify_orchestration.EvaluationResult,
        )
        if result.evaluation_run_id == legacy.evaluation_run_id
    ]
    reviser, _ = verify_orchestration._create_and_launch_reviser(
        orchestration_ctx["store"],
        legacy,
        requested_revision_result_ids=result_ids,
        disposition_ids=[],
        spawn_detached=orchestration_ctx["spawn"],
    )
    reviser_configuration = reviser.request["effective_configuration"]
    assert reviser_configuration["execution_plan"]["coordination"][
        "selection"
    ] == {
        "mode": "explicit_at_run_start",
        **SELECTION.to_dict(),
    }
    assert reviser.request["effective_configuration_sha256"] == sha256_text(
        canonical_json(reviser_configuration)
    )
    assert reviser.request["effective_policy_sha256"] == (
        verify_orchestration._effective_policy_sha256(
            effective_configuration_sha256=reviser.request[
                "effective_configuration_sha256"
            ],
            active_criterion_ids=reviser.request["active_criterion_ids"],
        )
    )

    coordinator = verify_orchestration._create_and_launch_coordinator(
        orchestration_ctx["store"],
        reviser,
        spawn_detached=orchestration_ctx["spawn"],
    )
    assert (
        coordinator.request["effective_configuration"]["execution_plan"]
        == reviser_configuration["execution_plan"]
    )
    assert coordinator.request["coordinator_stage"] == "post_revision"


def test_pre_disclosure_upgrade_rejects_selection_tampering(
    orchestration_ctx: dict[str, Any],
):
    _, started = _start_verify(orchestration_ctx)
    legacy = _downgrade_runtime_job_to_legacy(
        orchestration_ctx,
        started["job_id"],
    )
    tampered_selection = {
        **legacy.selection,
        "provider_id": "claude-code",
        "model_id": "sonnet",
    }
    with sqlite3.connect(orchestration_ctx["runtime_path"]) as conn:
        conn.execute(
            "UPDATE cowork_verify_jobs SET selection_json = ? "
            "WHERE job_id = ?",
            (canonical_json(tampered_selection), legacy.job_id),
        )
    tampered = get_job(legacy.job_id)
    assert tampered is not None

    with pytest.raises(
        VerifyOrchestrationError,
        match="no longer matches its exact authorization",
    ):
        verify_orchestration._validated_execution_request(
            orchestration_ctx["store"],
            tampered,
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


def _specialist_assignment_fixture() -> dict[str, Any]:
    return {
        "criterion_definition_version_id": "a1" * 16,
        "check_definition_version_id": "b2" * 16,
        "criterion_check_binding_id": "c3" * 16,
        "sequence": 1,
        "total": 2,
        "configuration_sha256": "d4" * 32,
    }


def test_portable_specialist_assignment_is_exact_and_role_scoped():
    assignment = _specialist_assignment_fixture()

    summary = sanitized_request_summary(
        CoworkVerifyRole.SPECIALIST,
        {
            "user_goal": "Check one admitted criterion.",
            "protected_intent": "Preserve the captured target.",
            "specialist_assignment": assignment,
        },
    )
    coordinator_summary = sanitized_request_summary(
        CoworkVerifyRole.COORDINATOR,
        {
            "user_goal": "Coordinate the complete run.",
            "protected_intent": "Preserve the captured target.",
            "specialist_assignment": assignment,
        },
    )

    assert summary["specialist_assignment"] == assignment
    assert coordinator_summary["specialist_assignment"] is None


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"sequence": 0}, "sequence must be a positive integer"),
        ({"sequence": 3}, "sequence cannot exceed total"),
        ({"total": False}, "total must be a positive integer"),
        (
            {"criterion_definition_version_id": "not-an-id"},
            "criterion_definition_version_id must be a lowercase 32-hex id",
        ),
        (
            {"configuration_sha256": "not-a-digest"},
            "configuration_sha256 must be a lowercase SHA-256 digest",
        ),
        (
            {"private_worker_prompt": "must not survive"},
            "must contain exactly its admitted fields",
        ),
    ],
)
def test_portable_specialist_assignment_rejects_invalid_bindings(
    mutation: dict[str, Any],
    match: str,
):
    assignment = {**_specialist_assignment_fixture(), **mutation}

    with pytest.raises(VerifyInvariantViolation, match=match):
        sanitized_request_summary(
            CoworkVerifyRole.SPECIALIST,
            {
                "user_goal": "Check one admitted criterion.",
                "protected_intent": "Preserve the captured target.",
                "specialist_assignment": assignment,
            },
        )


def test_specialist_completion_and_lineage_survive_truth_export(
    orchestration_ctx: dict[str, Any],
    tmp_path: Path,
):
    capture = _capture(orchestration_ctx)
    store = orchestration_ctx["store"]
    created = create_user_verification_check(
        store,
        document_id=capture["documentId"],
        title="Portable clarity check",
        description="Identify wording that may be unclear.",
        evaluation_instructions="Review the captured target for unclear wording.",
        actor=HUMAN,
        at=NOW,
    )
    assert created["status"] == "active"
    started = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Evaluate the selected checks.",
        protected_intent="Preserve the captured target.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )
    specialist = get_job(started["job_id"])
    assert specialist is not None
    assert specialist.role is CoworkVerifyRole.SPECIALIST
    submit_worker_job(
        job_id=specialist.job_id,
        payload={
            "results": [
                {
                    "result_kind": "conforming",
                    "severity": "info",
                    "message": "No clarity issue was found.",
                    "evidence": None,
                    "coverage": "complete_target_review",
                    "limitations": ["Model judgment is advisory."],
                }
            ],
            "summary": "The complete captured target was reviewed.",
        },
        agent_session_id=specialist.session_id,
        spawn_detached=orchestration_ctx["spawn"],
    )
    execution = next(
        item
        for item in verify_store.list_records(
            store,
            CheckExecution,
            where="source.evaluation_run_id = ?",
            params=(started["run_id"],),
        )
        if json.loads(item.producer_json).get("job_id") == specialist.job_id
    )
    results = verify_store.list_records(
        store,
        EvaluationResult,
        where="source.check_execution_id = ?",
        params=(execution.id,),
    )
    assert results

    specialist_history = next(
        item
        for item in portable_coordination_jobs(
            store,
            document_id=capture["documentId"],
        )
        if item["job_id"] == specialist.job_id
    )
    assert specialist_history["request_summary"][
        "specialist_assignment"
    ]["criterion_check_binding_id"] == execution.criterion_check_binding_id
    assert specialist_history["consequence_refs"][
        "check_execution_ids"
    ] == [execution.id]
    assert specialist_history["consequence_refs"][
        "evaluation_result_ids"
    ] == [result.id for result in results]

    exported = export_store(
        store,
        tmp_path / "portable-specialist.jsonl",
    )
    target = tmp_path / "portable-specialist-target"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store
    restored_specialist = next(
        item
        for item in portable_coordination_jobs(
            restored,
            document_id=capture["documentId"],
        )
        if item["job_id"] == specialist.job_id
    )
    assert restored_specialist == specialist_history


def test_ranged_specialist_context_contains_only_the_captured_target(
    orchestration_ctx: dict[str, Any],
):
    exact = "Co-work scope"
    capture = _capture(orchestration_ctx, target_text=exact)
    selector = capture["target"]["selector"]
    capture["target"]["targetReference"] = {
        "schema": "wb.cowork.document-target/v1",
        "storeId": capture["storeId"],
        "documentId": capture["documentId"],
        "kind": "text_range",
        "granularity": "character",
        "relative": {
            "startBase64": "AA==",
            "endBase64": "AQ==",
        },
        "quote": {
            "exact": exact,
            "prefix": selector["prefix"],
            "suffix": selector["suffix"],
        },
        "label": "Working on",
        "headingPath": [],
        "createdAt": NOW,
        "updatedAt": NOW,
    }
    store = orchestration_ctx["store"]
    created = create_user_verification_check(
        store,
        document_id=capture["documentId"],
        title="Clarity",
        description="Identify wording that may be unclear in the captured passage.",
        evaluation_instructions=(
            "Review only the captured passage and identify unclear wording."
        ),
        actor=HUMAN,
        at=NOW,
    )
    assert created["status"] == "active"

    started = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Evaluate the selected checks.",
        protected_intent="Preserve the captured passage.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )
    specialist = get_job(started["job_id"])
    assert specialist is not None
    assert specialist.role is CoworkVerifyRole.SPECIALIST
    context = get_worker_job(
        job_id=specialist.job_id,
        agent_session_id=specialist.session_id,
    )["context"]

    assert context["target"] == {
        "kind": "text_quote",
        "text": exact,
        "text_sha256": sha256_text(exact),
    }
    assert set(context["document"]) == {"document_version_id"}
    serialized = canonical_json(context)
    assert selector["prefix"] not in serialized
    assert selector["suffix"] not in serialized
    assert "target_reference" not in serialized
    assert "Throwaway Verify orchestration fixture" not in serialized


def test_specialist_fanout_limit_fails_before_run_side_effects(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx)
    store = orchestration_ctx["store"]
    limit = verify_orchestration.MAX_VERIFY_SPECIALIST_CHECKS_PER_RUN
    for index in range(limit + 1):
        created = create_user_verification_check(
            store,
            document_id=capture["documentId"],
            title=f"Personal check {index + 1}",
            description=f"Evaluate requirement {index + 1}.",
            evaluation_instructions=f"Evaluate requirement {index + 1}.",
            actor=HUMAN,
            at=NOW,
        )
        assert created["status"] == "active"

    portable_types = (
        ActionSnapshot,
        EvaluationPlanSnapshot,
        EvaluationRun,
        ModelCallAuthorizationReceipt,
        CoworkCoordinationJob,
        CoworkCoordinationStatusEvent,
    )
    counts_before = {
        record_type: len(verify_store.list_records(store, record_type))
        for record_type in portable_types
    }
    blobs_before = {
        path.name for path in store.paths.blobs.iterdir() if path.is_file()
    }
    runtime_existed_before = orchestration_ctx["runtime_path"].exists()
    spawned_before = tuple(orchestration_ctx["spawned"])

    with pytest.raises(
        VerifyOrchestrationError,
        match=rf"at most {limit} selected account-backed checks",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Evaluate all selected checks.",
            protected_intent="Preserve the document.",
            validate_selection=lambda selection: selection,
            spawn_detached=orchestration_ctx["spawn"],
        )

    assert {
        record_type: len(verify_store.list_records(store, record_type))
        for record_type in portable_types
    } == counts_before
    assert {
        path.name for path in store.paths.blobs.iterdir() if path.is_file()
    } == blobs_before
    assert orchestration_ctx["runtime_path"].exists() is runtime_existed_before
    assert tuple(orchestration_ctx["spawned"]) == spawned_before


def test_runnable_model_checks_execute_sequentially_before_coordination(
    orchestration_ctx: dict[str, Any],
):
    capture = _capture(orchestration_ctx)
    store = orchestration_ctx["store"]
    for title, instructions in (
        (
            "Positive framing",
            "Identify wording that defines the subject by negation.",
        ),
        (
            "Reader clarity",
            "Identify wording that would be unclear to an unfamiliar reader.",
        ),
    ):
        created = create_user_verification_check(
            store,
            document_id=capture["documentId"],
            title=title,
            description=instructions,
            evaluation_instructions=instructions,
            actor=HUMAN,
            at=NOW,
        )
        assert created["status"] == "active"

    started = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Run the selected checks, then reconcile their results.",
        protected_intent="Preserve the author's substantive meaning.",
        validate_selection=lambda selection: selection,
        spawn_detached=orchestration_ctx["spawn"],
    )

    assert started["stage"] == "checking"
    assert started["result_count"] == 1
    first = get_job(started["job_id"])
    assert first is not None
    assert first.role is CoworkVerifyRole.SPECIALIST
    first_assignment = first.request["specialist_assignment"]
    assert first_assignment["sequence"] == 1
    assert first_assignment["total"] == 2
    first_context = get_worker_job(
        job_id=first.job_id,
        agent_session_id=first.session_id,
    )["context"]
    assert "frozen_markdown" not in first_context["document"]
    assert first_context["target"]["text"] == BODY
    assert len(first_context["criteria"]) == 1
    assert first_context["normalized_results"] == []
    first_receipt = verify_store.get_record(
        store,
        ModelCallAuthorizationReceipt,
        first.authorization_receipt_id,
    )
    assert first_receipt is not None
    assert json.loads(first_receipt.content_boundary_json)["document"] == (
        "captured_target_only"
    )

    conforming_payload = {
        "results": [
            {
                "result_kind": "conforming",
                "severity": "info",
                "message": "No issue found by this check.",
                "evidence": None,
                "coverage": "complete_target_review",
                "limitations": ["Model judgment is advisory."],
            }
        ],
        "summary": "The complete target was reviewed.",
    }
    incomplete_conforming = deepcopy(conforming_payload)
    incomplete_conforming["results"][0]["coverage"] = "partial_target_review"
    with pytest.raises(
        VerifyOrchestrationError,
        match="requires complete target review",
    ):
        submit_worker_job(
            job_id=first.job_id,
            payload=incomplete_conforming,
            agent_session_id=first.session_id,
            spawn_detached=orchestration_ctx["spawn"],
        )
    assert get_job(first.job_id).status == "running"
    first_submission = submit_worker_job(
        job_id=first.job_id,
        payload=conforming_payload,
        agent_session_id=first.session_id,
        spawn_detached=orchestration_ctx["spawn"],
    )
    second = get_job(first_submission["next_job_id"])
    assert second is not None
    assert second.role is CoworkVerifyRole.SPECIALIST
    assert second.parent_job_id == first.job_id
    assert second.request["specialist_assignment"]["sequence"] == 2

    finding_payload = {
        "results": [
            {
                "result_kind": "finding",
                "severity": "warning",
                "message": "The phrase may be unclear without product context.",
                "evidence": {
                    "exact": "Co-work scope",
                    "prefix": "model-supplied inconsistent context",
                    "suffix": "model-supplied inconsistent context",
                },
                "coverage": "complete_target_review",
                "limitations": ["Clarity depends on the intended audience."],
            }
        ],
        "summary": "One clarity concern was found.",
    }
    second_submission = submit_worker_job(
        job_id=second.job_id,
        payload=finding_payload,
        agent_session_id=second.session_id,
        spawn_detached=orchestration_ctx["spawn"],
    )
    coordinator = get_job(second_submission["next_job_id"])
    assert coordinator is not None
    assert coordinator.role is CoworkVerifyRole.COORDINATOR
    assert coordinator.parent_job_id is None
    coordinator_context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]
    assert coordinator_context["document"]["frozen_markdown"] == BODY
    assert len(coordinator_context["normalized_results"]) == 3

    executions = verify_store.list_records(
        store,
        CheckExecution,
        where="source.evaluation_run_id = ?",
        params=(started["run_id"],),
    )
    results = verify_store.list_records(
        store,
        EvaluationResult,
        where="source.evaluation_run_id = ?",
        params=(started["run_id"],),
    )
    assert len(executions) == 3
    assert len(results) == 3
    specialist_results = [
        result
        for result in results
        if json.loads(
            next(
                execution.producer_json
                for execution in executions
                if execution.id == result.check_execution_id
            )
        ).get("kind")
        == "account_backed_specialist"
    ]
    assert len(specialist_results) == 2
    model_finding = next(
        result
        for result in specialist_results
        if result.result_kind == "finding"
    )
    selector = CompositeSelector.from_web_annotation(
        json.loads(model_finding.evidence_selector_json)
    )
    quote_start = BODY.index("Co-work scope")
    assert selector.start == quote_start
    assert selector.end == quote_start + len("Co-work scope")
    assert selector.prefix == BODY[max(0, quote_start - 120) : quote_start]
    assert selector.suffix == BODY[
        quote_start + len("Co-work scope") :
        quote_start + len("Co-work scope") + 120
    ]

    decisions = [
        {
            "evaluation_result_id": result.id,
            "decision": (
                "request_revision"
                if result.id == model_finding.id
                else "retain"
            ),
            "rationale": "Test the revision authority boundary.",
        }
        for result in results
    ]
    with pytest.raises(
        VerifyOrchestrationError,
        match="no admitted deterministic revision evaluator",
    ):
        submit_worker_job(
            job_id=coordinator.job_id,
            payload={
                "decisions": decisions,
                "summary": "Attempted an unsupported model-based revision.",
            },
            agent_session_id=coordinator.session_id,
            spawn_detached=orchestration_ctx["spawn"],
        )
    assert get_job(coordinator.job_id).status == "running"

    for decision in decisions:
        if decision["evaluation_result_id"] == model_finding.id:
            decision["decision"] = "surface"
            decision["rationale"] = "Show the evidence-backed advisory finding."
    completed = submit_worker_job(
        job_id=coordinator.job_id,
        payload={
            "decisions": decisions,
            "summary": "The specialist findings were reconciled.",
        },
        agent_session_id=coordinator.session_id,
        spawn_detached=orchestration_ctx["spawn"],
    )
    assert completed["status"] == "completed"
    assert completed.get("next_job_id") is None
    assert [
        item["id"]
        for item in surfaced_results(
            store,
            document_id=capture["documentId"],
        )
    ] == [model_finding.id]
    document = documents.get_document(store, capture["documentId"])
    projected_result = result_projection(store, document)[0]
    assert projected_result["method_label"] == (
        "Instruction-based model evaluation"
    )
    assert projected_result["coverage_label"] == (
        "Model review of the complete frozen target"
    )
    assert "Clarity depends on the intended audience." in projected_result[
        "limitations"
    ]

    replay = submit_worker_job(
        job_id=second.job_id,
        payload=finding_payload,
        agent_session_id=second.session_id,
        spawn_detached=orchestration_ctx["spawn"],
    )
    assert replay["replayed"] is True
    assert replay["next_job_id"] == coordinator.job_id
    assert len(
        verify_store.list_records(
            store,
            CheckExecution,
            where="source.evaluation_run_id = ?",
            params=(started["run_id"],),
        )
    ) == 3
    summary = next(
        item
        for item in run_status_projection(
            store,
            document_id=capture["documentId"],
        )
        if item["run_id"] == started["run_id"]
    )
    assert summary["status"] == "completed"
    assert summary["coordination_status"] == "completed"
