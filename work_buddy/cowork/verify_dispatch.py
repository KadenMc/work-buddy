"""Restart-durable dispatch for account-backed Co-work Verify workers.

The Truth ledger owns the portable authorization and evaluation records.
``verify_runtime`` owns process metadata. Launch requests themselves ride the
sidecar's existing disk-backed operation queue through a non-discoverable
internal handler.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.cowork.verify import ModelCallAuthorizationReceipt
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_jobs import (
    MAX_VERIFY_JOB_BUDGET_USD,
    SpawnDetached,
    spawn_verify_job,
)
from work_buddy.cowork.verify_coordination import record_coordination_status
from work_buddy.cowork.verify_runtime import (
    VerifyRuntimeJob,
    claim_job_launch,
    get_job,
    reconcilable_jobs,
    update_job,
)
from work_buddy.sidecar.internal_operations import (
    COWORK_VERIFY_LAUNCH,
    InternalOperationError,
    InternalOperationRetry,
    enqueue_internal_operation,
    internal_operation_id,
)
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import TruthStore
from work_buddy.utils.process import is_process_alive


class VerifyDispatchError(RuntimeError):
    """The durable Verify launch binding is invalid."""


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise VerifyDispatchError("Verify dispatch timestamp is invalid") from exc
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _authorization(
    job: VerifyRuntimeJob,
    *,
    store: TruthStore | None = None,
) -> ModelCallAuthorizationReceipt:
    resolved_store = (
        TruthStoreRegistry().open_store(job.store_id)
        if store is None
        else store
    )
    if resolved_store.store_id != job.store_id:
        raise VerifyDispatchError(
            "Verify launch store does not match its runtime binding"
        )
    receipt = verify_store.get_record(
        resolved_store,
        ModelCallAuthorizationReceipt,
        job.authorization_receipt_id,
    )
    if receipt is None:
        raise VerifyDispatchError("Verify launch authorization is unavailable")
    try:
        boundary = json.loads(receipt.content_boundary_json)
    except json.JSONDecodeError as exc:
        raise VerifyDispatchError(
            "Verify launch authorization boundary is invalid"
        ) from exc
    if not isinstance(boundary, Mapping):
        raise VerifyDispatchError(
            "Verify launch authorization boundary is invalid"
        )
    expected = {
        "action_snapshot_id": job.action_snapshot_id,
        "plan_snapshot_id": job.plan_snapshot_id,
        "provider": str(job.selection.get("provider_id") or ""),
        "model": str(job.selection.get("model_id") or ""),
        "context_sha256": job.context_sha256,
        "role": job.role.value,
        "job_id": job.job_id,
    }
    observed = {
        "action_snapshot_id": receipt.action_snapshot_id,
        "plan_snapshot_id": receipt.plan_snapshot_id,
        "provider": receipt.provider,
        "model": receipt.model,
        "context_sha256": receipt.context_sha256,
        "role": boundary.get("role"),
        "job_id": boundary.get("job_id"),
    }
    if observed != expected:
        raise VerifyDispatchError(
            "Verify launch no longer matches its exact authorization"
        )
    if (
        receipt.egress_class != "account_backed_agent"
        or receipt.retry_limit != 0
        or receipt.cost_ceiling_usd < 0
        or receipt.cost_ceiling_usd > MAX_VERIFY_JOB_BUDGET_USD
    ):
        raise VerifyDispatchError(
            "Verify launch authorization exceeds the admitted execution policy"
        )
    return receipt


def _mark_unavailable(
    job: VerifyRuntimeJob,
    *,
    error_code: str,
    error: str,
    expected_launch_owner: str | None = None,
) -> VerifyRuntimeJob:
    current = get_job(job.job_id)
    if current is None:
        raise VerifyDispatchError("Verify runtime job disappeared")
    if current.status in {"completed", "submitted", "unavailable", "failed"}:
        if current.status != "completed":
            record_coordination_status(
                TruthStoreRegistry().open_store(current.store_id),
                current,
            )
        return current
    persisted = update_job(
        current.job_id,
        status="unavailable",
        error_code=error_code,
        error=error,
        expected_launch_owner=expected_launch_owner,
    )
    record_coordination_status(
        TruthStoreRegistry().open_store(persisted.store_id),
        persisted,
    )
    return persisted


def enqueue_verify_launch(
    job: VerifyRuntimeJob,
    *,
    store: TruthStore | None = None,
    originating_session_id: str | None = None,
    operations_dir: Path | None = None,
) -> dict[str, Any]:
    """Ensure one exact prepared job has a durable internal queue record."""

    receipt = _authorization(job, store=store)
    return enqueue_internal_operation(
        COWORK_VERIFY_LAUNCH,
        {"job_id": job.job_id},
        deduplication_key=job.job_id,
        authorization_expires_at=receipt.expires_at,
        originating_session_id=originating_session_id,
        lease_seconds=120,
        max_attempts=3,
        operations_dir=operations_dir,
    )


def _validate_record(
    record: Mapping[str, Any],
) -> tuple[VerifyRuntimeJob, ModelCallAuthorizationReceipt]:
    params = record.get("params")
    if not isinstance(params, Mapping) or set(params) != {"job_id"}:
        raise VerifyDispatchError("Verify launch queue parameters are invalid")
    job_id = str(params.get("job_id") or "").strip()
    if (
        not job_id
        or record.get("operation_id")
        != internal_operation_id(COWORK_VERIFY_LAUNCH, job_id)
    ):
        raise VerifyDispatchError("Verify launch queue identity is invalid")
    job = get_job(job_id)
    if job is None:
        raise VerifyDispatchError("Verify runtime job is unavailable")
    receipt = _authorization(job)
    if _utc(str(record.get("authorization_expires_at") or "")) != _utc(
        receipt.expires_at
    ):
        raise VerifyDispatchError(
            "Verify launch queue expiry does not match its authorization"
        )
    return job, receipt


def _lease(record: Mapping[str, Any]) -> tuple[str, str]:
    owner = str(record.get("lease_token") or "").strip()
    deadline = str(record.get("locked_until") or "").strip()
    if not owner or not deadline:
        raise VerifyDispatchError(
            "Verify launch requires the queue's exact execution lease"
        )
    if _utc(deadline) <= datetime.now(timezone.utc):
        raise VerifyDispatchError("Verify launch queue lease expired")
    return owner, deadline


def _running_or_terminal(job: VerifyRuntimeJob) -> dict[str, Any] | None:
    if job.status in {"completed", "submitted"}:
        return {"job_id": job.job_id, "status": job.status}
    if job.status in {"unavailable", "failed"}:
        return {
            "job_id": job.job_id,
            "status": job.status,
            "error_code": job.error_code,
        }
    if job.status != "running":
        return None
    if job.pid is not None and is_process_alive(job.pid):
        return {"job_id": job.job_id, "status": "running", "pid": job.pid}
    terminal = _mark_unavailable(
        job,
        error_code="worker_exited_before_submission",
        error="The Verify worker exited before delivering a typed result.",
    )
    return {
        "job_id": terminal.job_id,
        "status": terminal.status,
        "error_code": terminal.error_code,
    }


def dispatch_verify_launch(
    record: Mapping[str, Any],
    *,
    spawn_detached: SpawnDetached | None = None,
) -> dict[str, Any]:
    """Launch once under the queue lease, or recover without double-spawning."""

    job, receipt = _validate_record(record)
    existing = _running_or_terminal(job)
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc)
    if _utc(receipt.expires_at) <= now:
        terminal = _mark_unavailable(
            job,
            error_code="authorization_expired",
            error="The exact model-call authorization expired before dispatch.",
        )
        return {
            "job_id": terminal.job_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    owner, lease_deadline = _lease(record)
    if job.status == "launching":
        if (
            job.launch_lease_expires_at is not None
            and _utc(job.launch_lease_expires_at) > now
        ):
            raise InternalOperationRetry(
                "the exact Verify launch is already in progress"
            )
        terminal = _mark_unavailable(
            job,
            error_code="launch_outcome_unknown",
            error=(
                "The Verify host stopped before the launch outcome was "
                "durably recorded. The job was not replayed."
            ),
        )
        return {
            "job_id": terminal.job_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    launching, claimed = claim_job_launch(
        job.job_id,
        launch_owner=owner,
        lease_expires_at=lease_deadline,
    )
    if not claimed:
        raise InternalOperationRetry(
            "the exact Verify launch changed state before it could be claimed"
        )
    record_coordination_status(
        TruthStoreRegistry().open_store(launching.store_id),
        launching,
    )
    try:
        metadata = spawn_verify_job(
            store_id=launching.store_id,
            document_id=launching.document_id,
            run_id=launching.evaluation_run_id,
            job_id=launching.job_id,
            role=launching.role,
            selection=launching_selection(launching),
            max_budget_usd=min(
                receipt.cost_ceiling_usd,
                MAX_VERIFY_JOB_BUDGET_USD,
            ),
            spawn_detached=spawn_detached,
        )
    except Exception:
        terminal = _mark_unavailable(
            launching,
            error_code="launch_outcome_unknown",
            error=(
                "The selected account-backed agent did not return a durable "
                "launch receipt. The job was not replayed."
            ),
            expected_launch_owner=owner,
        )
        return {
            "job_id": terminal.job_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    if not metadata.ok:
        terminal = _mark_unavailable(
            launching,
            error_code=metadata.error_code or "coordination_unavailable",
            error=(
                metadata.error
                or "The selected account-backed agent is unavailable."
            ),
            expected_launch_owner=owner,
        )
        return {
            "job_id": terminal.job_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    current = get_job(job.job_id)
    if current is None:
        raise VerifyDispatchError("Verify runtime job disappeared after launch")
    status = (
        "running"
        if current.status in {"prepared", "launching"}
        else current.status
    )
    persisted = update_job(
        current.job_id,
        status=status,
        pid=metadata.pid,
        expected_launch_owner=owner,
    )
    record_coordination_status(
        TruthStoreRegistry().open_store(persisted.store_id),
        persisted,
    )
    return {
        "job_id": persisted.job_id,
        "status": persisted.status,
        "pid": persisted.pid,
    }


def launching_selection(job: VerifyRuntimeJob):
    """Rebuild the exact provider/model pair stored on the runtime binding."""

    from work_buddy.agent_execution.models import AgentExecutionSelection

    return AgentExecutionSelection(
        provider_id=str(job.selection.get("provider_id") or ""),
        model_id=str(job.selection.get("model_id") or ""),
        provider_label=str(job.selection.get("provider_label") or ""),
        model_label=str(job.selection.get("model_label") or ""),
    )


def _launch_expired(job: VerifyRuntimeJob, now: datetime) -> bool:
    if job.launch_lease_expires_at is None:
        return True
    try:
        return _utc(job.launch_lease_expires_at) <= now
    except VerifyDispatchError:
        return True


def reconcile_verify_launches(
    *,
    operations_dir: Path | None = None,
) -> dict[str, int]:
    """Heal prepared handoffs and fail closed on abandoned process states."""

    counts = {
        "queued": 0,
        "expired": 0,
        "launch_unknown": 0,
        "worker_exited": 0,
        "running": 0,
        "projected": 0,
        "projection_busy": 0,
        "projection_failed": 0,
    }
    now = datetime.now(timezone.utc)
    for job in reconcilable_jobs():
        if job.status == "submitted":
            try:
                from work_buddy.cowork.verify_events import (
                    emit_verify_completion_event,
                )
                from work_buddy.cowork.verify_orchestration import (
                    resume_submitted_job,
                )

                resumed = resume_submitted_job(job.job_id)
            except Exception:
                counts["projection_failed"] += 1
                continue
            if resumed is None:
                counts["projection_busy"] += 1
            else:
                emit_verify_completion_event(job, resumed)
                counts["projected"] += 1
            continue
        try:
            receipt = _authorization(job)
        except VerifyDispatchError:
            _mark_unavailable(
                job,
                error_code="authorization_invalid",
                error="The Verify launch authorization is unavailable or invalid.",
            )
            counts["expired"] += 1
            continue
        if job.status == "prepared":
            if _utc(receipt.expires_at) <= now:
                _mark_unavailable(
                    job,
                    error_code="authorization_expired",
                    error=(
                        "The exact model-call authorization expired before "
                        "dispatch."
                    ),
                )
                counts["expired"] += 1
                continue
            enqueue_verify_launch(job, operations_dir=operations_dir)
            counts["queued"] += 1
            continue
        if job.status == "launching":
            if _launch_expired(job, now):
                _mark_unavailable(
                    job,
                    error_code="launch_outcome_unknown",
                    error=(
                        "The Verify host stopped before the launch outcome was "
                        "durably recorded. The job was not replayed."
                    ),
                )
                counts["launch_unknown"] += 1
            continue
        if job.pid is not None and is_process_alive(job.pid):
            counts["running"] += 1
            continue
        _mark_unavailable(
            job,
            error_code="worker_exited_before_submission",
            error="The Verify worker exited before delivering a typed result.",
        )
        counts["worker_exited"] += 1
    return counts


def exhaust_verify_launch(
    record: Mapping[str, Any],
    *,
    error: str,
) -> None:
    """Make an exhausted queue record visible as a terminal runtime state."""

    params = record.get("params")
    if not isinstance(params, Mapping):
        return
    job = get_job(str(params.get("job_id") or ""))
    if job is None or job.status not in {"prepared", "launching"}:
        return
    _mark_unavailable(
        job,
        error_code="dispatch_exhausted",
        error=(
            "The durable Verify dispatcher exhausted before launch. "
            "No model call was replayed."
        ),
    )


__all__ = [
    "VerifyDispatchError",
    "dispatch_verify_launch",
    "enqueue_verify_launch",
    "exhaust_verify_launch",
    "launching_selection",
    "reconcile_verify_launches",
]
