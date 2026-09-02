"""Restart-durable dispatch for account-backed Co-work Truth analysis."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.cowork import truth_analysis_runtime
from work_buddy.cowork.truth_activation import (
    TruthActivationError,
    require_truth_access,
    resolve_document_truth_policy,
)
from work_buddy.cowork.truth_analysis_jobs import (
    MAX_TRUTH_ANALYSIS_BUDGET_USD,
    SpawnDetached,
    spawn_truth_analysis_job,
)
from work_buddy.cowork.verify_execution import provider_cost_control
from work_buddy.cowork.verify import ModelCallAuthorizationReceipt
from work_buddy.cowork.verify import store as verify_store
from work_buddy.sidecar.internal_operations import (
    COWORK_TRUTH_ANALYSIS_LAUNCH,
    InternalOperationRetry,
    enqueue_internal_operation,
    internal_operation_id,
)
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import TruthStore
from work_buddy.utils.process import is_process_alive


class TruthAnalysisDispatchError(RuntimeError):
    """The durable launch no longer matches its exact authorization."""

    def __init__(self, message: str, *, code: str = "dispatch_invalid") -> None:
        super().__init__(message)
        self.code = code


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise TruthAnalysisDispatchError(
            "Truth analysis dispatch timestamp is invalid"
        ) from exc
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _authorization(
    run: truth_analysis_runtime.TruthAnalysisRuntimeRun,
    *,
    store: TruthStore | None = None,
) -> ModelCallAuthorizationReceipt:
    resolved_store = store or TruthStoreRegistry().open_store(run.store_id)
    if resolved_store.store_id != run.store_id:
        raise TruthAnalysisDispatchError("Truth analysis store binding changed")
    receipt = verify_store.get_record(
        resolved_store,
        ModelCallAuthorizationReceipt,
        run.authorization_receipt_id,
    )
    if receipt is None:
        raise TruthAnalysisDispatchError("Truth analysis authorization is unavailable")
    try:
        boundary = json.loads(receipt.content_boundary_json)
    except json.JSONDecodeError as exc:
        raise TruthAnalysisDispatchError(
            "Truth analysis authorization boundary is invalid"
        ) from exc
    if not isinstance(boundary, Mapping):
        raise TruthAnalysisDispatchError(
            "Truth analysis authorization boundary is invalid"
        )
    expected = {
        "action_snapshot_id": run.action_snapshot_id,
        "provider": str(run.selection.get("provider_id") or ""),
        "model": str(run.selection.get("model_id") or ""),
        "context_sha256": run.context_sha256,
        "role": "truth_analysis",
        "run_id": run.run_id,
        "truth_activation_revision": run.activation_revision,
    }
    observed = {
        "action_snapshot_id": receipt.action_snapshot_id,
        "provider": receipt.provider,
        "model": receipt.model,
        "context_sha256": receipt.context_sha256,
        "role": boundary.get("role"),
        "run_id": boundary.get("run_id"),
        "truth_activation_revision": boundary.get("truth_activation_revision"),
    }
    if observed != expected:
        raise TruthAnalysisDispatchError(
            "Truth analysis launch no longer matches its authorization"
        )
    cost_control = provider_cost_control(receipt.provider)
    if (
        cost_control.enforcement_class != "hard_ceiling"
        or cost_control.ceiling_usd_per_worker_session is None
    ):
        raise TruthAnalysisDispatchError(
            "Truth analysis requires a provider-enforced hard spending ceiling.",
            code="analysis_provider_cost_control_unavailable",
        )
    if (
        receipt.plan_snapshot_id is not None
        or receipt.egress_class != "account_backed_agent"
        or receipt.retry_limit != 0
        or receipt.cost_ceiling_usd <= 0
        or receipt.cost_ceiling_usd > MAX_TRUTH_ANALYSIS_BUDGET_USD
        or receipt.cost_ceiling_usd
        > cost_control.ceiling_usd_per_worker_session
    ):
        raise TruthAnalysisDispatchError(
            "Truth analysis authorization exceeds execution policy"
        )
    return receipt


def _terminate_expired_worker(
    run: truth_analysis_runtime.TruthAnalysisRuntimeRun,
) -> bool:
    if run.pid is None:
        return False
    try:
        from work_buddy.sidecar.dispatch.executor import terminate_detached_process

        return terminate_detached_process(run.pid, owner_token=run.session_id)
    except Exception:  # noqa: BLE001 - deadline remains fenced if cleanup fails
        return False


def _mark_unavailable(
    run: truth_analysis_runtime.TruthAnalysisRuntimeRun,
    *,
    error_code: str,
    error: str,
    expected_launch_owner: str | None = None,
) -> truth_analysis_runtime.TruthAnalysisRuntimeRun:
    current = truth_analysis_runtime.get_run(run.run_id)
    if current is None:
        raise TruthAnalysisDispatchError("Truth analysis runtime disappeared")
    if current.status in {"completed", "unavailable", "failed"}:
        return current
    return truth_analysis_runtime.update_run(
        current.run_id,
        status="unavailable",
        error_code=error_code,
        error=error,
        expected_launch_owner=expected_launch_owner,
    )


def cancel_truth_analysis_runs_for_activation(
    *,
    store_id: str,
    document_id: str,
    valid_activation_revision: int | None,
) -> dict[str, int]:
    """Persistently fence and best-effort terminate runs from an old policy epoch."""

    invalidated = truth_analysis_runtime.invalidate_active_runs_for_document(
        store_id,
        document_id,
        valid_activation_revision=valid_activation_revision,
    )
    terminated = sum(1 for run in invalidated if _terminate_expired_worker(run))
    return {"invalidated": len(invalidated), "terminated": terminated}


def enqueue_truth_analysis_launch(
    run: truth_analysis_runtime.TruthAnalysisRuntimeRun,
    *,
    store: TruthStore | None = None,
    originating_session_id: str | None = None,
    operations_dir: Path | None = None,
) -> dict[str, Any]:
    receipt = _authorization(run, store=store)
    return enqueue_internal_operation(
        COWORK_TRUTH_ANALYSIS_LAUNCH,
        {"run_id": run.run_id},
        deduplication_key=run.run_id,
        authorization_expires_at=receipt.expires_at,
        originating_session_id=originating_session_id,
        lease_seconds=120,
        max_attempts=3,
        operations_dir=operations_dir,
    )


def _validate_record(
    record: Mapping[str, Any],
) -> tuple[
    truth_analysis_runtime.TruthAnalysisRuntimeRun,
    ModelCallAuthorizationReceipt,
]:
    params = record.get("params")
    if not isinstance(params, Mapping) or set(params) != {"run_id"}:
        raise TruthAnalysisDispatchError("Truth analysis queue parameters are invalid")
    run_id = str(params.get("run_id") or "").strip()
    if (
        not run_id
        or record.get("operation_id")
        != internal_operation_id(COWORK_TRUTH_ANALYSIS_LAUNCH, run_id)
    ):
        raise TruthAnalysisDispatchError("Truth analysis queue identity is invalid")
    run = truth_analysis_runtime.get_run(run_id)
    if run is None:
        raise TruthAnalysisDispatchError("Truth analysis runtime is unavailable")
    receipt = _authorization(run)
    if _utc(str(record.get("authorization_expires_at") or "")) != _utc(
        receipt.expires_at
    ):
        raise TruthAnalysisDispatchError(
            "Truth analysis queue expiry does not match authorization"
        )
    return run, receipt


def _lease(record: Mapping[str, Any]) -> tuple[str, str]:
    owner = str(record.get("lease_token") or "").strip()
    deadline = str(record.get("locked_until") or "").strip()
    if not owner or not deadline:
        raise TruthAnalysisDispatchError(
            "Truth analysis launch requires the queue execution lease"
        )
    if _utc(deadline) <= datetime.now(timezone.utc):
        raise TruthAnalysisDispatchError("Truth analysis launch lease expired")
    return owner, deadline


def _selection(
    run: truth_analysis_runtime.TruthAnalysisRuntimeRun,
) -> AgentExecutionSelection:
    return AgentExecutionSelection(
        provider_id=str(run.selection.get("provider_id") or ""),
        model_id=str(run.selection.get("model_id") or ""),
        provider_label=str(run.selection.get("provider_label") or ""),
        model_label=str(run.selection.get("model_label") or ""),
    )


def _running_or_terminal(
    run: truth_analysis_runtime.TruthAnalysisRuntimeRun,
) -> dict[str, Any] | None:
    if run.status == "completed":
        return {"run_id": run.run_id, "status": "completed"}
    if run.status in {"unavailable", "failed"}:
        if run.error_code == "execution_deadline_exceeded":
            _terminate_expired_worker(run)
        return {
            "run_id": run.run_id,
            "status": run.status,
            "error_code": run.error_code,
        }
    if run.status != "running":
        return None
    if run.pid is not None and is_process_alive(run.pid):
        return {"run_id": run.run_id, "status": "running", "pid": run.pid}
    terminal = _mark_unavailable(
        run,
        error_code="worker_exited_before_submission",
        error="The Truth analysis worker exited without a typed result.",
    )
    return {
        "run_id": terminal.run_id,
        "status": terminal.status,
        "error_code": terminal.error_code,
    }


def dispatch_truth_analysis_launch(
    record: Mapping[str, Any],
    *,
    spawn_detached: SpawnDetached | None = None,
) -> dict[str, Any]:
    try:
        run, receipt = _validate_record(record)
    except TruthAnalysisDispatchError as exc:
        if exc.code != "analysis_provider_cost_control_unavailable":
            raise
        params = record.get("params")
        run_id = str(params.get("run_id") or "") if isinstance(params, Mapping) else ""
        unsafe = truth_analysis_runtime.get_run(run_id)
        if unsafe is None:
            raise
        terminal = _mark_unavailable(
            unsafe,
            error_code=exc.code,
            error=str(exc),
        )
        return {
            "run_id": terminal.run_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    existing = _running_or_terminal(run)
    if existing is not None:
        return existing
    try:
        if run.activation_revision <= 0:
            raise TruthActivationError(
                "truth_activation_changed",
                "The analysis run has no document Truth activation binding.",
            )
        require_truth_access(
            TruthStoreRegistry().open_store(run.store_id),
            run.document_id,
            mutation=True,
            expected_activation_revision=run.activation_revision,
        )
    except TruthActivationError as exc:
        terminal = _mark_unavailable(
            run,
            error_code="truth_activation_changed",
            error=str(exc),
        )
        return {
            "run_id": terminal.run_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    now = datetime.now(timezone.utc)
    if _utc(receipt.expires_at) <= now:
        terminal = _mark_unavailable(
            run,
            error_code="authorization_expired",
            error="The exact model-call authorization expired before dispatch.",
        )
        return {
            "run_id": terminal.run_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    owner, lease_deadline = _lease(record)
    if run.status == "launching":
        if (
            run.launch_lease_expires_at is not None
            and _utc(run.launch_lease_expires_at) > now
        ):
            raise InternalOperationRetry(
                "the exact Truth analysis launch is already in progress"
            )
        terminal = _mark_unavailable(
            run,
            error_code="launch_outcome_unknown",
            error=(
                "The host stopped before the Truth analysis launch outcome "
                "was recorded. The model call was not replayed."
            ),
        )
        return {
            "run_id": terminal.run_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    launching, claimed = truth_analysis_runtime.claim_run_launch(
        run.run_id,
        launch_owner=owner,
        lease_expires_at=lease_deadline,
    )
    if not claimed:
        raise InternalOperationRetry(
            "the exact Truth analysis launch changed before it was claimed"
        )
    try:
        metadata = spawn_truth_analysis_job(
            store_id=launching.store_id,
            document_id=launching.document_id,
            run_id=launching.run_id,
            selection=_selection(launching),
            max_budget_usd=min(
                receipt.cost_ceiling_usd,
                MAX_TRUTH_ANALYSIS_BUDGET_USD,
            ),
            spawn_detached=spawn_detached,
        )
    except Exception:
        terminal = _mark_unavailable(
            launching,
            error_code="launch_outcome_unknown",
            error=(
                "The selected account-backed agent returned no durable launch "
                "receipt. The model call was not replayed."
            ),
            expected_launch_owner=owner,
        )
        return {
            "run_id": terminal.run_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    if not metadata.ok:
        terminal = _mark_unavailable(
            launching,
            error_code=metadata.error_code or "coordination_unavailable",
            error=metadata.error or "The selected account-backed agent is unavailable.",
            expected_launch_owner=owner,
        )
        return {
            "run_id": terminal.run_id,
            "status": terminal.status,
            "error_code": terminal.error_code,
        }
    current = truth_analysis_runtime.get_run(run.run_id)
    if current is None:
        raise TruthAnalysisDispatchError("Truth analysis runtime disappeared after launch")
    status = "running" if current.status in {"prepared", "launching"} else current.status
    persisted = truth_analysis_runtime.update_run(
        current.run_id,
        status=status,
        pid=metadata.pid,
        expected_launch_owner=owner,
    )
    return {
        "run_id": persisted.run_id,
        "status": persisted.status,
        "pid": persisted.pid,
    }


def reconcile_truth_analysis_launches(
    *,
    operations_dir: Path | None = None,
) -> dict[str, int]:
    counts = {
        "queued": 0,
        "expired": 0,
        "launch_unknown": 0,
        "worker_exited": 0,
        "running": 0,
        "deadline_exceeded": 0,
        "terminated": 0,
        "activation_changed": 0,
    }
    now = datetime.now(timezone.utc)
    for run in truth_analysis_runtime.reconcilable_runs():
        store = TruthStoreRegistry().open_store(run.store_id)
        try:
            require_truth_access(
                store,
                run.document_id,
                mutation=True,
                expected_activation_revision=run.activation_revision,
            )
        except TruthActivationError:
            try:
                policy = resolve_document_truth_policy(store, run.document_id)
                valid_revision = (
                    policy.activation_revision if policy.truth_mutable else None
                )
            except TruthActivationError:
                valid_revision = None
            cancelled = cancel_truth_analysis_runs_for_activation(
                store_id=run.store_id,
                document_id=run.document_id,
                valid_activation_revision=valid_revision,
            )
            counts["activation_changed"] += cancelled["invalidated"]
            counts["terminated"] += cancelled["terminated"]
            continue
        expired, did_expire = truth_analysis_runtime.expire_run_if_overdue(
            run.run_id
        )
        if did_expire:
            counts["deadline_exceeded"] += 1
            if expired is not None and _terminate_expired_worker(expired):
                counts["terminated"] += 1
            continue
        try:
            receipt = _authorization(run)
        except TruthAnalysisDispatchError as exc:
            _mark_unavailable(
                run,
                error_code=exc.code,
                error=str(exc),
            )
            counts["expired"] += 1
            continue
        if run.status == "prepared":
            if _utc(receipt.expires_at) <= now:
                _mark_unavailable(
                    run,
                    error_code="authorization_expired",
                    error="The exact model-call authorization expired before dispatch.",
                )
                counts["expired"] += 1
            else:
                enqueue_truth_analysis_launch(run, operations_dir=operations_dir)
                counts["queued"] += 1
            continue
        if run.status == "launching":
            if (
                run.launch_lease_expires_at is None
                or _utc(run.launch_lease_expires_at) <= now
            ):
                _mark_unavailable(
                    run,
                    error_code="launch_outcome_unknown",
                    error=(
                        "The host stopped before the Truth analysis launch "
                        "outcome was recorded. The model call was not replayed."
                    ),
                )
                counts["launch_unknown"] += 1
            continue
        if run.pid is not None and is_process_alive(run.pid):
            counts["running"] += 1
        else:
            _mark_unavailable(
                run,
                error_code="worker_exited_before_submission",
                error="The Truth analysis worker exited without a typed result.",
            )
            counts["worker_exited"] += 1
    return counts


def exhaust_truth_analysis_launch(
    record: Mapping[str, Any],
    *,
    error: str,
) -> None:
    del error
    params = record.get("params")
    if not isinstance(params, Mapping):
        return
    run = truth_analysis_runtime.get_run(str(params.get("run_id") or ""))
    if run is None or run.status not in {"prepared", "launching"}:
        return
    _mark_unavailable(
        run,
        error_code="dispatch_exhausted",
        error=(
            "The durable Truth analysis dispatcher exhausted before launch. "
            "No model call was replayed."
        ),
    )


__all__ = [
    "TruthAnalysisDispatchError",
    "dispatch_truth_analysis_launch",
    "enqueue_truth_analysis_launch",
    "exhaust_truth_analysis_launch",
    "reconcile_truth_analysis_launches",
]
