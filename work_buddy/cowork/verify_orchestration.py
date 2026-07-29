"""End-to-end orchestration for Co-work Verify and explicit Co-think.

Narrow checks never publish directly.  Their immutable results are returned to
a job-scoped coordinator that receives the complete permitted frozen document,
the user goal and protected intent, every result, prior dispositions named by
the run, and any separately drafted candidate.  Only the coordinator's typed
submission can append a surfacing disposition or cause the server to create an
ordinary human-reviewable proposal.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.cowork.execution_identity import (
    CoworkVerifyRole,
    cowork_verify_job_from_session,
    cowork_verify_job_session_id,
)
from work_buddy.cowork.verify import (
    ActionSnapshot,
    CheckDefinitionVersion,
    CheckExecution,
    CothinkItem,
    CriterionCheckBinding,
    CriterionDefinitionVersion,
    EvaluationPlanSnapshot,
    EvaluationResult,
    EvaluationRun,
    ModelCallAuthorizationReceipt,
    ResultRelation,
    RoutingDisposition,
    VerifyInvariantViolation,
    create_action_snapshot,
    record_cothink_item,
    record_model_call_authorization,
    record_result_relation,
    record_routing_disposition,
    run_terminology_exact_match,
    seed_terminology_exact_match,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_configuration import (
    list_effective_verification_configuration,
)
from work_buddy.cowork.verify_candidate_evaluation import (
    CandidateEvaluationError,
    evaluate_terminology_candidate,
    sanitize_candidate_evaluations,
)
from work_buddy.cowork.verify_coordination import (
    portable_coordination_jobs,
    record_coordination_status,
    root_coordination_for_run,
)
from work_buddy.cowork.verify_jobs import (
    MAX_VERIFY_JOB_BUDGET_USD,
    VerifyJobSpawnMetadata,
    spawn_verify_job,
)
from work_buddy.cowork.verify_runtime import (
    VerifyRuntimeJob,
    claim_job_launch,
    claim_job_projection,
    create_job,
    get_job,
    jobs_for_document,
    jobs_for_run,
    redact_job_output,
    update_job,
)
from work_buddy.cowork.verify_rechecks import validate_recheck_intent
from work_buddy.truth import documents, proposals
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import (
    canonical_json,
    new_id,
    sha256_bytes,
    sha256_text,
    utc_now,
)
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import DocumentRecord, TruthStore


VERIFY_CONTRACT_VERSION = 1
_AUTHORIZATION_TTL = timedelta(hours=24)
_ACTION_TARGET_SOURCES = frozenset(
    {
        "working_target",
        "current_selection",
        "current_section",
        "custom_range",
        "whole_document",
    }
)
_INITIAL_COORDINATOR_DECISIONS = frozenset(
    {"retain", "defer", "surface", "request_revision"}
)
_SECOND_COORDINATOR_DECISIONS = frozenset(
    {"retain", "defer", "surface", "route_to_correction"}
)
_PERSISTED_COORDINATOR_DECISIONS = {
    "retain": "suppress",
    "defer": "defer",
    "surface": "surface",
    "route_to_correction": "route_to_correction",
}

SelectionValidator = Callable[
    [AgentExecutionSelection],
    AgentExecutionSelection,
]
SpawnDetached = Callable[..., Any]


class VerifyOrchestrationError(VerifyInvariantViolation):
    """An exact run or constrained worker transition is invalid."""


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise VerifyOrchestrationError(f"{label} must be an object")
    return dict(value)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerifyOrchestrationError(f"{label} must be a nonempty string")
    return value


def _decode_base64(value: object, label: str) -> bytes:
    text = _required_text(value, label)
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise VerifyOrchestrationError(f"{label} must be canonical base64") from exc


def _target_reference(
    value: object,
    *,
    store_id: str,
    document_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a browser-authored Yjs target identity without resolving it."""

    if value is None:
        return None, None
    reference = _mapping(value, "target.targetReference")
    if reference.get("schema") != "wb.cowork.document-target/v1":
        raise VerifyOrchestrationError(
            "target.targetReference has an unsupported schema"
        )
    if reference.get("storeId") != store_id:
        raise VerifyOrchestrationError(
            "target.targetReference belongs to another store"
        )
    if reference.get("documentId") != document_id:
        raise VerifyOrchestrationError(
            "target.targetReference belongs to another document"
        )
    if reference.get("kind") != "text_range":
        raise VerifyOrchestrationError(
            "target.targetReference must identify a text range"
        )
    relative = _mapping(
        reference.get("relative"),
        "target.targetReference.relative",
    )
    quote = _mapping(
        reference.get("quote"),
        "target.targetReference.quote",
    )
    start = _required_text(
        relative.get("startBase64"),
        "target.targetReference.relative.startBase64",
    )
    end = _required_text(
        relative.get("endBase64"),
        "target.targetReference.relative.endBase64",
    )
    _decode_base64(start, "target.targetReference.relative.startBase64")
    _decode_base64(end, "target.targetReference.relative.endBase64")
    exact = _required_text(
        quote.get("exact"),
        "target.targetReference.quote.exact",
    )
    prefix = quote.get("prefix", "")
    suffix = quote.get("suffix", "")
    if not isinstance(prefix, str) or not isinstance(suffix, str):
        raise VerifyOrchestrationError(
            "target.targetReference quote context must be text"
        )
    identity = {
        "schema": "wb.cowork.document-target/v1",
        "storeId": store_id,
        "documentId": document_id,
        "kind": "text_range",
        "relative": {
            "startBase64": start,
            "endBase64": end,
        },
        "quote": {
            "exact": exact,
            "prefix": prefix,
            "suffix": suffix,
        },
    }
    return reference, sha256_text(canonical_json(identity))


def _read_blob(store: TruthStore, digest: str, label: str) -> bytes:
    path = store.resolve_blob_path(f"blobs/{digest}")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise VerifyOrchestrationError(f"{label} is unavailable") from exc
    if sha256_bytes(payload) != digest:
        raise VerifyOrchestrationError(f"{label} failed integrity validation")
    return payload


def _record(
    store: TruthStore,
    record_type: type[Any],
    record_id: str,
) -> Any:
    value = verify_store.get_record(store, record_type, record_id)
    if value is None:
        raise VerifyOrchestrationError(
            f"{record_type.__name__} does not exist: {record_id}"
        )
    return value


def _selection_from_mapping(value: Mapping[str, Any]) -> AgentExecutionSelection:
    return AgentExecutionSelection(
        provider_id=_required_text(value.get("provider_id"), "provider_id"),
        model_id=_required_text(value.get("model_id"), "model_id"),
        provider_label=str(value.get("provider_label") or ""),
        model_label=str(value.get("model_label") or ""),
    )


def _default_selection_validator(
    selection: AgentExecutionSelection,
) -> AgentExecutionSelection:
    from work_buddy.agent_execution.registry import validate_selection

    return validate_selection(selection, refresh=False)


def _agent_actor(job: VerifyRuntimeJob) -> Actor:
    selection = job.selection
    return Actor(
        "agent_run",
        job.job_id,
        {
            "model": str(selection["model_id"]),
            "harness": str(selection["provider_id"]),
            "surface": f"cowork_verify_{job.role.value}",
            "session_id": job.session_id,
        },
    )


def _authorization_actor(job: VerifyRuntimeJob) -> Actor:
    authorizer = str(job.request.get("authorized_by_ref") or "dashboard-user")
    return Actor("human", authorizer)


def _result_view(result: EvaluationResult) -> dict[str, Any]:
    return {
        "evaluation_result_id": result.id,
        "result_kind": result.result_kind,
        "severity": result.severity,
        "message": result.message,
        "evidence_selector": (
            None
            if result.evidence_selector_json is None
            else json.loads(result.evidence_selector_json)
        ),
        "payload": json.loads(result.payload_json),
        "canonical_sha256": result.canonical_sha256,
        "created_at": result.created_at,
    }


def _coordinator_stage(job: VerifyRuntimeJob) -> str:
    stage = str(job.request.get("coordinator_stage") or "initial")
    if stage not in {"initial", "post_revision"}:
        raise VerifyOrchestrationError("coordinator job has an invalid stage")
    return stage


def _coordinator_decisions(job: VerifyRuntimeJob) -> frozenset[str]:
    return (
        _SECOND_COORDINATOR_DECISIONS
        if _coordinator_stage(job) == "post_revision"
        else _INITIAL_COORDINATOR_DECISIONS
    )


def _output_schema(job: VerifyRuntimeJob) -> dict[str, Any]:
    role = job.role
    if role is CoworkVerifyRole.REVISER:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["candidates"],
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "evaluation_result_id",
                            "replacement",
                            "rationale",
                            "tldr",
                        ],
                        "properties": {
                            "evaluation_result_id": {"type": "string"},
                            "replacement": {"type": "string"},
                            "rationale": {"type": "string"},
                            "tldr": {"type": "string"},
                        },
                    },
                }
            },
        }
    if role is CoworkVerifyRole.COORDINATOR:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["decisions", "summary"],
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "evaluation_result_id",
                            "decision",
                            "rationale",
                        ],
                        "properties": {
                            "evaluation_result_id": {"type": "string"},
                            "decision": {
                                "enum": sorted(_coordinator_decisions(job))
                            },
                            "rationale": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
            },
        }
    if role is CoworkVerifyRole.COTHINK:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["outcome", "rationale"],
            "properties": {
                "outcome": {"enum": ["perspective", "none"]},
                "content": {"type": "string"},
                "rationale": {"type": "string"},
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {"results": {"type": "array"}},
    }


