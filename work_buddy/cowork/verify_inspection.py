"""Safe, portable inspection projection for one Co-work Verify run.

The projection deliberately excludes raw coordinator/reviser prose and host
process output. It exposes only immutable Truth records, typed dispositions,
lineage, bounded authorization metadata, and sanitized runtime state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from work_buddy.agent_execution.models import AgentExecutionSelection

from work_buddy.cowork.verify import (
    ActionSnapshot,
    CheckDefinitionVersion,
    CheckExecution,
    CriterionDefinitionVersion,
    EvaluationPlanSnapshot,
    EvaluationResult,
    EvaluationRun,
    ModelCallAuthorizationReceipt,
    ResultRelation,
    RoutingDisposition,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_coordination import (
    coordination_jobs_with_runtime_fallback,
)
from work_buddy.cowork.verify_execution import (
    verify_execution_disclosure_plan,
)
from work_buddy.cowork.verify_jobs import MAX_VERIFY_JOB_BUDGET_USD
from work_buddy.truth.store import TruthStore


class VerifyInspectionError(ValueError):
    """A run cannot be safely inspected for the requested document."""


def _record(
    store: TruthStore,
    record_type: type[Any],
    record_id: str,
) -> Any:
    record = verify_store.get_record(store, record_type, record_id)
    if record is None:
        raise VerifyInspectionError(
            f"{record_type.__name__} does not exist: {record_id}"
        )
    return record


def _json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _job_execution_plan(
    job: Mapping[str, Any],
    authorization: ModelCallAuthorizationReceipt,
) -> dict[str, Any] | None:
    """Return the exact persisted plan, or its validated legacy equivalent."""

    selection_value = job.get("selection")
    if not isinstance(selection_value, Mapping):
        raise VerifyInspectionError("Verify coordination selection is invalid")
    try:
        selection = AgentExecutionSelection(
            provider_id=str(selection_value.get("provider_id") or ""),
            model_id=str(selection_value.get("model_id") or ""),
            provider_label=str(selection_value.get("provider_label") or ""),
            model_label=str(selection_value.get("model_label") or ""),
        )
    except ValueError as exc:
        raise VerifyInspectionError(
            "Verify coordination selection is invalid"
        ) from exc
    if (
        authorization.provider != selection.provider_id
        or authorization.model != selection.model_id
        or authorization.egress_class != "account_backed_agent"
        or authorization.cost_ceiling_usd != MAX_VERIFY_JOB_BUDGET_USD
        or authorization.retry_limit != 0
    ):
        raise VerifyInspectionError(
            "Verify coordination authorization does not match its selection"
        )
    expected = verify_execution_disclosure_plan(selection)
    request_summary = job.get("request_summary")
    if not isinstance(request_summary, Mapping):
        return None
    configuration = request_summary.get("effective_configuration")
    if not isinstance(configuration, Mapping):
        return None
    stored = configuration.get("execution_plan")
    if stored is not None and (
        not isinstance(stored, Mapping) or dict(stored) != expected
    ):
        raise VerifyInspectionError(
            "Verify coordination execution plan failed integrity validation"
        )
    return expected


def verify_run_detail(
    store: TruthStore,
    *,
    document_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Project one run without disclosing untyped private worker output."""

    run = _record(store, EvaluationRun, run_id)
    action = _record(store, ActionSnapshot, run.action_snapshot_id)
    if action.document_id != document_id:
        raise VerifyInspectionError(
            "Verify run does not belong to this document"
        )
    plan = _record(store, EvaluationPlanSnapshot, run.plan_snapshot_id)
    executions = [
        execution
        for execution in verify_store.list_records(store, CheckExecution)
        if execution.evaluation_run_id == run.id
    ]
    results = [
        result
        for result in verify_store.list_records(store, EvaluationResult)
        if result.evaluation_run_id == run.id
    ]
    result_ids = {result.id for result in results}
    dispositions = [
        disposition
        for disposition in verify_store.list_records(store, RoutingDisposition)
        if disposition.evaluation_result_id in result_ids
    ]
    relations = [
        relation
        for relation in verify_store.list_records(store, ResultRelation)
        if relation.evaluation_result_id in result_ids
    ]
    checks = {
        execution.check_definition_version_id: _record(
            store,
            CheckDefinitionVersion,
            execution.check_definition_version_id,
        )
        for execution in executions
    }
    criteria = {
        result.criterion_definition_version_id: _record(
            store,
            CriterionDefinitionVersion,
            result.criterion_definition_version_id,
        )
        for result in results
    }
    coordination_jobs = coordination_jobs_with_runtime_fallback(
        store,
        evaluation_run_id=run.id,
    )
    authorizations = {
        str(job["authorization_receipt_id"]): _record(
            store,
            ModelCallAuthorizationReceipt,
            str(job["authorization_receipt_id"]),
        )
        for job in coordination_jobs
    }

    dispositions_by_result: dict[str, list[RoutingDisposition]] = {
        result.id: [] for result in results
    }
    for disposition in dispositions:
        dispositions_by_result[disposition.evaluation_result_id].append(
            disposition
        )
    relations_by_result: dict[str, list[ResultRelation]] = {
        result.id: [] for result in results
    }
    for relation in relations:
        relations_by_result[relation.evaluation_result_id].append(relation)

    return {
        "schema": "work-buddy.cowork-verify-run-inspection/v1",
        "run_id": run.id,
        "run_kind": run.run_kind,
        "truth_status": run.status,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "action": {
            "action_snapshot_id": action.id,
            "structured_head_sha256": action.structured_head_sha256,
            "projection_sha256": action.projection_sha256,
            "target_kind": action.target_kind,
            "target_selector": _json(action.target_selector_json),
            "context_boundary": _json(action.context_boundary_json),
            "allowed_change_ranges": _json(
                action.allowed_change_ranges_json
            ),
            "egress_boundary": _json(action.egress_boundary_json),
            "canonical_sha256": action.canonical_sha256,
            "created_at": action.created_at,
        },
        "plan": {
            "plan_snapshot_id": plan.id,
            "definition": _json(plan.plan_json),
            "canonical_sha256": plan.canonical_sha256,
            "created_at": plan.created_at,
        },
        "checks": [
            {
                "check_execution_id": execution.id,
                "status": execution.status,
                "mechanism": execution.mechanism,
                "input_sha256": execution.input_sha256,
                "output_sha256": execution.output_sha256,
                "diagnostics": _json(execution.diagnostics_json),
                "producer": _json(execution.producer_json),
                "started_at": execution.started_at,
                "completed_at": execution.completed_at,
                "definition": {
                    "id": checks[
                        execution.check_definition_version_id
                    ].id,
                    "stable_key": checks[
                        execution.check_definition_version_id
                    ].stable_key,
                    "version": checks[
                        execution.check_definition_version_id
                    ].version,
                    "title": checks[
                        execution.check_definition_version_id
                    ].title,
                    "limitations": _json(
                        checks[
                            execution.check_definition_version_id
                        ].limitations_json
                    ),
                },
            }
            for execution in executions
        ],
        "results": [
            {
                "evaluation_result_id": result.id,
                "kind": result.result_kind,
                "severity": result.severity,
                "message": result.message,
                "evidence_selector": _json(result.evidence_selector_json),
                "payload": _json(result.payload_json),
                "canonical_sha256": result.canonical_sha256,
                "criterion": {
                    "id": criteria[
                        result.criterion_definition_version_id
                    ].id,
                    "stable_key": criteria[
                        result.criterion_definition_version_id
                    ].stable_key,
                    "version": criteria[
                        result.criterion_definition_version_id
                    ].version,
                    "title": criteria[
                        result.criterion_definition_version_id
                    ].title,
                },
                "dispositions": [
                    {
                        "id": disposition.id,
                        "decision": disposition.decision,
                        "rationale": disposition.rationale,
                        "policy_snapshot_sha256": (
                            disposition.policy_snapshot_sha256
                        ),
                        "created_at": disposition.created_at,
                    }
                    for disposition in dispositions_by_result[result.id]
                ],
                "lineage": [
                    {
                        "relation": relation.relation_kind,
                        "target_kind": relation.target_kind,
                        "target_ref": relation.target_ref,
                    }
                    for relation in relations_by_result[result.id]
                ],
                "created_at": result.created_at,
            }
            for result in results
        ],
        "coordination": [
            {
                "job_id": job["job_id"],
                "role": job["role"],
                "status": job["status"],
                "outcome_kind": job["outcome_kind"],
                "parent_job_id": job["parent_job_id"],
                "provider": authorizations[
                    job["authorization_receipt_id"]
                ].provider,
                "model": authorizations[
                    job["authorization_receipt_id"]
                ].model,
                "selection": job["selection"],
                "execution_plan": _job_execution_plan(
                    job,
                    authorizations[job["authorization_receipt_id"]],
                ),
                "context_sha256": job["context_sha256"],
                "request_summary": job["request_summary"],
                "candidate_lineage": {
                    "parent_job_id": job["parent_job_id"],
                    "requested_revision_result_ids": job[
                        "request_summary"
                    ].get("requested_revision_result_ids", []),
                    "affected_evaluations": job["request_summary"].get(
                        "candidate_evaluations",
                        [],
                    ),
                    "output_sha256": job["output_sha256"],
                    "consequence_refs": job["consequence_refs"],
                },
                "content_boundary": _json(
                    authorizations[
                        job["authorization_receipt_id"]
                    ].content_boundary_json
                ),
                "egress_class": authorizations[
                    job["authorization_receipt_id"]
                ].egress_class,
                "cost_ceiling_usd": authorizations[
                    job["authorization_receipt_id"]
                ].cost_ceiling_usd,
                "retry_limit": authorizations[
                    job["authorization_receipt_id"]
                ].retry_limit,
                "authorization_expires_at": authorizations[
                    job["authorization_receipt_id"]
                ].expires_at,
                "error_code": job["error_code"],
                "error": job["message"],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
            }
            for job in coordination_jobs
        ],
    }


__all__ = [
    "VerifyInspectionError",
    "verify_run_detail",
]
