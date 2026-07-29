"""Portable, sanitized coordination history for Co-work Verify and Co-think.

The machine-local runtime database owns leases, process identifiers, and
private typed worker output.  This module records only the immutable job
binding and append-only lifecycle facts needed to understand a run after a
Truth export/import.  Arbitrary request keys, provider diagnostics, raw model
output, and revision text are deliberately excluded.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from work_buddy.cowork.execution_identity import CoworkVerifyRole
from work_buddy.cowork.verify_candidate_evaluation import (
    CandidateEvaluationError,
    sanitize_candidate_evaluations,
)
from work_buddy.cowork.verify import (
    ActionSnapshot,
    CheckDefinitionVersion,
    CheckExecution,
    CoworkCoordinationJob,
    CoworkCoordinationStatusEvent,
    CoworkReviewApplication,
    CriterionCheckBinding,
    CriterionDefinitionVersion,
    EvaluationResult,
    EvaluationPlanSnapshot,
    EvaluationRun,
    ModelCallAuthorizationReceipt,
    VerifyInvariantViolation,
    admitted_check_executor,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_jobs import MAX_VERIFY_JOB_BUDGET_USD
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import canonical_json, new_id, sha256_text, utc_now
from work_buddy.truth.store import TruthStore


COORDINATION_STATUSES = frozenset(
    {
        "prepared",
        "launching",
        "running",
        "submitted",
        "completed",
        "unavailable",
        "failed",
    }
)
COORDINATION_OUTCOMES = frozenset(
    {
        "typed_submission_received",
        "routing_completed",
        "revision_requested",
        "revision_candidate_prepared",
        "correction_routing_completed",
        "completed_with_item",
        "completed_no_useful_item",
        "unavailable",
    }
)
_TERMINAL_STATUSES = frozenset({"completed", "unavailable", "failed"})
_TRANSITIONS = {
    "prepared": frozenset(
        {"launching", "running", "submitted", "unavailable", "failed"}
    ),
    "launching": frozenset(
        {"running", "submitted", "unavailable", "failed"}
    ),
    "running": frozenset({"submitted", "unavailable", "failed"}),
    "submitted": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "unavailable": frozenset(),
    "failed": frozenset(),
}
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SYSTEM_ACTOR = Actor("system", "cowork-coordination")


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerifyInvariantViolation(f"{label} must be a nonempty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerifyInvariantViolation(
            f"{label} must be an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerifyInvariantViolation(f"{label} must carry a UTC offset")
    return value


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise VerifyInvariantViolation(f"{label} must be a lowercase 32-hex id")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise VerifyInvariantViolation(
            f"{label} must be a lowercase SHA-256 digest"
        )
    return value


def _actor_fields(actor: Actor) -> tuple[str, str | None, str | None]:
    if not isinstance(actor, Actor):
        raise TypeError("actor must be an Actor")
    return (
        actor.kind,
        actor.ref,
        canonical_json(dict(actor.meta)) if actor.meta else None,
    )


def _string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if (
        isinstance(value, (str, bytes, bytearray))
        or not isinstance(value, Sequence)
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise VerifyInvariantViolation(f"{label} must be a list of ids")
    result = list(value)
    if len(result) != len(set(result)):
        raise VerifyInvariantViolation(f"{label} contains duplicates")
    return result


def _id_list(value: object, label: str) -> list[str]:
    return [_id(item, label) for item in _string_list(value, label)]


def _specialist_assignment(
    role: CoworkVerifyRole,
    value: object,
) -> dict[str, Any] | None:
    """Sanitize the immutable check binding assigned to one specialist."""

    if role is not CoworkVerifyRole.SPECIALIST:
        return None
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise VerifyInvariantViolation(
            "specialist_assignment must be an object"
        )
    fields = {
        "criterion_definition_version_id",
        "check_definition_version_id",
        "criterion_check_binding_id",
        "sequence",
        "total",
        "configuration_sha256",
    }
    if set(value) != fields:
        raise VerifyInvariantViolation(
            "specialist_assignment must contain exactly its admitted fields"
        )
    sequence = value["sequence"]
    total = value["total"]
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
    ):
        raise VerifyInvariantViolation(
            "specialist_assignment.sequence must be a positive integer"
        )
    if isinstance(total, bool) or not isinstance(total, int) or total < 1:
        raise VerifyInvariantViolation(
            "specialist_assignment.total must be a positive integer"
        )
    if sequence > total:
        raise VerifyInvariantViolation(
            "specialist_assignment.sequence cannot exceed total"
        )
    return {
        "criterion_definition_version_id": _id(
            value["criterion_definition_version_id"],
            "specialist_assignment.criterion_definition_version_id",
        ),
        "check_definition_version_id": _id(
            value["check_definition_version_id"],
            "specialist_assignment.check_definition_version_id",
        ),
        "criterion_check_binding_id": _id(
            value["criterion_check_binding_id"],
            "specialist_assignment.criterion_check_binding_id",
        ),
        "sequence": sequence,
        "total": total,
        "configuration_sha256": _digest(
            value["configuration_sha256"],
            "specialist_assignment.configuration_sha256",
        ),
    }


def sanitized_request_summary(
    role: CoworkVerifyRole,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact forest-context bindings, excluding arbitrary keys."""

    effective_configuration = request.get("effective_configuration")
    if effective_configuration is not None and not isinstance(
        effective_configuration,
        Mapping,
    ):
        raise VerifyInvariantViolation(
            "effective_configuration must be an object"
        )
    recheck_target_confirmation = request.get(
        "recheck_target_confirmation"
    )
    if (
        recheck_target_confirmation is not None
        and not isinstance(recheck_target_confirmation, Mapping)
    ):
        raise VerifyInvariantViolation(
            "recheck_target_confirmation must be an object"
        )
    summary = {
        "schema": "work-buddy.cowork-coordination-request/v1",
        "user_goal": str(request.get("user_goal") or ""),
        "protected_intent": str(request.get("protected_intent") or ""),
        "effective_configuration": (
            None
            if effective_configuration is None
            else dict(effective_configuration)
        ),
        "effective_configuration_sha256": request.get(
            "effective_configuration_sha256"
        ),
        "effective_policy_sha256": request.get("effective_policy_sha256"),
        "active_criterion_ids": _string_list(
            request.get("active_criterion_ids"),
            "active_criterion_ids",
        ),
        "prior_disposition_ids": _string_list(
            request.get("prior_disposition_ids"),
            "prior_disposition_ids",
        ),
        "prior_human_review_outcome_ids": _string_list(
            request.get("prior_human_review_outcome_ids"),
            "prior_human_review_outcome_ids",
        ),
        "recheck_of_run_id": request.get("recheck_of_run_id"),
        "recheck_of_proposal_ids": _string_list(
            request.get("recheck_of_proposal_ids"),
            "recheck_of_proposal_ids",
        ),
        "recheck_intent_id": request.get("recheck_intent_id"),
        "coordinator_stage": request.get("coordinator_stage"),
        "requested_revision_result_ids": _string_list(
            request.get("requested_revision_result_ids"),
            "requested_revision_result_ids",
        ),
        "candidate_evaluations": [],
        "specialist_assignment": _specialist_assignment(
            role,
            request.get("specialist_assignment"),
        ),
    }
    # Keep pre-confirmation v1 jobs byte-compatible during replay/export while
    # persisting the field (including explicit null) for newly created jobs.
    if "recheck_target_confirmation" in request:
        summary["recheck_target_confirmation"] = (
            None
            if recheck_target_confirmation is None
            else dict(recheck_target_confirmation)
        )
    try:
        summary["candidate_evaluations"] = sanitize_candidate_evaluations(
            request.get("candidate_evaluations", [])
        )
    except CandidateEvaluationError as exc:
        raise VerifyInvariantViolation(str(exc)) from exc
    if role is CoworkVerifyRole.COTHINK:
        # Co-think has no evaluation configuration, but retaining explicit
        # null/empty fields keeps the portable shape stable for inspection.
        summary["coordinator_stage"] = None
    for field in (
        "effective_configuration_sha256",
        "effective_policy_sha256",
    ):
        value = summary[field]
        if value is not None:
            _digest(value, field)
    for field in (
        "recheck_of_run_id",
        "recheck_intent_id",
        "coordinator_stage",
    ):
        value = summary[field]
        if value is not None and (not isinstance(value, str) or not value):
            raise VerifyInvariantViolation(f"{field} must be nonempty text")
    # Canonicalization is also the admission check for unsupported JSON
    # values such as NaN or process-local objects.
    canonical_json(summary)
    return summary