def _criterion_and_check_context(
    store: TruthStore,
    run: EvaluationRun,
    results: Sequence[EvaluationResult],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    executions = verify_store.list_records(
        store,
        CheckExecution,
        where="source.evaluation_run_id = ?",
        params=(run.id,),
    )
    checks: list[dict[str, Any]] = []
    for execution in executions:
        check = _record(
            store,
            CheckDefinitionVersion,
            execution.check_definition_version_id,
        )
        checks.append(
            {
                "check_definition_version_id": check.id,
                "stable_key": check.stable_key,
                "version": check.version,
                "title": check.title,
                "mechanism": check.mechanism,
                "limitations": json.loads(check.limitations_json),
                "execution_status": execution.status,
                "diagnostics": json.loads(execution.diagnostics_json),
            }
        )
    criterion_ids = list(
        dict.fromkeys(result.criterion_definition_version_id for result in results)
    )
    for execution in executions:
        binding = _record(
            store,
            CriterionCheckBinding,
            execution.criterion_check_binding_id,
        )
        if binding.criterion_definition_version_id not in criterion_ids:
            criterion_ids.append(binding.criterion_definition_version_id)
    criteria: list[dict[str, Any]] = []
    for criterion_id in criterion_ids:
        criterion = _record(store, CriterionDefinitionVersion, criterion_id)
        criteria.append(
            {
                "criterion_definition_version_id": criterion.id,
                "stable_key": criterion.stable_key,
                "version": criterion.version,
                "title": criterion.title,
                "statement": criterion.description,
                "criterion_kind": criterion.criterion_kind,
                "origin": criterion.origin,
            }
        )
    return criteria, checks


def _affected_candidate_evaluations(
    store: TruthStore,
    job: VerifyRuntimeJob,
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    action = _record(store, ActionSnapshot, job.action_snapshot_id)
    try:
        projection = _read_blob(
            store,
            action.projection_blob_sha256,
            "frozen Markdown projection",
        ).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerifyOrchestrationError(
            "frozen action content is not UTF-8"
        ) from exc
    configuration = _mapping(
        job.request.get("effective_configuration"),
        "effective_configuration",
    )
    results = {
        result.id: result for result in _results_for_job(store, job)
    }
    affected_evaluations: list[dict[str, Any]] = []
    for candidate in candidates:
        item = _mapping(candidate, "reviser candidate")
        result_id = _required_text(
            item.get("evaluation_result_id"),
            "evaluation_result_id",
        )
        result = results.get(result_id)
        if result is None or result.evidence_selector_json is None:
            raise VerifyOrchestrationError(
                "candidate re-evaluation requires exact finding evidence"
            )
        try:
            affected_evaluations.append(
                evaluate_terminology_candidate(
                    projection=projection,
                    evaluation_result_id=result_id,
                    evidence_selector_json=result.evidence_selector_json,
                    replacement=_required_text(
                        item.get("replacement"),
                        "replacement",
                    ),
                    effective_configuration=configuration,
                )
            )
        except CandidateEvaluationError as exc:
            raise VerifyOrchestrationError(str(exc)) from exc
    return affected_evaluations


def _candidate_context(
    store: TruthStore,
    job: VerifyRuntimeJob,
) -> dict[str, Any]:
    if job.parent_job_id is None:
        return {
            "status": "not_requested",
            "candidates": [],
            "affected_evaluations": [],
        }
    parent = get_job(job.parent_job_id)
    if parent is None:
        raise VerifyOrchestrationError("coordinator parent job is unavailable")
    candidates: list[Mapping[str, Any]] = []
    affected_evaluations: list[dict[str, Any]] = []
    if parent.output is not None:
        raw = parent.output.get("candidates", [])
        if isinstance(raw, list):
            candidates = [
                item for item in raw if isinstance(item, Mapping)
            ]
        affected_evaluations = _affected_candidate_evaluations(
            store,
            job,
            candidates,
        )
        try:
            portable_evaluations = sanitize_candidate_evaluations(
                job.request.get("candidate_evaluations", [])
            )
        except CandidateEvaluationError as exc:
            raise VerifyOrchestrationError(str(exc)) from exc
        if portable_evaluations != affected_evaluations:
            raise VerifyOrchestrationError(
                "portable candidate evaluation proof does not match the "
                "authorized coordinator context"
            )
    return {
        # Operational status changes after the coordinator authorization receipt
        # is created. Keep the authorized context stable while still making the
        # availability of a typed candidate explicit.
        "status": "available" if parent.output is not None else "unavailable",
        "job_id": parent.job_id,
        "output_sha256": parent.output_sha256,
        "candidates": candidates,
        "affected_evaluations": affected_evaluations,
        "error_code": parent.error_code,
    }


def _build_job_context(
    store: TruthStore,
    job: VerifyRuntimeJob,
) -> dict[str, Any]:
    action = _record(store, ActionSnapshot, job.action_snapshot_id)
    projection_bytes = _read_blob(
        store,
        action.projection_blob_sha256,
        "frozen Markdown projection",
    )
    target_bytes = _read_blob(
        store,
        action.target_blob_sha256,
        "frozen action target",
    )
    try:
        projection = projection_bytes.decode("utf-8")
        target_text = target_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerifyOrchestrationError("frozen action content is not UTF-8") from exc
    document = documents.get_document(store, job.document_id)

    results: tuple[EvaluationResult, ...] = ()
    criteria: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    plan: EvaluationPlanSnapshot | None = None
    run: EvaluationRun | None = None
    if job.role is not CoworkVerifyRole.COTHINK:
        run = _record(store, EvaluationRun, job.evaluation_run_id)
        results = verify_store.list_records(
            store,
            EvaluationResult,
            where="source.evaluation_run_id = ?",
            params=(run.id,),
        )
        if job.role is CoworkVerifyRole.REVISER:
            requested_ids = set(_requested_revision_result_ids(job))
            results = tuple(
                result for result in results if result.id in requested_ids
            )
            if {result.id for result in results} != requested_ids:
                raise VerifyOrchestrationError(
                    "reviser context is not bound to its requested results"
                )
        criteria, checks = _criterion_and_check_context(store, run, results)
        if job.plan_snapshot_id is not None:
            plan = _record(
                store,
                EvaluationPlanSnapshot,
                job.plan_snapshot_id,
            )

    prior_dispositions = []
    for disposition_id in job.request.get("prior_disposition_ids", []):
        if not isinstance(disposition_id, str):
            raise VerifyOrchestrationError(
                "job prior_disposition_ids contains an invalid id"
            )
        disposition = _record(
            store,
            RoutingDisposition,
            disposition_id,
        )
        prior_dispositions.append(
            {
                "id": disposition.id,
                "evaluation_result_id": disposition.evaluation_result_id,
                "decision": disposition.decision,
                "rationale": disposition.rationale,
                "policy_snapshot_sha256": (
                    disposition.policy_snapshot_sha256
                ),
                "created_at": disposition.created_at,
            }
        )
    prior_human_review_outcomes = _prior_human_review_outcomes(store, job)
    effective_configuration: dict[str, Any] | None = None
    if job.role is not CoworkVerifyRole.COTHINK:
        effective_configuration = _mapping(
            job.request.get("effective_configuration"),
            "effective_configuration",
        )
        expected_configuration_sha256 = _required_text(
            job.request.get("effective_configuration_sha256"),
            "effective_configuration_sha256",
        )
        if (
            sha256_text(canonical_json(effective_configuration))
            != expected_configuration_sha256
        ):
            raise VerifyOrchestrationError(
                "effective verification configuration failed integrity validation"
            )

    return {
        "schema": "work-buddy.cowork-verify-job/v1",
        "binding": {
            "store_id": job.store_id,
            "document_id": job.document_id,
            "run_id": job.evaluation_run_id,
            "job_id": job.job_id,
            "role": job.role.value,
            "action_snapshot_id": action.id,
            "plan_snapshot_id": None if plan is None else plan.id,
        },
        "document": {
            "title": document.title or "",
            "document_class": document.document_class,
            "frozen_markdown": projection,
            "projection_sha256": action.projection_sha256,
            "structured_head_sha256": action.structured_head_sha256,
            "document_version_id": action.document_version_id,
        },
        "target": {
            "kind": action.target_kind,
            "selector": json.loads(action.target_selector_json),
            "text": target_text,
            "text_sha256": action.target_text_sha256,
            "context_boundary": json.loads(action.context_boundary_json),
            "allowed_change_ranges": json.loads(
                action.allowed_change_ranges_json
            ),
            "egress_boundary": json.loads(action.egress_boundary_json),
        },
        "user_goal": str(job.request.get("user_goal") or ""),
        "protected_intent": str(job.request.get("protected_intent") or ""),
        "recheck_of_proposal_ids": list(
            job.request.get("recheck_of_proposal_ids", [])
        ),
        "criteria": criteria,
        "checks": checks,
        "normalized_results": [_result_view(result) for result in results],
        "prior_dispositions": prior_dispositions,
        "prior_human_review_outcomes": prior_human_review_outcomes,
        "candidate_revision": (
            _candidate_context(store, job)
            if job.role is CoworkVerifyRole.COORDINATOR
            else {
                "status": "not_applicable",
                "candidates": [],
                "affected_evaluations": [],
            }
        ),
        "policy": {
            "raw_specialist_output_may_surface": False,
            "coordinator_required_before_review": True,
            "coordinator_may_apply_changes": False,
            "human_decision_required_for_proposals": True,
            "co_think_is_non_evidential": True,
            "coordinator_stage": (
                _coordinator_stage(job)
                if job.role is CoworkVerifyRole.COORDINATOR
                else None
            ),
            "allowed_decisions": (
                sorted(_coordinator_decisions(job))
                if job.role is CoworkVerifyRole.COORDINATOR
                else []
            ),
            "decision_rules": {
                "conforming": ["retain"],
                "finding": (
                    sorted(_coordinator_decisions(job))
                    if job.role is CoworkVerifyRole.COORDINATOR
                    else []
                ),
                "request_revision_only_for_findings": True,
                "route_to_correction_only_after_revision": True,
            },
            "requested_revision_result_ids": list(
                job.request.get("requested_revision_result_ids", [])
            ),
            "effective_configuration_sha256": job.request.get(
                "effective_configuration_sha256"
            ),
            "effective_configuration": effective_configuration,
            "effective_policy_sha256": job.request.get(
                "effective_policy_sha256"
            ),
            "active_criterion_ids": list(
                job.request.get("active_criterion_ids", [])
            ),
        },
        "output_schema": _output_schema(job),
    }


def _prior_disposition_ids(
    store: TruthStore,
    *,
    document_id: str,
    before: str,
) -> list[str]:
    with store._read_connection() as conn:
        rows = conn.execute(
            """
            SELECT disposition.id
            FROM routing_dispositions AS disposition
            JOIN evaluation_results AS result
              ON result.id = disposition.evaluation_result_id
            JOIN evaluation_runs AS run
              ON run.id = result.evaluation_run_id
            JOIN action_snapshots AS snapshot
              ON snapshot.id = run.action_snapshot_id
            JOIN ledger_records AS ledger
              ON ledger.record_type = 'routing_disposition'
             AND ledger.record_key = disposition.id
            WHERE snapshot.document_id = ?
              AND disposition.created_at < ?
            ORDER BY ledger.seq DESC
            LIMIT 50
            """,
            (document_id, before),
        ).fetchall()
    return [str(row["id"]) for row in reversed(rows)]


def _prior_human_review_outcome_ids(
    store: TruthStore,
    *,
    document_id: str,
    before: str,
) -> list[str]:
    """Select bounded, portable human proposal decisions visible to a coordinator."""

    with store._read_connection() as conn:
        rows = conn.execute(
            """
            SELECT event.id
            FROM proposal_status_events AS event
            JOIN proposals AS proposal
              ON proposal.id = event.proposal_id
            WHERE proposal.document_id = ?
              AND event.actor_kind = 'human'
              AND event.at < ?
            ORDER BY event.seq DESC
            LIMIT 50
            """,
            (document_id, before),
        ).fetchall()
    return [str(row["id"]) for row in reversed(rows)]


def _prior_human_review_outcomes(
    store: TruthStore,
    job: VerifyRuntimeJob,
) -> list[dict[str, Any]]:
    raw = job.request.get("prior_human_review_outcome_ids", [])
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item for item in raw
    ):
        raise VerifyOrchestrationError(
            "job prior_human_review_outcome_ids is invalid"
        )
    if len(raw) != len(set(raw)):
        raise VerifyOrchestrationError(
            "job prior_human_review_outcome_ids contains duplicates"
        )
    outcomes: list[dict[str, Any]] = []
    with store._read_connection() as conn:
        for event_id in raw:
            row = conn.execute(
                """
                SELECT event.*, proposal.canonical_sha256
                FROM proposal_status_events AS event
                JOIN proposals AS proposal
                  ON proposal.id = event.proposal_id
                WHERE event.id = ?
                  AND proposal.document_id = ?
                  AND event.actor_kind = 'human'
                """,
                (event_id, job.document_id),
            ).fetchone()
            if row is None:
                raise VerifyOrchestrationError(
                    "a prior human review outcome is unavailable"
                )
            outcomes.append(
                {
                    "id": str(row["id"]),
                    "proposal_id": str(row["proposal_id"]),
                    "proposal_canonical_sha256": str(
                        row["canonical_sha256"]
                    ),
                    "status": str(row["status"]),
                    "decision": (
                        None
                        if row["decision"] is None
                        else str(row["decision"])
                    ),
                    "at": str(row["at"]),
                    "actor_ref": (
                        None
                        if row["actor_ref"] is None
                        else str(row["actor_ref"])
                    ),
                    "basis_kind": str(row["basis_kind"]),
                    "basis_ref": (
                        None
                        if row["basis_ref"] is None
                        else str(row["basis_ref"])
                    ),
                    "note": (
                        None if row["note"] is None else str(row["note"])
                    ),
                }
            )
    return outcomes


def _validated_recheck_proposal_ids(
    store: TruthStore,
    *,
    document_id: str,
    proposal_ids: Sequence[str],
) -> list[str]:
    if not isinstance(proposal_ids, Sequence) or isinstance(
        proposal_ids,
        (str, bytes, bytearray),
    ):
        raise VerifyOrchestrationError(
            "recheck_of_proposal_ids must be a sequence of proposal ids"
        )
    validated: list[str] = []
    seen: set[str] = set()
    for value in proposal_ids:
        proposal_id = _required_text(value, "recheck proposal id")
        if proposal_id in seen:
            raise VerifyOrchestrationError(
                "recheck_of_proposal_ids contains a duplicate proposal"
            )
        try:
            proposal = proposals.get_proposal(store, proposal_id)
        except InvariantViolation as exc:
            raise VerifyOrchestrationError(str(exc)) from exc
        if proposal.document_id != document_id:
            raise VerifyOrchestrationError(
                "a recheck proposal belongs to another document"
            )
        seen.add(proposal_id)
        validated.append(proposal_id)
    return validated


def _validate_recheck_origin(
    store: TruthStore,
    *,
    document_id: str,
    proposal_ids: Sequence[str],
    source_run_id: str | None,
    selection: AgentExecutionSelection,
) -> str | None:
    if not proposal_ids:
        if source_run_id is not None:
            raise VerifyOrchestrationError(
                "recheck_of_run_id requires recheck_of_proposal_ids"
            )
        return None
    if source_run_id is None:
        return None
    source_run = _record(
        store,
        EvaluationRun,
        _required_text(source_run_id, "recheck_of_run_id"),
    )
    source_action = _record(
        store,
        ActionSnapshot,
        source_run.action_snapshot_id,
    )
    if source_action.document_id != document_id:
        raise VerifyOrchestrationError(
            "the source Verify run belongs to another document"
        )
    result_run_by_id = {
        result.id: result.evaluation_run_id
        for result in verify_store.list_records(store, EvaluationResult)
    }
    addressed_by_proposal: dict[str, set[str]] = {
        proposal_id: set() for proposal_id in proposal_ids
    }
    for relation in verify_store.list_records(store, ResultRelation):
        if (
            relation.relation_kind == "addresses"
            and relation.target_kind == "proposal"
            and relation.target_ref in addressed_by_proposal
        ):
            run_id = result_run_by_id.get(relation.evaluation_result_id)
            if run_id is not None:
                addressed_by_proposal[relation.target_ref].add(run_id)
    if any(
        source_run.id not in run_ids
        for run_ids in addressed_by_proposal.values()
    ):
        raise VerifyOrchestrationError(
            "a recheck proposal is not linked to the source Verify run"
        )
    source_coordination = root_coordination_for_run(store, source_run.id)
    if source_coordination is None:
        raise VerifyOrchestrationError(
            "the source Verify run has no execution authorization"
        )
    source_selection = source_coordination["selection"]
    if (
        source_selection.get("provider_id") != selection.provider_id
        or source_selection.get("model_id") != selection.model_id
    ):
        raise VerifyOrchestrationError(
            "automatic recheck must use the source Verify run's provider and model"
        )
    return source_run.id


def _create_job(
    store: TruthStore,
    *,
    document_id: str,
    run_id: str,
    action_snapshot_id: str,
    plan_snapshot_id: str | None,
    role: CoworkVerifyRole,
    selection: AgentExecutionSelection,
    request_payload: Mapping[str, Any],
    parent_job_id: str | None = None,
) -> VerifyRuntimeJob:
    job_id = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.cowork-verify-job/v1",
                "store_id": store.store_id,
                "evaluation_run_id": run_id,
                "role": role.value,
                "parent_job_id": parent_job_id,
            }
        )
    )[:32]
    session_id = cowork_verify_job_session_id(job_id, role)
    provisional_request = dict(request_payload)
    # Build the exact context against a temporary in-memory binding first.
    provisional = VerifyRuntimeJob(
        job_id=job_id,
        store_id=store.store_id,
        document_id=document_id,
        evaluation_run_id=run_id,
        action_snapshot_id=action_snapshot_id,
        plan_snapshot_id=plan_snapshot_id,
        role=role,
        status="prepared",
        selection=selection.to_dict(),
        authorization_receipt_id="pending",
        context_sha256="pending",
        request=provisional_request,
        parent_job_id=parent_job_id,
        session_id=session_id,
        pid=None,
        output_sha256=None,
        output=None,
        error_code="",
        error="",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    context = _build_job_context(store, provisional)
    context_sha256 = sha256_text(canonical_json(context))
    created_at = utc_now()
    expires_at = (
        datetime.now(timezone.utc) + _AUTHORIZATION_TTL
    ).isoformat(timespec="milliseconds")
    receipt = record_model_call_authorization(
        store,
        action_snapshot_id=action_snapshot_id,
        plan_snapshot_id=plan_snapshot_id,
        provider=selection.provider_id,
        model=selection.model_id,
        context_sha256=context_sha256,
        content_boundary={
            "role": role.value,
            "job_id": job_id,
            "document": "complete_permitted_frozen_projection",
            "action_snapshot_id": action_snapshot_id,
            "authority_context": {
                "user_goal": str(request_payload.get("user_goal") or ""),
                "protected_intent": str(
                    request_payload.get("protected_intent") or ""
                ),
                "effective_configuration": request_payload.get(
                    "effective_configuration"
                ),
                "effective_configuration_sha256": request_payload.get(
                    "effective_configuration_sha256"
                ),
                "effective_policy_sha256": request_payload.get(
                    "effective_policy_sha256"
                ),
                "active_criterion_ids": list(
                    request_payload.get("active_criterion_ids", [])
                ),
                "prior_disposition_ids": list(
                    request_payload.get("prior_disposition_ids", [])
                ),
                "prior_human_review_outcome_ids": list(
                    request_payload.get(
                        "prior_human_review_outcome_ids",
                        [],
                    )
                ),
                "recheck_of_run_id": request_payload.get(
                    "recheck_of_run_id"
                ),
                "recheck_of_proposal_ids": list(
                    request_payload.get("recheck_of_proposal_ids", [])
                ),
                "recheck_intent_id": request_payload.get(
                    "recheck_intent_id"
                ),
                "coordinator_stage": request_payload.get(
                    "coordinator_stage"
                ),
                "requested_revision_result_ids": list(
                    request_payload.get(
                        "requested_revision_result_ids",
                        [],
                    )
                ),
            },
        },
        egress_class="account_backed_agent",
        cost_ceiling_usd=MAX_VERIFY_JOB_BUDGET_USD,
        retry_limit=0,
        expires_at=expires_at,
        actor=Actor(
            "human",
            str(request_payload.get("authorized_by_ref") or "dashboard-user"),
        ),
        at=created_at,
    )
    runtime_job = create_job(
        job_id=job_id,
        store_id=store.store_id,
        document_id=document_id,
        evaluation_run_id=run_id,
        action_snapshot_id=action_snapshot_id,
        plan_snapshot_id=plan_snapshot_id,
        role=role,
        selection=selection.to_dict(),
        authorization_receipt_id=receipt.id,
        context_sha256=context_sha256,
        request=provisional_request,
        parent_job_id=parent_job_id,
        session_id=session_id,
        at=created_at,
    )
    record_coordination_status(store, runtime_job)
    return runtime_job


def _launch_job(
    job: VerifyRuntimeJob,
    *,
    store: TruthStore | None = None,
    spawn_detached: SpawnDetached | None = None,
) -> tuple[VerifyRuntimeJob, VerifyJobSpawnMetadata | None]:
    resolved_store = (
        TruthStoreRegistry().open_store(job.store_id)
        if store is None
        else store
    )
    if spawn_detached is None:
        # Production launches ride the sidecar's disk-backed operation queue.
        # The internal handler is intentionally absent from the MCP capability
        # registry, and the runtime job ID is its replay fence.  A crash after
        # job creation but before this enqueue is healed by the sidecar's
        # prepared-job reconciliation pass.
        from work_buddy.cowork.verify_dispatch import enqueue_verify_launch

        enqueue_verify_launch(job, store=resolved_store)
        return get_job(job.job_id) or job, None

    # Tests inject the provider-neutral spawn seam and keep deterministic
    # synchronous assertions. Production never reaches this direct path.
    launching, claimed = claim_job_launch(job.job_id)
    if not claimed:
        record_coordination_status(resolved_store, launching)
        return launching, None
    record_coordination_status(resolved_store, launching)
    metadata = spawn_verify_job(
        store_id=launching.store_id,
        document_id=launching.document_id,
        run_id=launching.evaluation_run_id,
        job_id=launching.job_id,
        role=launching.role,
        selection=_selection_from_mapping(launching.selection),
        spawn_detached=spawn_detached,
    )
    if metadata.ok:
        current = get_job(job.job_id)
        if current is None:
            raise VerifyOrchestrationError(
                "Verify job disappeared while its worker was launching"
            )
        # A detached worker can submit before the provider returns its spawn
        # receipt. Never regress submitted/completed state back to running.
        status = (
            "running"
            if current.status in {"prepared", "launching"}
            else current.status
        )
        persisted = update_job(
            launching.job_id,
            status=status,
            pid=metadata.pid,
        )
        record_coordination_status(resolved_store, persisted)
        return persisted, metadata
    persisted = update_job(
        launching.job_id,
        status="unavailable",
        error_code=metadata.error_code or "coordination_unavailable",
        error=metadata.error or "The selected account-backed agent is unavailable.",
    )
    record_coordination_status(resolved_store, persisted)
    return persisted, metadata


def _validate_capture(
    store: TruthStore,
    *,
    document_id: str,
    capture: Mapping[str, Any],
    actor: Actor,
    selection: AgentExecutionSelection,
) -> ActionSnapshot:
    if capture.get("schema") != "wb.cowork.action-snapshot/v1":
        raise VerifyOrchestrationError("unsupported action snapshot schema")
    if capture.get("storeId") != store.store_id:
        raise VerifyOrchestrationError("action snapshot belongs to another store")
    if capture.get("documentId") != document_id:
        raise VerifyOrchestrationError("action snapshot belongs to another document")
    snapshot = _decode_base64(capture.get("snapshotBase64"), "snapshotBase64")
    state_vector = _decode_base64(
        capture.get("stateVectorBase64"),
        "stateVectorBase64",
    )
    snapshot_sha256 = _required_text(
        capture.get("snapshotSha256"),
        "snapshotSha256",
    )
    if sha256_bytes(snapshot) != snapshot_sha256:
        raise VerifyOrchestrationError(
            "snapshotBase64 does not match snapshotSha256"
        )
    state_vector_sha256 = _required_text(
        capture.get("stateVectorSha256"),
        "stateVectorSha256",
    )
    if sha256_bytes(state_vector) != state_vector_sha256:
        raise VerifyOrchestrationError(
            "stateVectorBase64 does not match stateVectorSha256"
        )
    projection = _required_text(
        capture.get("projectionMarkdown"),
        "projectionMarkdown",
    )
    target = _mapping(capture.get("target"), "target")
    selector = _mapping(target.get("selector"), "target.selector")
    target_source = _required_text(
        target.get("source"),
        "target.source",
    )
    if target_source not in _ACTION_TARGET_SOURCES:
        raise VerifyOrchestrationError(
            "target.source must name an admitted Co-work target source"
        )
    target_label = _required_text(target.get("label"), "target.label")
    if (
        target_source
        in {"current_selection", "current_section", "custom_range"}
        and selector.get("kind") == "document"
    ):
        raise VerifyOrchestrationError(
            "a scoped Co-work target cannot silently widen to the whole document"
        )
    target_reference, target_reference_sha256 = _target_reference(
        target.get("targetReference"),
        store_id=store.store_id,
        document_id=document_id,
    )
    if selector.get("kind") == "document" and target_reference is not None:
        raise VerifyOrchestrationError(
            "a whole-document capture cannot carry a text target reference"
        )
    expected_target_sha256 = _required_text(
        target.get("targetTextSha256"),
        "target.targetTextSha256",
    )
    client_captured_at = capture.get("capturedAt")
    if client_captured_at is not None and not isinstance(
        client_captured_at,
        str,
    ):
        raise VerifyOrchestrationError("capturedAt telemetry must be text")
    server_captured_at = utc_now()
    try:
        action = create_action_snapshot(
            store,
            document_id=document_id,
            projection=projection,
            expected_snapshot_sha256=snapshot_sha256,
            expected_structured_head_sha256=_required_text(
                capture.get("structuredHeadSha256"),
                "structuredHeadSha256",
            ),
            expected_ydoc_generation_sha256=_required_text(
                capture.get("ydocGenerationSha256"),
                "ydocGenerationSha256",
            ),
            expected_projection_sha256=_required_text(
                capture.get("projectionSha256"),
                "projectionSha256",
            ),
            target=selector,
            context_boundary={
                "kind": "complete_frozen_document",
                "purpose": "whole-context coordinator and bounded reviser",
                "capture_id": _required_text(
                    capture.get("captureId"),
                    "captureId",
                ),
                "target_source": target_source,
                "target_label": target_label,
                "target_reference": target_reference,
                "target_reference_sha256": target_reference_sha256,
                # Browser time is useful diagnostics but never temporal
                # authority for prior-decision or recheck ordering.
                "client_captured_at": client_captured_at,
            },
            egress_boundary={
                "class": "account_backed_agent",
                "provider_id": selection.provider_id,
                "model_id": selection.model_id,
                "content": "complete_permitted_frozen_document",
            },
            actor=actor,
            at=server_captured_at,
        )
    except VerifyOrchestrationError:
        raise
    except VerifyInvariantViolation as exc:
        raise VerifyOrchestrationError(str(exc)) from exc
    if action.target_text_sha256 != expected_target_sha256:
        raise VerifyOrchestrationError(
            "canonical target text does not match targetTextSha256"
        )
    return action


def _resolve_execution_configuration(
    store: TruthStore,
    *,
    document_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    configuration = list_effective_verification_configuration(
        store,
        document_id=document_id,
        ensure_system_defaults=True,
    )
    criteria = configuration.get("criteria")
    if not isinstance(criteria, list):
        raise VerifyOrchestrationError(
            "effective Verify configuration has an invalid criteria projection"
        )
    blocked = [
        item
        for item in criteria
        if isinstance(item, Mapping)
        and item.get("operational_state") == "blocked_required_check"
    ]
    if blocked:
        titles = ", ".join(
            str(item.get("title") or item.get("stable_key") or "criterion")
            for item in blocked
        )
        raise VerifyOrchestrationError(
            f"required verification criteria are blocked or unavailable: {titles}"
        )
    active = [
        dict(item)
        for item in criteria
        if isinstance(item, Mapping) and item.get("operational_state") == "active"
    ]
    if not active:
        raise VerifyOrchestrationError(
            "no active available verification criteria apply to this document"
        )

    supported = [
        item
        for item in active
        if item.get("stable_key") == "terminology_exact_match"
    ]
    if len(supported) != 1 or len(active) != 1:
        raise VerifyOrchestrationError(
            "the active verification configuration includes a criterion "
            "without an admitted orchestration implementation"
        )
    criterion = supported[0]
    selected_checks = [
        item
        for item in criterion.get("checks", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("binding"), Mapping)
        and item["binding"].get("selected") is True
        and isinstance(item.get("availability"), Mapping)
        and item["availability"].get("state") == "available"
    ]
    seeded = seed_terminology_exact_match(store)
    if (
        len(selected_checks) != 1
        or selected_checks[0].get("id") != seeded.check.id
        or selected_checks[0]["binding"].get("id") != seeded.binding.id
    ):
        raise VerifyOrchestrationError(
            "the active terminology criterion selects an unsupported binding"
        )
    return configuration, active


def _effective_policy_sha256(
    *,
    effective_configuration_sha256: str,
    active_criterion_ids: Sequence[str],
) -> str:
    """Bind coordinator dispositions to the exact effective decision policy."""

    return sha256_text(
        canonical_json(
            {
                "schema": "work-buddy.cowork-verify-routing-policy/v1",
                "effective_configuration_sha256": effective_configuration_sha256,
                "active_criterion_ids": list(active_criterion_ids),
                "result_rules": {
                    "conforming": {
                        "initial": ["retain"],
                        "post_revision": [],
                    },
                    "finding": {
                        "initial": sorted(_INITIAL_COORDINATOR_DECISIONS),
                        "post_revision": sorted(_SECOND_COORDINATOR_DECISIONS),
                    },
                },
                "retain_persists_as": "suppress",
                "proposal_authority": "post_revision_coordinator_only",
                "human_decision_required_for_proposals": True,
            }
        )
    )


def start_verify_run(
    store: TruthStore,
    *,
    document_id: str,
    capture: Mapping[str, Any],
    selection: AgentExecutionSelection,
    actor: Actor,
    user_goal: str,
    protected_intent: str,
    recheck_of_proposal_ids: Sequence[str] = (),
    recheck_of_run_id: str | None = None,
    recheck_intent_id: str | None = None,
    validate_selection: SelectionValidator = _default_selection_validator,
    spawn_detached: SpawnDetached | None = None,
) -> dict[str, Any]:
    """Capture, check, and launch the first required forest-level worker."""

    if actor.kind != "human":
        raise VerifyOrchestrationError("Verify runs require a human authorizer")
    recheck_proposal_ids = _validated_recheck_proposal_ids(
        store,
        document_id=document_id,
        proposal_ids=recheck_of_proposal_ids,
    )
    if recheck_proposal_ids and recheck_intent_id is None:
        raise VerifyOrchestrationError(
            "proposal-bound rechecks require the exact committed-sitting "
            "recheck_intent_id"
        )
    if recheck_intent_id is not None and not recheck_proposal_ids:
        raise VerifyOrchestrationError(
            "recheck_intent_id requires pending proposal bindings"
        )
    effective_configuration, active_criteria = _resolve_execution_configuration(
        store,
        document_id=document_id,
    )
    validated_selection = validate_selection(selection)
    validated_recheck_run_id = _validate_recheck_origin(
        store,
        document_id=document_id,
        proposal_ids=recheck_proposal_ids,
        source_run_id=recheck_of_run_id,
        selection=validated_selection,
    )
    action = _validate_capture(
        store,
        document_id=document_id,
        capture=capture,
        actor=actor,
        selection=validated_selection,
    )
    if recheck_intent_id is not None:
        validate_recheck_intent(
            store,
            document_id=document_id,
            intent_id=_required_text(
                recheck_intent_id,
                "recheck_intent_id",
            ),
            source_run_id=validated_recheck_run_id or "",
            proposal_ids=recheck_proposal_ids,
            action_snapshot=action,
        )
    activation_id = active_criteria[0].get("effective_activation", {}).get("id")
    if not isinstance(activation_id, str) or not activation_id:
        raise VerifyOrchestrationError(
            "the active terminology criterion has no exact activation"
        )
    evaluation = run_terminology_exact_match(
        store,
        action_snapshot_id=action.id,
        criterion_activation_id=activation_id,
        actor=Actor("system", "cowork-verify"),
    )
    prior_result_ids = {
        relation.evaluation_result_id
        for relation in verify_store.list_records(store, ResultRelation)
        if relation.relation_kind == "addresses"
        and relation.target_kind == "proposal"
        and relation.target_ref in recheck_proposal_ids
    }
    for result in evaluation.results:
        for proposal_id in recheck_proposal_ids:
            record_result_relation(
                store,
                evaluation_result_id=result.id,
                relation_kind="rechecks",
                target_kind="proposal",
                target_ref=proposal_id,
                actor=Actor("system", "cowork-verify"),
            )
        for prior_result_id in sorted(prior_result_ids):
            record_result_relation(
                store,
                evaluation_result_id=result.id,
                relation_kind="rechecks",
                target_kind="evaluation_result",
                target_ref=prior_result_id,
                actor=Actor("system", "cowork-verify"),
            )
    effective_configuration_sha256 = sha256_text(
        canonical_json(effective_configuration)
    )
    active_criterion_ids = [str(item["id"]) for item in active_criteria]
    request_payload = {
        "authorized_by_ref": actor.ref,
        "user_goal": _required_text(user_goal, "user_goal"),
        "protected_intent": _required_text(
            protected_intent,
            "protected_intent",
        ),
        "prior_disposition_ids": _prior_disposition_ids(
            store,
            document_id=document_id,
            before=action.created_at,
        ),
        "prior_human_review_outcome_ids": (
            _prior_human_review_outcome_ids(
                store,
                document_id=document_id,
                before=action.created_at,
            )
        ),
        "recheck_of_proposal_ids": recheck_proposal_ids,
        "recheck_of_run_id": validated_recheck_run_id,
        "recheck_intent_id": recheck_intent_id,
        "effective_configuration_sha256": effective_configuration_sha256,
        "effective_configuration": effective_configuration,
        "effective_policy_sha256": _effective_policy_sha256(
            effective_configuration_sha256=effective_configuration_sha256,
            active_criterion_ids=active_criterion_ids,
        ),
        "active_criterion_ids": active_criterion_ids,
        "coordinator_stage": "initial",
        "requested_revision_result_ids": [],
    }
    existing_jobs = jobs_for_run(store.store_id, evaluation.run.id)
    if existing_jobs:
        for candidate in existing_jobs:
            if candidate.status == "completed":
                _completed_submission_response(store, candidate)
            else:
                record_coordination_status(store, candidate)
        existing = next(
            (
                candidate
                for candidate in existing_jobs
                if candidate.role is CoworkVerifyRole.COORDINATOR
                and candidate.parent_job_id is None
            ),
            None,
        )
        if existing is None:
            raise VerifyOrchestrationError(
                "Verify run is missing its initial coordinator binding"
            )
        comparable_keys = {
            "authorized_by_ref",
            "user_goal",
            "protected_intent",
            "prior_disposition_ids",
            "prior_human_review_outcome_ids",
            "recheck_of_proposal_ids",
            "recheck_of_run_id",
            "recheck_intent_id",
            "effective_configuration",
            "effective_configuration_sha256",
            "effective_policy_sha256",
            "active_criterion_ids",
            "coordinator_stage",
            "requested_revision_result_ids",
        }
        if (
            any(
                existing.request.get(key) != request_payload.get(key)
                for key in comparable_keys
            )
            or existing.selection.get("provider_id")
            != validated_selection.provider_id
            or existing.selection.get("model_id") != validated_selection.model_id
        ):
            raise VerifyOrchestrationError(
                "captureId was already used with different Verify inputs"
            )
        coordinator = next(
            (
                job
                for job in reversed(existing_jobs)
                if job.role is CoworkVerifyRole.COORDINATOR
            ),
            None,
        )
        active_job = next(
            (
                job
                for job in reversed(existing_jobs)
                if job.status
                in {"prepared", "launching", "running", "submitted"}
            ),
            None,
        )
        visible_job = active_job or coordinator or existing_jobs[-1]
        unavailable = visible_job.status in {"unavailable", "failed"}
        completed = (
            visible_job.status == "completed"
            and not any(
                job.status
                in {"prepared", "launching", "running", "submitted"}
                for job in existing_jobs
            )
        )
        replay_stage = (
            "complete"
            if completed
            else "coordination_unavailable"
            if unavailable
            else "drafting_correction"
            if visible_job.role is CoworkVerifyRole.REVISER
            else "reconciling"
        )
        return {
            "ok": True,
            "contract_version": VERIFY_CONTRACT_VERSION,
            "action_snapshot_id": action.id,
            "run_id": evaluation.run.id,
            "job_id": visible_job.job_id,
            "stage": replay_stage,
            "result_count": len(evaluation.results),
            "coordination_status": (
                "completed" if completed else "unavailable" if unavailable else "pending"
            ),
            "selection": validated_selection.to_dict(),
            "replayed": True,
        }
    job = _create_job(
        store,
        document_id=document_id,
        run_id=evaluation.run.id,
        action_snapshot_id=action.id,
        plan_snapshot_id=evaluation.plan.id,
        role=CoworkVerifyRole.COORDINATOR,
        selection=validated_selection,
        request_payload=request_payload,
    )
    launched, _ = _launch_job(
        job,
        store=store,
        spawn_detached=spawn_detached,
    )
    return {
        "ok": True,
        "contract_version": VERIFY_CONTRACT_VERSION,
        "action_snapshot_id": action.id,
        "run_id": evaluation.run.id,
        "job_id": launched.job_id,
        "stage": "reconciling",
        "result_count": len(evaluation.results),
        "coordination_status": (
            "pending"
            if launched.status in {"prepared", "launching", "running"}
            else "unavailable"
        ),
        "selection": validated_selection.to_dict(),
    }


def start_cothink(
    store: TruthStore,
    *,
    document_id: str,
    capture: Mapping[str, Any],
    selection: AgentExecutionSelection,
    actor: Actor,
    purpose: str,
    protected_intent: str,
    validate_selection: SelectionValidator = _default_selection_validator,
    spawn_detached: SpawnDetached | None = None,
) -> dict[str, Any]:
    """Explicitly invite at most one non-evidential alternative perspective."""

    if actor.kind != "human":
        raise VerifyOrchestrationError("Co-think requires a human authorizer")
    validated_selection = validate_selection(selection)
    action = _validate_capture(
        store,
        document_id=document_id,
        capture=capture,
        actor=actor,
        selection=validated_selection,
    )
    request_payload = {
        "authorized_by_ref": actor.ref,
        "user_goal": _required_text(purpose, "purpose"),
        "protected_intent": _required_text(
            protected_intent,
            "protected_intent",
        ),
        "prior_disposition_ids": [],
    }
    existing_jobs = jobs_for_run(store.store_id, action.id)
    if existing_jobs:
        existing = existing_jobs[-1]
        if existing.status == "completed":
            _completed_submission_response(store, existing)
        else:
            record_coordination_status(store, existing)
        if (
            existing.request.get("authorized_by_ref")
            != request_payload["authorized_by_ref"]
            or existing.request.get("user_goal") != request_payload["user_goal"]
            or existing.request.get("protected_intent")
            != request_payload["protected_intent"]
            or existing.selection.get("provider_id")
            != validated_selection.provider_id
            or existing.selection.get("model_id") != validated_selection.model_id
        ):
            raise VerifyOrchestrationError(
                "captureId was already used with different Co-think inputs"
            )
        return {
            "ok": True,
            "contract_version": VERIFY_CONTRACT_VERSION,
            "action_snapshot_id": action.id,
            "job_id": existing.job_id,
            "status": existing.status,
            "selection": validated_selection.to_dict(),
            "replayed": True,
        }
    job = _create_job(
        store,
        document_id=document_id,
        run_id=action.id,
        action_snapshot_id=action.id,
        plan_snapshot_id=None,
        role=CoworkVerifyRole.COTHINK,
        selection=validated_selection,
        request_payload=request_payload,
    )
    launched, _ = _launch_job(
        job,
        store=store,
        spawn_detached=spawn_detached,
    )
    return {
        "ok": True,
        "contract_version": VERIFY_CONTRACT_VERSION,
        "action_snapshot_id": action.id,
        "job_id": launched.job_id,
        "status": launched.status,
        "selection": validated_selection.to_dict(),
    }


def _bound_job(
    job_id: str,
    agent_session_id: str | None,
) -> VerifyRuntimeJob:
    identity = cowork_verify_job_from_session(agent_session_id)
    if identity is None or identity.job_id != job_id:
        raise VerifyOrchestrationError("Verify job session is not bound to this job")
    job = get_job(job_id)
    if job is None:
        raise VerifyOrchestrationError("Verify job does not exist")
    if identity.role is not job.role or agent_session_id != job.session_id:
        raise VerifyOrchestrationError("Verify job role binding does not match")
    return job


def get_worker_job(
    *,
    job_id: str,
    agent_session_id: str | None,
) -> dict[str, Any]:
    job = _bound_job(job_id, agent_session_id)
    if job.status not in {"prepared", "launching", "running", "submitted"}:
        raise VerifyOrchestrationError(
            f"Verify job is not available in state {job.status}"
        )
    store = TruthStoreRegistry().open_store(job.store_id)
    receipt = _record(
        store,
        ModelCallAuthorizationReceipt,
        job.authorization_receipt_id,
    )
    if datetime.fromisoformat(
        receipt.expires_at.replace("Z", "+00:00")
    ) <= datetime.now(timezone.utc):
        expired_job = update_job(
            job.job_id,
            status="unavailable",
            error_code="authorization_expired",
            error="The exact model-call authorization expired.",
        )
        record_coordination_status(store, expired_job)
        raise VerifyOrchestrationError("Verify job authorization expired")
    context = _build_job_context(store, job)
    if sha256_text(canonical_json(context)) != job.context_sha256:
        failed_job = update_job(
            job.job_id,
            status="failed",
            error_code="context_integrity_failed",
            error="The immutable Verify job context no longer matches its receipt.",
        )
        record_coordination_status(store, failed_job)
        raise VerifyOrchestrationError("Verify job context failed integrity validation")
    return {
        "ok": True,
        "job_id": job.job_id,
        "role": job.role.value,
        "context_sha256": job.context_sha256,
        "authorization_receipt_id": receipt.id,
        "context": context,
    }


def _results_for_job(
    store: TruthStore,
    job: VerifyRuntimeJob,
) -> tuple[EvaluationResult, ...]:
    return verify_store.list_records(
        store,
        EvaluationResult,
        where="source.evaluation_run_id = ?",
        params=(job.evaluation_run_id,),
    )


def _requested_revision_result_ids(job: VerifyRuntimeJob) -> tuple[str, ...]:
    raw = job.request.get("requested_revision_result_ids", [])
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item for item in raw
    ):
        raise VerifyOrchestrationError(
            "job requested_revision_result_ids is invalid"
        )
    if len(set(raw)) != len(raw):
        raise VerifyOrchestrationError(
            "job requested_revision_result_ids contains duplicates"
        )
    return tuple(raw)


def _validate_reviser_output(
    store: TruthStore,
    job: VerifyRuntimeJob,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if set(payload) != {"candidates"}:
        raise VerifyOrchestrationError(
            "reviser output must contain only candidates"
        )
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise VerifyOrchestrationError("reviser candidates must be a list")
    results = {result.id: result for result in _results_for_job(store, job)}
    allowed_ids = set(_requested_revision_result_ids(job))
    if not allowed_ids or any(
        result_id not in results
        or results[result_id].result_kind != "finding"
        for result_id in allowed_ids
    ):
        raise VerifyOrchestrationError(
            "reviser is not bound to requested finding results"
        )
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for candidate in candidates:
        item = _mapping(candidate, "reviser candidate")
        if set(item) != {
            "evaluation_result_id",
            "replacement",
            "rationale",
            "tldr",
        }:
            raise VerifyOrchestrationError(
                "reviser candidate has unsupported fields"
            )
        result_id = _required_text(
            item.get("evaluation_result_id"),
            "evaluation_result_id",
        )
        if result_id not in allowed_ids or result_id in seen:
            raise VerifyOrchestrationError(
                "reviser candidate references an unbound or duplicate result"
            )
        seen.add(result_id)
        replacement = _required_text(item.get("replacement"), "replacement")
        normalized.append(
            {
                "evaluation_result_id": result_id,
                "replacement": replacement,
                "rationale": _required_text(
                    item.get("rationale"),
                    "candidate rationale",
                ),
                "tldr": _required_text(item.get("tldr"), "candidate tldr"),
            }
        )
    return {"candidates": normalized}


def _validate_coordinator_output(
    store: TruthStore,
    job: VerifyRuntimeJob,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if set(payload) != {"decisions", "summary"}:
        raise VerifyOrchestrationError(
            "coordinator output must contain decisions and summary"
        )
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise VerifyOrchestrationError("coordinator decisions must be a list")
    results = {
        result.id: result for result in _results_for_job(store, job)
    }
    stage = _coordinator_stage(job)
    expected_ids = (
        set(_requested_revision_result_ids(job))
        if stage == "post_revision"
        else set(results)
    )
    if not expected_ids or any(result_id not in results for result_id in expected_ids):
        raise VerifyOrchestrationError(
            "coordinator stage is not bound to valid normalized results"
        )
    candidate_context = _candidate_context(store, job)
    candidates = {
        str(item.get("evaluation_result_id")): item
        for item in candidate_context.get("candidates", [])
        if isinstance(item, Mapping)
    }
    affected_evaluations = {
        str(item.get("evaluation_result_id")): item
        for item in candidate_context.get("affected_evaluations", [])
        if isinstance(item, Mapping)
    }
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for decision in decisions:
        item = _mapping(decision, "coordinator decision")
        if set(item) != {
            "evaluation_result_id",
            "decision",
            "rationale",
        }:
            raise VerifyOrchestrationError(
                "coordinator decision has unsupported fields"
            )
        result_id = _required_text(
            item.get("evaluation_result_id"),
            "evaluation_result_id",
        )
        choice = _required_text(item.get("decision"), "decision")
        if result_id not in expected_ids or result_id in seen:
            raise VerifyOrchestrationError(
                "coordinator decision references an unbound or duplicate result"
            )
        if choice not in _coordinator_decisions(job):
            raise VerifyOrchestrationError("unsupported coordinator decision")
        result = results[result_id]
        if result.result_kind == "conforming" and (
            stage != "initial" or choice != "retain"
        ):
            raise VerifyOrchestrationError(
                "conforming results must be quietly retained"
            )
        if result.result_kind != "finding" and result.result_kind != "conforming":
            raise VerifyOrchestrationError(
                "coordinator received an unsupported result kind"
            )
        if choice == "request_revision" and result.result_kind != "finding":
            raise VerifyOrchestrationError(
                "only a finding may request revision"
            )
        if choice == "route_to_correction" and result_id not in candidates:
            raise VerifyOrchestrationError(
                "route_to_correction requires the reviser's candidate"
            )
        if choice == "route_to_correction" and (
            affected_evaluations.get(result_id, {}).get("status") != "passed"
        ):
            raise VerifyOrchestrationError(
                "route_to_correction requires a passing deterministic "
                "affected-region re-evaluation"
            )
        seen.add(result_id)
        normalized.append(
            {
                "evaluation_result_id": result_id,
                "decision": choice,
                "rationale": _required_text(
                    item.get("rationale"),
                    "decision rationale",
                ),
            }
        )
    if seen != expected_ids:
        raise VerifyOrchestrationError(
            "coordinator must decide every result assigned to its stage"
        )
    return {
        "decisions": normalized,
        "summary": _required_text(payload.get("summary"), "summary"),
    }


def _validate_cothink_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not set(payload).issubset({"outcome", "content", "rationale"}):
        raise VerifyOrchestrationError("Co-think output has unsupported fields")
    outcome = _required_text(payload.get("outcome"), "Co-think outcome")
    if outcome not in {"perspective", "none"}:
        raise VerifyOrchestrationError(
            "Co-think outcome must be perspective or none"
        )
    rationale = _required_text(payload.get("rationale"), "Co-think rationale")
    content = str(payload.get("content") or "")
    if outcome == "perspective" and not content.strip():
        raise VerifyOrchestrationError(
            "a Co-think perspective requires nonempty content"
        )
    return {"outcome": outcome, "content": content, "rationale": rationale}


def _process_coordinator(
    store: TruthStore,
    job: VerifyRuntimeJob,
    payload: Mapping[str, Any],
) -> dict[str, list[str]]:
    actor = _agent_actor(job)
    action = _record(store, ActionSnapshot, job.action_snapshot_id)
    stage = _coordinator_stage(job)
    policy_snapshot_sha256 = _required_text(
        job.request.get("effective_policy_sha256"),
        "effective_policy_sha256",
    )
    results = {
        result.id: result for result in _results_for_job(store, job)
    }
    candidate_context = _candidate_context(store, job)
    candidates = {
        str(item.get("evaluation_result_id")): item
        for item in candidate_context.get("candidates", [])
        if isinstance(item, Mapping)
    }
    affected_evaluations = {
        str(item.get("evaluation_result_id")): item
        for item in candidate_context.get("affected_evaluations", [])
        if isinstance(item, Mapping)
    }
    corrections: dict[str, tuple[CompositeSelector, Mapping[str, Any]]] = {}
    for decision in payload["decisions"]:
        if str(decision["decision"]) != "route_to_correction":
            continue
        if stage != "post_revision":
            raise VerifyOrchestrationError(
                "only a post-revision coordinator may route a correction"
            )
        result_id = str(decision["evaluation_result_id"])
        result = results[result_id]
        if affected_evaluations.get(result_id, {}).get("status") != "passed":
            raise VerifyOrchestrationError(
                "correction proposal requires a passing deterministic "
                "affected-region re-evaluation"
            )
        if result.evidence_selector_json is None:
            raise VerifyOrchestrationError(
                "a correction proposal requires exact evidence"
            )
        evidence = json.loads(result.evidence_selector_json)
        try:
            selector = CompositeSelector.from_web_annotation(evidence)
        except Exception as exc:
            raise VerifyOrchestrationError(
                "a correction proposal requires a valid exact evidence selector"
            ) from exc
        corrections[result_id] = (selector, candidates[result_id])

    proposal_ids: list[str] = []
    disposition_ids: list[str] = []
    requested_revision_result_ids: list[str] = []
    for decision in payload["decisions"]:
        result_id = str(decision["evaluation_result_id"])
        choice = str(decision["decision"])
        if choice == "request_revision":
            requested_revision_result_ids.append(result_id)
            continue
        disposition = record_routing_disposition(
            store,
            evaluation_result_id=result_id,
            decision=_PERSISTED_COORDINATOR_DECISIONS[choice],
            rationale=str(decision["rationale"]),
            actor=actor,
            policy_snapshot_sha256=policy_snapshot_sha256,
        )
        disposition_ids.append(disposition.id)
        if choice != "route_to_correction":
            continue
        selector, candidate = corrections[result_id]
        proposal = proposals.propose_edit(
            store,
            document_id=job.document_id,
            base_content_sha256=action.baseline_projection_sha256,
            base_structured_head_sha256=action.structured_head_sha256,
            selector=selector,
            quote_exact=selector.exact,
            replacement=str(candidate["replacement"]),
            rationale=str(candidate["rationale"]),
            tldr=str(candidate["tldr"]),
            actor=actor,
        )
        proposal_ids.append(proposal.id)
        record_result_relation(
            store,
            evaluation_result_id=result_id,
            relation_kind="addresses",
            target_kind="proposal",
            target_ref=proposal.id,
            actor=actor,
        )
    return {
        "proposal_ids": proposal_ids,
        "disposition_ids": disposition_ids,
        "requested_revision_result_ids": requested_revision_result_ids,
    }


def _create_and_launch_reviser(
    store: TruthStore,
    coordinator: VerifyRuntimeJob,
    *,
    requested_revision_result_ids: Sequence[str],
    disposition_ids: Sequence[str],
    spawn_detached: SpawnDetached | None = None,
) -> tuple[VerifyRuntimeJob, VerifyJobSpawnMetadata | None]:
    def _existing() -> VerifyRuntimeJob | None:
        return next(
            (
                candidate
                for candidate in jobs_for_run(
                    coordinator.store_id,
                    coordinator.evaluation_run_id,
                )
                if candidate.role is CoworkVerifyRole.REVISER
                and candidate.parent_job_id == coordinator.job_id
            ),
            None,
        )

    request_payload = dict(coordinator.request)
    request_payload["requested_revision_result_ids"] = list(
        requested_revision_result_ids
    )
    request_payload["prior_disposition_ids"] = list(
        dict.fromkeys(
            [
                *coordinator.request.get("prior_disposition_ids", []),
                *disposition_ids,
            ]
        )
    )
    reviser = _existing()
    if reviser is None:
        try:
            reviser = _create_job(
                store,
                document_id=coordinator.document_id,
                run_id=coordinator.evaluation_run_id,
                action_snapshot_id=coordinator.action_snapshot_id,
                plan_snapshot_id=coordinator.plan_snapshot_id,
                role=CoworkVerifyRole.REVISER,
                selection=_selection_from_mapping(coordinator.selection),
                request_payload=request_payload,
                parent_job_id=coordinator.job_id,
            )
        except sqlite3.IntegrityError:
            reviser = _existing()
            if reviser is None:
                raise
    if reviser.status == "prepared":
        return _launch_job(
            reviser,
            store=store,
            spawn_detached=spawn_detached,
        )
    return reviser, None


def _create_and_launch_coordinator(
    store: TruthStore,
    reviser: VerifyRuntimeJob,
    *,
    spawn_detached: SpawnDetached | None = None,
) -> VerifyRuntimeJob:
    def _existing() -> VerifyRuntimeJob | None:
        return next(
            (
                candidate
                for candidate in jobs_for_run(
                    reviser.store_id,
                    reviser.evaluation_run_id,
                )
                if candidate.role is CoworkVerifyRole.COORDINATOR
                and candidate.parent_job_id == reviser.job_id
            ),
            None,
        )

    coordinator = _existing()
    if coordinator is None:
        try:
            request_payload = dict(reviser.request)
            request_payload["coordinator_stage"] = "post_revision"
            raw_candidates = (
                reviser.output.get("candidates", [])
                if isinstance(reviser.output, Mapping)
                else []
            )
            candidates = (
                [
                    item
                    for item in raw_candidates
                    if isinstance(item, Mapping)
                ]
                if isinstance(raw_candidates, list)
                else []
            )
            request_payload["candidate_evaluations"] = (
                _affected_candidate_evaluations(
                    store,
                    reviser,
                    candidates,
                )
            )
            coordinator = _create_job(
                store,
                document_id=reviser.document_id,
                run_id=reviser.evaluation_run_id,
                action_snapshot_id=reviser.action_snapshot_id,
                plan_snapshot_id=reviser.plan_snapshot_id,
                role=CoworkVerifyRole.COORDINATOR,
                selection=_selection_from_mapping(reviser.selection),
                request_payload=request_payload,
                parent_job_id=reviser.job_id,
            )
        except sqlite3.IntegrityError:
            # A concurrent identical reviser submission may have won the
            # one-coordinator-per-parent uniqueness race.
            coordinator = _existing()
            if coordinator is None:
                raise
    if coordinator.status == "prepared":
        coordinator, _ = _launch_job(
            coordinator,
            store=store,
            spawn_detached=spawn_detached,
        )
    return coordinator


def _proposal_ids_for_job(
    store: TruthStore,
    job: VerifyRuntimeJob,
) -> list[str]:
    result_ids = {result.id for result in _results_for_job(store, job)}
    return [
        relation.target_ref
        for relation in verify_store.list_records(store, ResultRelation)
        if relation.evaluation_result_id in result_ids
        and relation.relation_kind == "addresses"
        and relation.target_kind == "proposal"
        and relation.created_by_kind == "agent_run"
        and relation.created_by_ref == job.job_id
    ]


def _disposition_ids_for_job(
    store: TruthStore,
    job: VerifyRuntimeJob,
) -> list[str]:
    return [
        disposition.id
        for disposition in verify_store.list_records(
            store,
            RoutingDisposition,
        )
        if disposition.created_by_kind == "agent_run"
        and disposition.created_by_ref == job.job_id
    ]


def _cothink_item_id_for_job(
    store: TruthStore,
    job: VerifyRuntimeJob,
) -> str | None:
    for item in verify_store.list_records(
        store,
        CothinkItem,
        where="source.action_snapshot_id = ?",
        params=(job.action_snapshot_id,),
    ):
        provenance = json.loads(item.provenance_json)
        if provenance.get("job_id") == job.job_id:
            return item.id
    return None


def _completed_submission_response(
    store: TruthStore,
    job: VerifyRuntimeJob,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": True,
        "job_id": job.job_id,
        "status": "completed",
        "replayed": True,
        "output_sha256": job.output_sha256,
    }
    if job.role is CoworkVerifyRole.REVISER:
        coordinator = next(
            (
                candidate
                for candidate in jobs_for_run(
                    job.store_id,
                    job.evaluation_run_id,
                )
                if candidate.role is CoworkVerifyRole.COORDINATOR
                and candidate.parent_job_id == job.job_id
            ),
            None,
        )
        if coordinator is not None:
            response["next_job_id"] = coordinator.job_id
            response["next_status"] = coordinator.status
    elif job.role is CoworkVerifyRole.COORDINATOR:
        if _coordinator_stage(job) == "initial":
            response["proposal_ids"] = []
            reviser = next(
                (
                    candidate
                    for candidate in jobs_for_run(
                        job.store_id,
                        job.evaluation_run_id,
                    )
                    if candidate.role is CoworkVerifyRole.REVISER
                    and candidate.parent_job_id == job.job_id
                ),
                None,
            )
            if reviser is not None:
                next_job = reviser
                if reviser.status in {"unavailable", "failed"}:
                    fallback = next(
                        (
                            candidate
                            for candidate in jobs_for_run(
                                job.store_id,
                                job.evaluation_run_id,
                            )
                            if candidate.role is CoworkVerifyRole.COORDINATOR
                            and candidate.parent_job_id == reviser.job_id
                        ),
                        None,
                    )
                    if fallback is not None:
                        next_job = fallback
                response["next_job_id"] = next_job.job_id
                response["next_status"] = next_job.status
        else:
            response["proposal_ids"] = _proposal_ids_for_job(store, job)
    elif job.role is CoworkVerifyRole.COTHINK:
        response["cothink_item_id"] = _cothink_item_id_for_job(store, job)
    refs: dict[str, Any] = {
        "proposal_ids": list(response.get("proposal_ids", [])),
        "disposition_ids": _disposition_ids_for_job(store, job),
        "requested_revision_result_ids": [
            str(decision["evaluation_result_id"])
            for decision in (
                job.output.get("decisions", [])
                if isinstance(job.output, Mapping)
                and isinstance(job.output.get("decisions"), list)
                else []
            )
            if isinstance(decision, Mapping)
            and decision.get("decision") == "request_revision"
        ],
    }
    if isinstance(response.get("next_job_id"), str):
        refs["next_job_id"] = response["next_job_id"]
    if isinstance(response.get("cothink_item_id"), str):
        refs["cothink_item_id"] = response["cothink_item_id"]
    record_coordination_status(store, job, consequence_refs=refs)
    return response


def _project_claimed_submission(
    store: TruthStore,
    job: VerifyRuntimeJob,
    normalized: Mapping[str, Any],
    *,
    normalized_sha256: str,
    projection_owner: str,
    replayed: bool,
    spawn_detached: SpawnDetached | None = None,
) -> dict[str, Any]:
    """Append idempotent typed consequences while holding the projection lease."""

    try:
        if job.role is CoworkVerifyRole.REVISER:
            coordinator = _create_and_launch_coordinator(
                store,
                get_job(job.job_id) or job,
                spawn_detached=spawn_detached,
            )
            completed_job = update_job(
                job.job_id,
                status="completed",
                expected_projection_owner=projection_owner,
            )
            record_coordination_status(
                store,
                completed_job,
                consequence_refs={"next_job_id": coordinator.job_id},
            )
            return {
                "ok": True,
                "job_id": job.job_id,
                "status": "completed",
                "replayed": replayed,
                "output_sha256": normalized_sha256,
                "next_job_id": coordinator.job_id,
                "next_status": coordinator.status,
            }
        if job.role is CoworkVerifyRole.COORDINATOR:
            outcome = _process_coordinator(store, job, normalized)
            next_job: VerifyRuntimeJob | None = None
            if (
                _coordinator_stage(job) == "initial"
                and outcome["requested_revision_result_ids"]
            ):
                reviser, metadata = _create_and_launch_reviser(
                    store,
                    job,
                    requested_revision_result_ids=outcome[
                        "requested_revision_result_ids"
                    ],
                    disposition_ids=outcome["disposition_ids"],
                    spawn_detached=spawn_detached,
                )
                next_job = reviser
                if metadata is not None and not metadata.ok:
                    next_job = _create_and_launch_coordinator(
                        store,
                        reviser,
                        spawn_detached=spawn_detached,
                    )
            completed_job = update_job(
                job.job_id,
                status="completed",
                expected_projection_owner=projection_owner,
            )
            consequence_refs: dict[str, Any] = {
                "proposal_ids": outcome["proposal_ids"],
                "disposition_ids": outcome["disposition_ids"],
                "requested_revision_result_ids": outcome[
                    "requested_revision_result_ids"
                ],
            }
            if next_job is not None:
                consequence_refs["next_job_id"] = next_job.job_id
            record_coordination_status(
                store,
                completed_job,
                consequence_refs=consequence_refs,
            )
            if _coordinator_stage(job) == "post_revision":
                if job.parent_job_id is None:
                    raise VerifyOrchestrationError(
                        "post-revision coordinator has no reviser parent"
                    )
                redact_job_output(job.parent_job_id)
            response = {
                "ok": True,
                "job_id": job.job_id,
                "status": "completed",
                "replayed": replayed,
                "output_sha256": normalized_sha256,
                "proposal_ids": outcome["proposal_ids"],
            }
            if next_job is not None:
                response["next_job_id"] = next_job.job_id
                response["next_status"] = next_job.status
            return response
        actor = _agent_actor(job)
        item_id: str | None = None
        if normalized["outcome"] == "perspective":
            item = record_cothink_item(
                store,
                action_snapshot_id=job.action_snapshot_id,
                subtype="alternative_perspective",
                purpose=str(
                    job.request.get("user_goal")
                    or "Invite another perspective"
                ),
                payload={
                    "text": normalized["content"],
                    "status": "open",
                },
                rationale=str(normalized["rationale"]),
                provenance={
                    "kind": "account_backed_agent",
                    "job_id": job.job_id,
                    "provider_id": job.selection["provider_id"],
                    "model_id": job.selection["model_id"],
                },
                actor=actor,
            )
            item_id = item.id
        completed_job = update_job(
            job.job_id,
            status="completed",
            expected_projection_owner=projection_owner,
        )
        record_coordination_status(
            store,
            completed_job,
            consequence_refs=(
                {}
                if item_id is None
                else {"cothink_item_id": item_id}
            ),
        )
        return {
            "ok": True,
            "job_id": job.job_id,
            "status": "completed",
            "replayed": replayed,
            "output_sha256": normalized_sha256,
            "cothink_item_id": item_id,
        }
    except Exception:
        current = get_job(job.job_id)
        if (
            current is not None
            and current.status == "submitted"
            and current.projection_owner == projection_owner
        ):
            failed_job = update_job(
                job.job_id,
                status="failed",
                error_code="job_processing_failed",
                error="The typed submission could not be projected.",
                expected_projection_owner=projection_owner,
            )
            record_coordination_status(store, failed_job)
        raise


def _claim_and_project_submission(
    store: TruthStore,
    job: VerifyRuntimeJob,
    *,
    projection_owner: str,
    replayed: bool,
    spawn_detached: SpawnDetached | None = None,
) -> dict[str, Any] | None:
    claimed_job, claimed = claim_job_projection(
        job.job_id,
        projection_owner=projection_owner,
    )
    if not claimed:
        current = get_job(job.job_id)
        if current is not None and current.status == "completed":
            return _completed_submission_response(store, current)
        return None
    if claimed_job.output is None or claimed_job.output_sha256 is None:
        raise VerifyOrchestrationError(
            "submitted Verify job has no readable typed output"
        )
    normalized_sha256 = sha256_text(canonical_json(claimed_job.output))
    if normalized_sha256 != claimed_job.output_sha256:
        raise VerifyOrchestrationError(
            "submitted Verify job output failed integrity validation"
        )
    return _project_claimed_submission(
        store,
        claimed_job,
        claimed_job.output,
        normalized_sha256=normalized_sha256,
        projection_owner=projection_owner,
        replayed=replayed,
        spawn_detached=spawn_detached,
    )


def resume_submitted_job(
    job_id: str,
    *,
    spawn_detached: SpawnDetached | None = None,
) -> dict[str, Any] | None:
    """Resume a crash-stranded typed submission without another model call."""

    job = get_job(job_id)
    if job is None:
        raise VerifyOrchestrationError("Verify job does not exist")
    if job.status == "completed":
        store = TruthStoreRegistry().open_store(job.store_id)
        return _completed_submission_response(store, job)
    if job.status != "submitted":
        return None
    store = TruthStoreRegistry().open_store(job.store_id)
    return _claim_and_project_submission(
        store,
        job,
        projection_owner=f"reconcile:{new_id()}",
        replayed=True,
        spawn_detached=spawn_detached,
    )


def submit_worker_job(
    *,
    job_id: str,
    payload: Mapping[str, Any],
    agent_session_id: str | None,
    spawn_detached: SpawnDetached | None = None,
) -> dict[str, Any]:
    """Validate one role output and append only its authorized consequences."""

    job = _bound_job(job_id, agent_session_id)
    normalized_input = _mapping(payload, "job payload")
    store = TruthStoreRegistry().open_store(job.store_id)
    replayed = job.output_sha256 is not None
    if job.output_sha256 is not None:
        normalized = (
            _validate_cothink_output(normalized_input)
            if job.role is CoworkVerifyRole.COTHINK
            else normalized_input
        )
        normalized_sha256 = sha256_text(canonical_json(normalized))
        if job.output_sha256 != normalized_sha256:
            raise VerifyOrchestrationError(
                "Verify job already received a different submission"
            )
        if job.status == "completed":
            return _completed_submission_response(store, job)
        if job.status != "submitted":
            raise VerifyOrchestrationError(
                f"Verify job cannot resume submission in state {job.status}"
            )
    else:
        if job.status not in {"prepared", "launching", "running"}:
            raise VerifyOrchestrationError(
                f"Verify job cannot submit in state {job.status}"
            )
        if job.role is CoworkVerifyRole.REVISER:
            normalized = _validate_reviser_output(store, job, normalized_input)
        elif job.role is CoworkVerifyRole.COORDINATOR:
            normalized = _validate_coordinator_output(store, job, normalized_input)
        elif job.role is CoworkVerifyRole.COTHINK:
            normalized = _validate_cothink_output(normalized_input)
        else:
            raise VerifyOrchestrationError(
                "model-based specialist submission is not enabled in this slice"
            )
        normalized_sha256 = sha256_text(canonical_json(normalized))
        job = update_job(
            job.job_id,
            status="submitted",
            output_sha256=normalized_sha256,
            output=normalized,
        )
        record_coordination_status(store, job)
    if job.status == "submitted":
        # Backfill a portable submitted fact after any crash between the
        # runtime transition and its Truth projection.
        record_coordination_status(store, job)
    projected = _claim_and_project_submission(
        store,
        job,
        projection_owner=f"worker:{job.session_id}",
        replayed=replayed,
        spawn_detached=spawn_detached,
    )
    if projected is not None:
        return projected
    return {
        "ok": True,
        "job_id": job.job_id,
        "status": "submitted",
        "projection_status": "in_progress",
        "replayed": True,
        "output_sha256": normalized_sha256,
    }


def run_status_projection(
    store: TruthStore,
    *,
    document_id: str,
    current_document: DocumentRecord | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    """Project durable run history without exposing undispositioned results."""

    coordination = portable_coordination_jobs(
        store,
        document_id=document_id,
        conn=conn,
    )
    if not coordination:
        from work_buddy.cowork.verify_coordination import (
            coordination_jobs_with_runtime_fallback,
        )

        coordination = coordination_jobs_with_runtime_fallback(
            store,
            document_id=document_id,
            conn=conn,
        )
    by_run: dict[str, list[dict[str, Any]]] = {}
    for item in coordination:
        run_id = item["evaluation_run_id"]
        if item["role"] == CoworkVerifyRole.COTHINK.value or run_id is None:
            continue
        by_run.setdefault(str(run_id), []).append(item)
    document = current_document or documents.get_document(
        store,
        document_id,
        conn=conn,
    )
    if document.id != document_id:
        raise VerifyOrchestrationError(
            "run projection document binding is invalid"
        )
    current_head = (
        None
        if document.ydoc_snapshot_sha256 is None
        else __import__(
            "work_buddy.truth.ydoc_store",
            fromlist=["current_structured_head"],
        ).current_structured_head(
            store,
            document_id=document_id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
    )
    summaries: list[dict[str, Any]] = []
    for run in verify_store.list_records(store, EvaluationRun, conn=conn):
        action = verify_store.get_record(
            store,
            ActionSnapshot,
            run.action_snapshot_id,
            conn=conn,
        )
        if action is None or action.document_id != document_id:
            continue
        run_jobs = by_run.get(run.id, [])
        results = verify_store.list_records(
            store,
            EvaluationResult,
            where="source.evaluation_run_id = ?",
            params=(run.id,),
            conn=conn,
        )
        surfaced = 0
        from work_buddy.cowork.verify import store as store_module

        for result in results:
            disposition = store_module.latest_disposition(
                store,
                result.id,
                conn=conn,
            )
            if (
                disposition is not None
                and disposition.decision in {"surface", "route_to_correction"}
            ):
                surfaced += 1
        coordinator = next(
            (
                candidate
                for candidate in reversed(run_jobs)
                if candidate["role"] == CoworkVerifyRole.COORDINATOR.value
            ),
            None,
        )
        has_active = any(
            candidate["status"]
            in {"prepared", "launching", "running", "submitted"}
            for candidate in run_jobs
        )
        has_unavailable = (
            coordinator is None
            or any(
                candidate["status"] in {"unavailable", "failed", None}
                for candidate in run_jobs
            )
        )
        effective_status = (
            "running"
            if has_active
            else "completed_with_failures"
            if has_unavailable
            else "completed"
        )
        root = next(
            (
                candidate
                for candidate in run_jobs
                if candidate["parent_job_id"] is None
                and candidate["role"] == CoworkVerifyRole.COORDINATOR.value
            ),
            None,
        )
        selection = (
            {}
            if root is None and not run_jobs
            else (root or run_jobs[-1])["selection"]
        )
        request_summary = (
            {}
            if root is None
            else root["request_summary"]
        )
        target_selector = json.loads(action.target_selector_json)
        summaries.append(
            {
                "run_id": run.id,
                "status": effective_status,
                "purpose": str(
                    request_summary.get("user_goal")
                    or "Preferred terminology"
                ),
                "target_label": (
                    "Whole document"
                    if action.target_kind == "document"
                    else "Document target"
                ),
                "coverage_label": (
                    "Complete exact-string coverage of the frozen document"
                    if action.target_kind == "document"
                    else "Complete exact-string coverage of the frozen target"
                ),
                "current_version": current_head == action.structured_head_sha256,
                "result_count": len(results),
                "surfaced_result_count": surfaced,
                "coordination_status": (
                    "pending"
                    if coordinator is not None and coordinator["status"] in {
                        "prepared",
                        "launching",
                        "running",
                        "submitted",
                    }
                    else "unavailable"
                    if coordinator is None
                    or coordinator["status"] in {"unavailable", "failed", None}
                    else "completed"
                ),
                "provider_label": str(
                    selection.get("provider_label")
                    or selection.get("provider_id")
                    or ""
                )
                or None,
                "provider_id": str(selection.get("provider_id") or "") or None,
                "model_label": str(
                    selection.get("model_label")
                    or selection.get("model_id")
                    or ""
                )
                or None,
                "model_id": str(selection.get("model_id") or "") or None,
                "created_at": run.started_at,
                "finished_at": (
                    None
                    if has_active
                    else (
                        run_jobs[-1]["updated_at"]
                        if run_jobs
                        else run.completed_at
                    )
                ),
                "_target_selector": target_selector,
            }
        )
    return tuple(
        sorted(summaries, key=lambda item: (item["created_at"], item["run_id"]))
    )


__all__ = [
    "VERIFY_CONTRACT_VERSION",
    "VerifyOrchestrationError",
    "get_worker_job",
    "run_status_projection",
    "start_cothink",
    "start_verify_run",
    "submit_worker_job",
]
