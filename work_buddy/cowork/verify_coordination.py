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
    CoworkCoordinationJob,
    CoworkCoordinationStatusEvent,
    CoworkReviewApplication,
    EvaluationPlanSnapshot,
    EvaluationRun,
    ModelCallAuthorizationReceipt,
    VerifyInvariantViolation,
)
from work_buddy.cowork.verify import store as verify_store
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
    }
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
    parent_id = job.parent_job_id
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
    ):
        raise VerifyInvariantViolation(
            "coordination selection does not match its authorization"
        )
    request_summary = sanitized_request_summary(role, job.request)
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
    return allowed


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