def _selection_summary(selection: Mapping[str, Any]) -> dict[str, str]:
    result = {
        "provider_id": str(selection.get("provider_id") or ""),
        "model_id": str(selection.get("model_id") or ""),
        "provider_label": str(selection.get("provider_label") or ""),
        "model_label": str(selection.get("model_label") or ""),
    }
    if not result["provider_id"] or not result["model_id"]:
        raise VerifyInvariantViolation(
            "coordination selection requires provider_id and model_id"
        )
    return result


def _validate_authorization_boundary(
    receipt: ModelCallAuthorizationReceipt,
    *,
    role: CoworkVerifyRole,
    job_id: str,
    action_snapshot_id: str,
    request: Mapping[str, Any],
) -> None:
    """Require the portable job to retain its exact execution authorization."""

    try:
        boundary = json.loads(receipt.content_boundary_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VerifyInvariantViolation(
            "coordination job authorization boundary is invalid"
        ) from exc
    expected_fields = {
        "role",
        "job_id",
        "document",
        "action_snapshot_id",
        "authority_context",
    }
    expected_document = (
        "captured_target_only"
        if role is CoworkVerifyRole.SPECIALIST
        else "complete_permitted_frozen_projection"
    )
    expected_authority_context = {
        "user_goal": str(request.get("user_goal") or ""),
        "protected_intent": str(request.get("protected_intent") or ""),
        "effective_configuration": request.get("effective_configuration"),
        "effective_configuration_sha256": request.get(
            "effective_configuration_sha256"
        ),
        "effective_policy_sha256": request.get("effective_policy_sha256"),
        "active_criterion_ids": list(request.get("active_criterion_ids", [])),
        "prior_disposition_ids": list(
            request.get("prior_disposition_ids", [])
        ),
        "prior_human_review_outcome_ids": list(
            request.get("prior_human_review_outcome_ids", [])
        ),
        "recheck_of_run_id": request.get("recheck_of_run_id"),
        "recheck_of_proposal_ids": list(
            request.get("recheck_of_proposal_ids", [])
        ),
        "recheck_intent_id": request.get("recheck_intent_id"),
        "coordinator_stage": request.get("coordinator_stage"),
        "requested_revision_result_ids": list(
            request.get("requested_revision_result_ids", [])
        ),
        "specialist_assignment": request.get("specialist_assignment"),
    }
    if "recheck_target_confirmation" in request:
        expected_authority_context["recheck_target_confirmation"] = (
            request.get("recheck_target_confirmation")
        )
    if (
        not isinstance(boundary, Mapping)
        or set(boundary) != expected_fields
        or boundary.get("role") != role.value
        or boundary.get("job_id") != job_id
        or boundary.get("document") != expected_document
        or boundary.get("action_snapshot_id") != action_snapshot_id
        or not isinstance(boundary.get("authority_context"), Mapping)
        or canonical_json(dict(boundary["authority_context"]))
        != canonical_json(expected_authority_context)
    ):
        raise VerifyInvariantViolation(
            "coordination job authorization boundary does not match its "
            "role, job, and content"
        )


def _validate_specialist_assignment_lineage(
    store: TruthStore,
    *,
    role: CoworkVerifyRole,
    action_snapshot_id: str,
    plan: EvaluationPlanSnapshot | None,
    request_summary: Mapping[str, Any],
) -> None:
    """Bind one specialist assignment to one exact frozen plan and binding."""

    if role is not CoworkVerifyRole.SPECIALIST:
        return
    assignment = request_summary.get("specialist_assignment")
    if not isinstance(assignment, Mapping) or plan is None:
        raise VerifyInvariantViolation(
            "specialist coordination requires an exact frozen assignment"
        )
    try:
        plan_payload = json.loads(plan.plan_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VerifyInvariantViolation(
            "specialist coordination plan is invalid"
        ) from exc
    checks = (
        plan_payload.get("checks")
        if isinstance(plan_payload, Mapping)
        else None
    )
    if (
        not isinstance(plan_payload, Mapping)
        or plan_payload.get("schema")
        != "work-buddy.cowork-evaluation-plan/v1"
        or plan_payload.get("action_snapshot_id") != action_snapshot_id
        or not isinstance(checks, list)
    ):
        raise VerifyInvariantViolation(
            "specialist coordination plan does not match its action snapshot"
        )
    plan_fields = {
        "criterion_definition_version_id",
        "check_definition_version_id",
        "criterion_check_binding_id",
        "criterion_activation_id",
        "configuration_sha256",
    }
    specialist_entries: list[dict[str, Any]] = []
    for check_entry in checks:
        if not isinstance(check_entry, Mapping) or set(check_entry) != plan_fields:
            raise VerifyInvariantViolation(
                "specialist coordination plan contains an invalid check entry"
            )
        criterion = verify_store.get_record(
            store,
            CriterionDefinitionVersion,
            check_entry["criterion_definition_version_id"],
        )
        check = verify_store.get_record(
            store,
            CheckDefinitionVersion,
            check_entry["check_definition_version_id"],
        )
        binding = verify_store.get_record(
            store,
            CriterionCheckBinding,
            check_entry["criterion_check_binding_id"],
        )
        if (
            criterion is None
            or check is None
            or binding is None
            or binding.criterion_definition_version_id != criterion.id
            or binding.check_definition_version_id != check.id
            or sha256_text(binding.configuration_json)
            != check_entry["configuration_sha256"]
        ):
            raise VerifyInvariantViolation(
                "specialist coordination plan no longer matches its immutable "
                "records"
            )
        executor = admitted_check_executor(
            check,
            criterion_kind=criterion.criterion_kind,
        )
        if executor is None:
            raise VerifyInvariantViolation(
                "specialist coordination plan contains an unadmitted check"
            )
        if executor.execution_mode == "account_backed_specialist":
            specialist_entries.append(
                {
                    "criterion_definition_version_id": criterion.id,
                    "check_definition_version_id": check.id,
                    "criterion_check_binding_id": binding.id,
                    "configuration_sha256": check_entry[
                        "configuration_sha256"
                    ],
                }
            )
    sequence = assignment["sequence"]
    total = len(specialist_entries)
    expected_assignment = (
        None
        if not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or sequence > total
        else {
            **specialist_entries[sequence - 1],
            "sequence": sequence,
            "total": total,
        }
    )
    if expected_assignment is None or dict(assignment) != expected_assignment:
        raise VerifyInvariantViolation(
            "specialist assignment does not match the exact admitted "
            "specialist sequence in its frozen plan"
        )


def _validate_specialist_parent_lineage(
    *,
    role: CoworkVerifyRole,
    parent: CoworkCoordinationJob | None,
    run_id: str | None,
    plan_id: str | None,
    request_summary: Mapping[str, Any],
) -> None:
    if role is not CoworkVerifyRole.SPECIALIST:
        return
    assignment = request_summary["specialist_assignment"]
    sequence = assignment["sequence"]
    if sequence == 1:
        if parent is not None:
            raise VerifyInvariantViolation(
                "first specialist assignment cannot have a parent job"
            )
        return
    if parent is None:
        raise VerifyInvariantViolation(
            "later specialist assignment requires the previous specialist job"
        )
    parent_request = json.loads(parent.request_summary_json)
    parent_assignment = parent_request.get("specialist_assignment")
    if (
        parent.role != CoworkVerifyRole.SPECIALIST.value
        or parent.evaluation_run_id != run_id
        or parent.plan_snapshot_id != plan_id
        or not isinstance(parent_assignment, Mapping)
        or parent_assignment.get("sequence") != sequence - 1
        or parent_assignment.get("total") != assignment["total"]
    ):
        raise VerifyInvariantViolation(
            "specialist parent does not match the previous frozen assignment"
        )


def record_coordination_job(
    store: TruthStore,
    job: Any,
    *,
    actor: Actor = _SYSTEM_ACTOR,
) -> CoworkCoordinationJob:
    """Persist one immutable job binding without private runtime payloads."""

    job_id = _id(job.job_id, "coordination job id")
    role = (
        job.role
        if isinstance(job.role, CoworkVerifyRole)
        else CoworkVerifyRole(str(job.role))
    )
    action = verify_store.get_record(store, ActionSnapshot, job.action_snapshot_id)
    if action is None or action.document_id != job.document_id:
        raise VerifyInvariantViolation(
            "coordination job action snapshot does not match its document"
        )
    plan_id = job.plan_snapshot_id
    plan: EvaluationPlanSnapshot | None = None
    if plan_id is not None:
        plan = verify_store.get_record(store, EvaluationPlanSnapshot, plan_id)
        if plan is None or plan.action_snapshot_id != action.id:
            raise VerifyInvariantViolation(
                "coordination job plan does not match its action snapshot"
            )
    run_id: str | None
    if role is CoworkVerifyRole.COTHINK:
        run_id = None
    else:
        run_id = _id(job.evaluation_run_id, "evaluation run id")
        run = verify_store.get_record(store, EvaluationRun, run_id)
        if run is None or run.action_snapshot_id != action.id:
            raise VerifyInvariantViolation(
                "coordination job run does not match its action snapshot"
            )
    receipt = verify_store.get_record(
        store,
        ModelCallAuthorizationReceipt,
        job.authorization_receipt_id,
    )
    if (
        receipt is None
        or receipt.action_snapshot_id != action.id
        or receipt.plan_snapshot_id != plan_id
        or receipt.context_sha256 != job.context_sha256
    ):
        raise VerifyInvariantViolation(
            "coordination job authorization binding is invalid"
        )
    _validate_authorization_boundary(
        receipt,
        role=role,
        job_id=job_id,
        action_snapshot_id=action.id,
        request=job.request,
    )
    parent_id = job.parent_job_id
    parent: CoworkCoordinationJob | None = None
    if parent_id is not None:
        parent_id = _id(parent_id, "coordination parent job id")
        parent = verify_store.get_record(
            store,
            CoworkCoordinationJob,
            parent_id,
        )
        if (
            parent is None
            or parent.document_id != job.document_id
            or parent.action_snapshot_id != action.id
            or parent.evaluation_run_id != run_id
        ):
            raise VerifyInvariantViolation(
                "coordination parent job binding is invalid"
            )
    selection = _selection_summary(job.selection)
    if (
        receipt.provider != selection["provider_id"]
        or receipt.model != selection["model_id"]
        or receipt.egress_class != "account_backed_agent"
        or receipt.cost_ceiling_usd != MAX_VERIFY_JOB_BUDGET_USD
        or receipt.retry_limit != 0
        or receipt.created_by_kind != "human"
        or receipt.created_by_ref
        != str(job.request.get("authorized_by_ref") or "dashboard-user")
    ):
        raise VerifyInvariantViolation(
            "coordination selection does not match its authorization"
        )
    request_summary = sanitized_request_summary(role, job.request)
    _validate_specialist_assignment_lineage(
        store,
        role=role,
        action_snapshot_id=action.id,
        plan=plan,
        request_summary=request_summary,
    )
    _validate_specialist_parent_lineage(
        role=role,
        parent=parent,
        run_id=run_id,
        plan_id=plan_id,
        request_summary=request_summary,
    )
    payload = {
        "document_id": job.document_id,
        "evaluation_run_id": run_id,
        "action_snapshot_id": action.id,
        "plan_snapshot_id": plan_id,
        "role": role.value,
        "parent_job_id": parent_id,
        "authorization_receipt_id": receipt.id,
        "context_sha256": _digest(job.context_sha256, "context_sha256"),
        "selection": selection,
        "request_summary": request_summary,
    }
    canonical_sha256 = sha256_text(canonical_json(payload))
    existing = verify_store.get_record(store, CoworkCoordinationJob, job_id)
    if existing is not None:
        if existing.canonical_sha256 != canonical_sha256:
            raise VerifyInvariantViolation(
                "coordination job id is already bound to different context"
            )
        return existing
    canonical_match = verify_store.get_by_canonical_sha256(
        store,
        CoworkCoordinationJob,
        canonical_sha256,
    )
    if canonical_match is not None:
        if canonical_match.id != job_id:
            raise VerifyInvariantViolation(
                "coordination context is already bound to another job"
            )
        return canonical_match
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    record = CoworkCoordinationJob(
        id=job_id,
        document_id=str(job.document_id),
        evaluation_run_id=run_id,
        action_snapshot_id=action.id,
        plan_snapshot_id=plan_id,
        role=role.value,
        parent_job_id=parent_id,
        authorization_receipt_id=receipt.id,
        context_sha256=str(job.context_sha256),
        selection_json=canonical_json(selection),
        request_summary_json=canonical_json(request_summary),
        canonical_sha256=canonical_sha256,
        created_at=_timestamp(job.created_at, "coordination job created_at"),
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    try:
        return verify_store.insert_record(store, record)
    except sqlite3.IntegrityError:
        concurrent = verify_store.get_record(
            store,
            CoworkCoordinationJob,
            job_id,
        )
        if concurrent is None or concurrent.canonical_sha256 != canonical_sha256:
            raise
        return concurrent


def _safe_error_code(value: object, *, status: str) -> str | None:
    if status not in {"unavailable", "failed"}:
        return None
    candidate = str(value or "").strip().lower()
    if _ERROR_CODE_RE.fullmatch(candidate) is None:
        return (
            "coordination_unavailable"
            if status == "unavailable"
            else "coordination_failed"
        )
    return candidate


def _safe_message(status: str) -> str | None:
    if status == "unavailable":
        return "The selected account-backed agent is unavailable."
    if status == "failed":
        return "The typed coordination result could not be completed."
    return None


def _infer_outcome(job: Any, status: str) -> str | None:
    if status in {"unavailable", "failed"}:
        return "unavailable"
    if status == "submitted":
        return "typed_submission_received"
    if status != "completed":
        return None
    role = (
        job.role
        if isinstance(job.role, CoworkVerifyRole)
        else CoworkVerifyRole(str(job.role))
    )
    output = job.output if isinstance(job.output, Mapping) else {}
    if role is CoworkVerifyRole.COTHINK:
        return (
            "completed_with_item"
            if output.get("outcome") == "perspective"
            else "completed_no_useful_item"
        )
    if role is CoworkVerifyRole.SPECIALIST:
        # The completed status plus its check/result consequence refs carries
        # completion semantics without widening the persisted v7 SQL enum.
        return "typed_submission_received"
    if role is CoworkVerifyRole.REVISER:
        return "revision_candidate_prepared"
    stage = str(job.request.get("coordinator_stage") or "initial")
    if stage == "post_revision":
        return "correction_routing_completed"
    decisions = output.get("decisions")
    if isinstance(decisions, list) and any(
        isinstance(decision, Mapping)
        and decision.get("decision") == "request_revision"
        for decision in decisions
    ):
        return "revision_requested"
    return "routing_completed"


def _consequence_refs(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = {} if value is None else dict(value)
    allowed: dict[str, Any] = {}
    for field in (
        "next_job_id",
        "cothink_item_id",
    ):
        item = raw.get(field)
        if item is not None:
            allowed[field] = _id(item, field)
    for field in (
        "proposal_ids",
        "disposition_ids",
        "requested_revision_result_ids",
    ):
        allowed[field] = _string_list(raw.get(field), field)
    for field in ("check_execution_ids", "evaluation_result_ids"):
        allowed[field] = _id_list(raw.get(field), field)
    return allowed


def _validate_specialist_consequence_lineage(
    store: TruthStore,
    *,
    binding: CoworkCoordinationJob,
    status: str,
    refs: Mapping[str, Any],
) -> None:
    """Keep specialist completion refs on their exact assigned execution."""

    execution_ids = refs.get("check_execution_ids", [])
    result_ids = refs.get("evaluation_result_ids", [])
    request_summary = json.loads(binding.request_summary_json)
    assignment = request_summary.get("specialist_assignment")
    if binding.role != CoworkVerifyRole.SPECIALIST.value:
        if execution_ids or result_ids:
            raise VerifyInvariantViolation(
                "non-specialist coordination cannot retain specialist "
                "execution lineage"
            )
        return
    if not isinstance(assignment, Mapping):
        if execution_ids or result_ids:
            raise VerifyInvariantViolation(
                "specialist execution lineage requires an exact assignment"
            )
        return
    if status == "completed" and (
        len(execution_ids) != 1 or not result_ids
    ):
        raise VerifyInvariantViolation(
            "completed specialist coordination requires one assigned "
            "execution and its results"
        )
    action = verify_store.get_record(
        store,
        ActionSnapshot,
        binding.action_snapshot_id,
    )
    if action is None:
        raise VerifyInvariantViolation(
            "specialist coordination action snapshot is unavailable"
        )
    executions: dict[str, CheckExecution] = {}
    for execution_id in execution_ids:
        execution = verify_store.get_record(
            store,
            CheckExecution,
            execution_id,
        )
        if execution is None:
            raise VerifyInvariantViolation(
                "specialist consequence references a missing check execution"
            )
        try:
            producer = json.loads(execution.producer_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VerifyInvariantViolation(
                "specialist execution producer is invalid"
            ) from exc
        if (
            execution.evaluation_run_id != binding.evaluation_run_id
            or execution.check_definition_version_id
            != assignment["check_definition_version_id"]
            or execution.criterion_check_binding_id
            != assignment["criterion_check_binding_id"]
            or execution.input_sha256 != action.target_text_sha256
            or not isinstance(producer, Mapping)
            or producer.get("kind") != "account_backed_specialist"
            or producer.get("job_id") != binding.id
        ):
            raise VerifyInvariantViolation(
                "specialist execution does not match its exact job assignment"
            )
        executions[execution.id] = execution
    for result_id in result_ids:
        result = verify_store.get_record(store, EvaluationResult, result_id)
        if (
            result is None
            or result.evaluation_run_id != binding.evaluation_run_id
            or result.criterion_definition_version_id
            != assignment["criterion_definition_version_id"]
            or result.check_execution_id not in executions
        ):
            raise VerifyInvariantViolation(
                "specialist result does not match its assigned execution "
                "lineage"
            )
    if status != "completed":
        return
    execution = next(iter(executions.values()))
    complete_result_ids = {
        result.id
        for result in verify_store.list_records(
            store,
            EvaluationResult,
            where="source.check_execution_id = ?",
            params=(execution.id,),
        )
    }
    if set(result_ids) != complete_result_ids:
        raise VerifyInvariantViolation(
            "specialist completion must retain the complete result set for "
            "its assigned execution"
        )
    next_job_id = refs.get("next_job_id")
    next_job = (
        None
        if next_job_id is None
        else verify_store.get_record(
            store,
            CoworkCoordinationJob,
            next_job_id,
        )
    )
    if next_job is None:
        raise VerifyInvariantViolation(
            "completed specialist coordination requires its exact next job"
        )
    if (
        next_job.document_id != binding.document_id
        or next_job.action_snapshot_id != binding.action_snapshot_id
        or next_job.evaluation_run_id != binding.evaluation_run_id
        or next_job.plan_snapshot_id != binding.plan_snapshot_id
    ):
        raise VerifyInvariantViolation(
            "specialist next job does not preserve its run and plan lineage"
        )
    sequence = assignment["sequence"]
    total = assignment["total"]
    next_request = json.loads(next_job.request_summary_json)
    if sequence < total:
        next_assignment = next_request.get("specialist_assignment")
        if (
            next_job.role != CoworkVerifyRole.SPECIALIST.value
            or next_job.parent_job_id != binding.id
            or not isinstance(next_assignment, Mapping)
            or next_assignment.get("sequence") != sequence + 1
            or next_assignment.get("total") != total
        ):
            raise VerifyInvariantViolation(
                "specialist handoff does not target the next frozen assignment"
            )
    elif (
        next_job.role != CoworkVerifyRole.COORDINATOR.value
        or next_job.parent_job_id is not None
        or next_request.get("coordinator_stage") != "initial"
    ):
        raise VerifyInvariantViolation(
            "final specialist handoff must target the initial coordinator"
        )


def record_coordination_status(
    store: TruthStore,
    job: Any,
    *,
    outcome_kind: str | None = None,
    consequence_refs: Mapping[str, Any] | None = None,
    actor: Actor = _SYSTEM_ACTOR,
    at: str | None = None,
) -> CoworkCoordinationStatusEvent:
    """Append one sanitized lifecycle state and never raw worker output."""

    binding = record_coordination_job(store, job, actor=actor)
    status = str(job.status)
    if status not in COORDINATION_STATUSES:
        raise VerifyInvariantViolation(
            f"unsupported coordination status: {status}"
        )
    outcome = _infer_outcome(job, status) if outcome_kind is None else outcome_kind
    if outcome is not None and outcome not in COORDINATION_OUTCOMES:
        raise VerifyInvariantViolation(
            f"unsupported coordination outcome: {outcome}"
        )
    output_sha256 = job.output_sha256
    if output_sha256 is not None:
        output_sha256 = _digest(output_sha256, "coordination output_sha256")
    refs = _consequence_refs(consequence_refs)
    _validate_specialist_consequence_lineage(
        store,
        binding=binding,
        status=status,
        refs=refs,
    )
    error_code = _safe_error_code(job.error_code, status=status)
    message = _safe_message(status)
    payload = {
        "coordination_job_id": binding.id,
        "status": status,
        "outcome_kind": outcome,
        "output_sha256": output_sha256,
        "error_code": error_code,
        "message": message,
        "consequence_refs": refs,
    }
    canonical_sha256 = sha256_text(canonical_json(payload))
    existing = verify_store.get_by_canonical_sha256(
        store,
        CoworkCoordinationStatusEvent,
        canonical_sha256,
    )
    if existing is not None:
        return existing
    latest = verify_store.latest_coordination_status(store, binding.id)
    if latest is None and status != "prepared":
        prepared = replace(
            job,
            status="prepared",
            output_sha256=None,
            output=None,
            error_code="",
            error="",
            updated_at=job.created_at,
        )
        record_coordination_status(store, prepared, actor=actor)
        latest = verify_store.latest_coordination_status(store, binding.id)
    if (
        status == "completed"
        and latest is not None
        and latest.status in {"prepared", "launching", "running"}
        and output_sha256 is not None
    ):
        # A submitted runtime row is the durable crash boundary. If the
        # process stopped after that local transition but before its Truth
        # projection, successful completion proves that exact intermediate
        # fact and may safely backfill it without another model call.
        submitted = replace(
            job,
            status="submitted",
            error_code="",
            error="",
        )
        record_coordination_status(store, submitted, actor=actor)
        latest = verify_store.latest_coordination_status(store, binding.id)
    if latest is not None:
        if latest.status == status:
            raise VerifyInvariantViolation(
                "coordination status already has different portable facts"
            )
        if status not in _TRANSITIONS[latest.status]:
            raise VerifyInvariantViolation(
                f"invalid coordination transition: {latest.status} -> {status}"
            )
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    record = CoworkCoordinationStatusEvent(
        id=new_id(),
        coordination_job_id=binding.id,
        status=status,
        outcome_kind=outcome,
        output_sha256=output_sha256,
        error_code=error_code,
        message=message,
        consequence_refs_json=canonical_json(refs),
        canonical_sha256=canonical_sha256,
        created_at=_timestamp(
            at or getattr(job, "updated_at", None) or utc_now(),
            "coordination status created_at",
        ),
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    try:
        return verify_store.insert_record(store, record)
    except sqlite3.IntegrityError:
        concurrent = verify_store.get_by_canonical_sha256(
            store,
            CoworkCoordinationStatusEvent,
            canonical_sha256,
        )
        if concurrent is None:
            raise
        return concurrent


def portable_coordination_jobs(
    store: TruthStore,
    *,
    document_id: str | None = None,
    evaluation_run_id: str | None = None,
    role: CoworkVerifyRole | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    """Project immutable bindings with their current append-only state."""

    filters: list[str] = []
    params: list[Any] = []
    if document_id is not None:
        filters.append("source.document_id = ?")
        params.append(document_id)
    if evaluation_run_id is not None:
        filters.append("source.evaluation_run_id = ?")
        params.append(evaluation_run_id)
    if role is not None:
        filters.append("source.role = ?")
        params.append(role.value)
    jobs = verify_store.list_records(
        store,
        CoworkCoordinationJob,
        where=" AND ".join(filters),
        params=tuple(params),
        conn=conn,
    )
    projected: list[dict[str, Any]] = []
    for job in jobs:
        event = verify_store.latest_coordination_status(
            store,
            job.id,
            conn=conn,
        )
        projected.append(
            {
                "job_id": job.id,
                "document_id": job.document_id,
                "evaluation_run_id": job.evaluation_run_id,
                "action_snapshot_id": job.action_snapshot_id,
                "plan_snapshot_id": job.plan_snapshot_id,
                "role": job.role,
                "parent_job_id": job.parent_job_id,
                "authorization_receipt_id": job.authorization_receipt_id,
                "context_sha256": job.context_sha256,
                "selection": json.loads(job.selection_json),
                "request_summary": json.loads(job.request_summary_json),
                "canonical_sha256": job.canonical_sha256,
                "created_at": job.created_at,
                "status": None if event is None else event.status,
                "outcome_kind": (
                    None if event is None else event.outcome_kind
                ),
                "output_sha256": (
                    None if event is None else event.output_sha256
                ),
                "error_code": None if event is None else event.error_code,
                "message": None if event is None else event.message,
                "consequence_refs": (
                    {}
                    if event is None
                    else json.loads(event.consequence_refs_json)
                ),
                "updated_at": (
                    job.created_at if event is None else event.created_at
                ),
            }
        )
    return tuple(projected)


def record_review_application(
    store: TruthStore,
    *,
    application_id: str,
    document_id: str,
    applied_proposal_ids: Sequence[str],
    committed_at: str,
    actor: Actor,
    conn: sqlite3.Connection | None = None,
) -> CoworkReviewApplication | None:
    """Record only the applied-proposal fact from one committed sitting."""

    proposal_ids = _string_list(
        applied_proposal_ids,
        "applied_proposal_ids",
    )
    if not proposal_ids:
        return None
    identifier = _id(application_id, "review application id")
    for proposal_id in proposal_ids:
        row = conn.execute(
            "SELECT document_id FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone() if conn is not None else None
        if row is None:
            with store._read_connection() as read_conn:
                row = read_conn.execute(
                    "SELECT document_id FROM proposals WHERE id = ?",
                    (proposal_id,),
                ).fetchone()
        if row is None or str(row["document_id"]) != document_id:
            raise VerifyInvariantViolation(
                "review application proposal binding is invalid"
            )
    timestamp = _timestamp(committed_at, "review application committed_at")
    payload = {
        "document_id": document_id,
        "applied_proposal_ids": proposal_ids,
        "committed_at": timestamp,
    }
    canonical_sha256 = sha256_text(canonical_json(payload))
    existing = verify_store.get_record(
        store,
        CoworkReviewApplication,
        identifier,
        conn=conn,
    )
    if existing is not None:
        if existing.canonical_sha256 != canonical_sha256:
            raise VerifyInvariantViolation(
                "review application id is already bound differently"
            )
        return existing
    actor_kind, actor_ref, actor_meta = _actor_fields(actor)
    record = CoworkReviewApplication(
        id=identifier,
        document_id=document_id,
        applied_proposal_ids_json=canonical_json(proposal_ids),
        canonical_sha256=canonical_sha256,
        committed_at=timestamp,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    return verify_store.insert_record(store, record, conn=conn)


def review_applications(
    store: TruthStore,
    *,
    document_id: str,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": record.id,
            "document_id": record.document_id,
            "applied_proposal_ids": json.loads(
                record.applied_proposal_ids_json
            ),
            "committed_at": record.committed_at,
            "canonical_sha256": record.canonical_sha256,
        }
        for record in verify_store.list_records(
            store,
            CoworkReviewApplication,
            where="source.document_id = ?",
            params=(document_id,),
            conn=conn,
        )
    )


def coordination_jobs_with_runtime_fallback(
    store: TruthStore,
    *,
    document_id: str | None = None,
    evaluation_run_id: str | None = None,
    role: CoworkVerifyRole | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    """Prefer Truth history; use sanitized runtime state for pre-v7 jobs."""

    portable = portable_coordination_jobs(
        store,
        document_id=document_id,
        evaluation_run_id=evaluation_run_id,
        role=role,
        conn=conn,
    )
    if portable:
        return portable

    # Runtime is a compatibility fallback only. Once any matching portable
    # records exist, machine-local rows are ignored so an imported store can
    # never accidentally join to stale source-machine process state.
    from work_buddy.cowork.verify_runtime import (
        jobs_for_document,
        jobs_for_run,
    )

    runtime_jobs = (
        jobs_for_run(store.store_id, evaluation_run_id)
        if evaluation_run_id is not None
        else jobs_for_document(store.store_id, document_id)
        if document_id is not None
        else ()
    )
    projected: list[dict[str, Any]] = []
    for job in runtime_jobs:
        if role is not None and job.role is not role:
            continue
        request_summary = sanitized_request_summary(job.role, job.request)
        projected.append(
            {
                "job_id": job.job_id,
                "document_id": job.document_id,
                "evaluation_run_id": (
                    None
                    if job.role is CoworkVerifyRole.COTHINK
                    else job.evaluation_run_id
                ),
                "action_snapshot_id": job.action_snapshot_id,
                "plan_snapshot_id": job.plan_snapshot_id,
                "role": job.role.value,
                "parent_job_id": job.parent_job_id,
                "authorization_receipt_id": job.authorization_receipt_id,
                "context_sha256": job.context_sha256,
                "selection": _selection_summary(job.selection),
                "request_summary": request_summary,
                "canonical_sha256": None,
                "created_at": job.created_at,
                "status": job.status,
                "outcome_kind": _infer_outcome(job, job.status),
                "output_sha256": job.output_sha256,
                "error_code": _safe_error_code(
                    job.error_code,
                    status=job.status,
                ),
                "message": _safe_message(job.status),
                "consequence_refs": {},
                "updated_at": job.updated_at,
            }
        )
    return tuple(projected)


def root_coordination_for_run(
    store: TruthStore,
    run_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Return the original whole-context binding for a Verify run."""

    return next(
        (
            item
            for item in coordination_jobs_with_runtime_fallback(
                store,
                evaluation_run_id=run_id,
                conn=conn,
            )
            if item["parent_job_id"] is None
            and item["role"] == CoworkVerifyRole.COORDINATOR.value
        ),
        None,
    )


__all__ = [
    "COORDINATION_OUTCOMES",
    "COORDINATION_STATUSES",
    "coordination_jobs_with_runtime_fallback",
    "portable_coordination_jobs",
    "record_coordination_job",
    "record_coordination_status",
    "record_review_application",
    "review_applications",
    "root_coordination_for_run",
    "sanitized_request_summary",
]
