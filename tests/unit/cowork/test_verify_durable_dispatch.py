"""Restart, fencing, and queue invariants for Co-work Verify launches."""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from work_buddy.cowork import (
    sitting_lifecycle,
    verify_dispatch,
    verify_orchestration,
    verify_rechecks,
    verify_runtime,
)
from work_buddy.cowork.execution_identity import (
    CoworkVerifyRole,
    cowork_verify_job_session_id,
)
from work_buddy.cowork.verify_configuration import (
    create_user_verification_check,
)
from work_buddy.cowork.verify_coordination import portable_coordination_jobs
from work_buddy.cowork.verify_dispatch import (
    dispatch_verify_launch,
    reconcile_verify_launches,
)
from work_buddy.cowork.verify_jobs import (
    VerifyJobBinding,
    VerifyJobSpawnMetadata,
)
from work_buddy.cowork.verify_orchestration import (
    VerifyOrchestrationError,
    get_worker_job,
    resume_submitted_job,
    run_status_projection,
    start_verify_run,
    submit_worker_job,
)
from work_buddy.cowork.verify_runtime import (
    claim_job_launch,
    get_job,
    jobs_for_run,
    update_job,
)
from work_buddy.cowork.verify import (
    ActionSnapshot,
    EvaluationResult,
    ModelCallAuthorizationReceipt,
    record_result_relation,
)
from work_buddy.sidecar import internal_operations, retry_sweep
from work_buddy.sidecar.internal_operations import (
    COWORK_VERIFY_LAUNCH,
    InternalOperationRetry,
    enqueue_internal_operation,
    internal_operation_id,
)
from work_buddy.sidecar.retry_sweep import RetrySweep
from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.identity import canonical_json, sha256_bytes, sha256_text
from work_buddy.truth.export import export_store, import_store

from .conftest import HUMAN
from .test_verify_orchestration import AGENT, BODY, SELECTION, _capture


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str):
        return ()


@pytest.fixture
def durable_dispatch_ctx(
    store_ctx: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    runtime_path = tmp_path / "runtime" / "cowork-verify-jobs.db"
    operations_path = tmp_path / "operations"
    operations_path.mkdir()
    monkeypatch.setattr(verify_runtime, "_DB_PATH", runtime_path)
    monkeypatch.setattr(
        verify_orchestration,
        "TruthStoreRegistry",
        lambda: store_ctx["registry"],
    )
    monkeypatch.setattr(
        verify_dispatch,
        "TruthStoreRegistry",
        lambda: store_ctx["registry"],
    )
    monkeypatch.setattr(
        internal_operations,
        "_operations_dir",
        lambda: operations_path,
    )
    monkeypatch.setattr(
        retry_sweep,
        "_get_operations_dir",
        lambda: operations_path,
    )
    return {
        **store_ctx,
        "runtime_path": runtime_path,
        "operations_path": operations_path,
    }


def _start(
    ctx: dict[str, Any],
    *,
    target_text: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    capture = _capture(ctx, target_text=target_text)
    if target_text is not None:
        capture["target"]["targetReference"] = {
            "schema": "wb.cowork.document-target/v1",
            "storeId": capture["storeId"],
            "documentId": capture["documentId"],
            "kind": "text_range",
            "relative": {
                "startBase64": "AA==",
                "endBase64": "AQ==",
            },
            "quote": {
                "exact": target_text,
                "prefix": "",
                "suffix": "",
            },
            "label": "Working on",
            "headingPath": [],
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
    started = start_verify_run(
        ctx["store"],
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Use established terminology.",
        protected_intent="Preserve the author's substantive meaning.",
        validate_selection=lambda selection: selection,
    )
    return capture, started


def _spawn_result(
    *,
    store_id: str,
    document_id: str,
    run_id: str,
    job_id: str,
    role,
    selection,
    pid: int = 987_654,
    **_kwargs: Any,
) -> VerifyJobSpawnMetadata:
    return VerifyJobSpawnMetadata(
        status="ok",
        binding=VerifyJobBinding(
            store_id=store_id,
            document_id=document_id,
            run_id=run_id,
            job_id=job_id,
            role=role,
        ),
        session_id=cowork_verify_job_session_id(job_id, role),
        selection=selection,
        pid=pid,
    )


def _operation(ctx: dict[str, Any], job_id: str) -> tuple[Path, dict[str, Any]]:
    op_id = internal_operation_id(COWORK_VERIFY_LAUNCH, job_id)
    path = ctx["operations_path"] / f"{op_id}.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_production_start_uses_non_capability_internal_queue(
    durable_dispatch_ctx: dict[str, Any],
):
    _capture_value, started = _start(durable_dispatch_ctx)
    job = get_job(started["job_id"])
    assert job is not None and job.status == "prepared"
    path, record = _operation(durable_dispatch_ctx, job.job_id)
    assert path.is_file()
    assert record["type"] == "internal"
    assert record["name"] == COWORK_VERIFY_LAUNCH
    assert record["params"] == {"job_id": job.job_id}
    assert record["queued"] is True
    assert started["coordination_status"] == "pending"


def test_missing_handoff_is_recreated_from_prepared_job(
    durable_dispatch_ctx: dict[str, Any],
):
    _capture_value, started = _start(durable_dispatch_ctx)
    path, _record = _operation(durable_dispatch_ctx, started["job_id"])
    path.unlink()

    result = reconcile_verify_launches(
        operations_dir=durable_dispatch_ctx["operations_path"]
    )

    assert result["queued"] == 1
    assert path.is_file()
    assert get_job(started["job_id"]).status == "prepared"


def test_specialist_handoff_crash_completes_parent_before_recovering_exact_child(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    store = durable_dispatch_ctx["store"]
    capture = _capture(durable_dispatch_ctx)
    for title in ("Positive framing", "Reader clarity"):
        create_user_verification_check(
            store,
            document_id=capture["documentId"],
            title=title,
            description=f"Evaluate {title.lower()}.",
            evaluation_instructions=f"Evaluate the target for {title.lower()}.",
            actor=HUMAN,
        )
    started = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Run the selected checks, then reconcile their results.",
        protected_intent="Preserve the author's substantive meaning.",
        validate_selection=lambda selection: selection,
    )
    first = get_job(started["job_id"])
    assert first is not None
    assert first.role is CoworkVerifyRole.SPECIALIST
    assert first.status == "prepared"

    spawn_calls: list[str] = []

    def spawn(**kwargs: Any) -> VerifyJobSpawnMetadata:
        spawn_calls.append(str(kwargs["job_id"]))
        return _spawn_result(**kwargs)

    monkeypatch.setattr(verify_dispatch, "spawn_verify_job", spawn)
    launched = RetrySweep(reconcile_internal=True).sweep()
    first = get_job(first.job_id)
    assert len(launched) == 1 and launched[0]["success"] is True
    assert first is not None and first.status == "running"
    assert spawn_calls == [first.job_id]

    original_enqueue = verify_dispatch.enqueue_verify_launch

    def crash_before_enqueue(*_args: Any, **_kwargs: Any):
        raise RuntimeError("simulated handoff enqueue crash")

    monkeypatch.setattr(
        verify_dispatch,
        "enqueue_verify_launch",
        crash_before_enqueue,
    )
    payload = {
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
    with pytest.raises(RuntimeError, match="simulated handoff enqueue crash"):
        submit_worker_job(
            job_id=first.job_id,
            payload=payload,
            agent_session_id=first.session_id,
        )

    parent = get_job(first.job_id)
    run_jobs = jobs_for_run(store.store_id, started["run_id"])
    assert parent is not None and parent.status == "completed"
    assert len(run_jobs) == 2
    child = next(job for job in run_jobs if job.job_id != first.job_id)
    assert child.role is CoworkVerifyRole.SPECIALIST
    assert child.parent_job_id == first.job_id
    assert child.status == "prepared"
    portable_parent = next(
        item
        for item in portable_coordination_jobs(
            store,
            evaluation_run_id=started["run_id"],
        )
        if item["job_id"] == first.job_id
    )
    assert portable_parent["status"] == "completed"
    assert portable_parent["consequence_refs"]["next_job_id"] == child.job_id

    replay = resume_submitted_job(first.job_id)
    assert replay is not None
    assert replay["status"] == "completed"
    assert replay["replayed"] is True
    assert replay["next_job_id"] == child.job_id
    assert len(jobs_for_run(store.store_id, started["run_id"])) == 2
    assert spawn_calls == [first.job_id]

    monkeypatch.setattr(
        verify_dispatch,
        "enqueue_verify_launch",
        original_enqueue,
    )
    reconciled = reconcile_verify_launches(
        operations_dir=durable_dispatch_ctx["operations_path"]
    )
    assert reconciled["queued"] == 1
    child_path, child_record = _operation(
        durable_dispatch_ctx,
        child.job_id,
    )
    assert child_path.is_file()
    assert child_record["params"] == {"job_id": child.job_id}

    launched_child = RetrySweep(reconcile_internal=True).sweep()
    recovered = get_job(child.job_id)
    assert len(launched_child) == 1
    assert launched_child[0]["success"] is True
    assert recovered is not None and recovered.status == "running"
    assert spawn_calls == [first.job_id, child.job_id]
    summary = next(
        item
        for item in run_status_projection(
            store,
            document_id=capture["documentId"],
        )
        if item["run_id"] == started["run_id"]
    )
    assert summary["status"] == "running"
    assert summary["status"] != "completed_with_failures"


def test_sidecar_recovers_prepared_job_when_operations_directory_is_missing(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    _capture_value, started = _start(durable_dispatch_ctx)
    path, _record = _operation(durable_dispatch_ctx, started["job_id"])
    path.unlink()
    durable_dispatch_ctx["operations_path"].rmdir()
    calls: list[str] = []

    def spawn(**kwargs: Any) -> VerifyJobSpawnMetadata:
        calls.append(str(kwargs["job_id"]))
        return _spawn_result(**kwargs)

    monkeypatch.setattr(verify_dispatch, "spawn_verify_job", spawn)

    outcomes = RetrySweep(reconcile_internal=True).sweep()

    recovered = get_job(started["job_id"])
    assert recovered is not None and recovered.status == "running"
    assert calls == [started["job_id"]]
    assert len(outcomes) == 1 and outcomes[0]["success"] is True


def test_restart_projects_submitted_output_without_another_model_call(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    _capture_value, started = _start(durable_dispatch_ctx)
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
    calls: list[str] = []
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        verify_dispatch,
        "spawn_verify_job",
        lambda **kwargs: calls.append(str(kwargs["job_id"])),
    )
    monkeypatch.setattr(
        "work_buddy.cowork.verify_events.emit_verify_completion_event",
        lambda job, result: events.append(
            (str(job.job_id), str(result["status"]))
        ),
    )

    first = reconcile_verify_launches(
        operations_dir=durable_dispatch_ctx["operations_path"]
    )
    second = reconcile_verify_launches(
        operations_dir=durable_dispatch_ctx["operations_path"]
    )

    completed = get_job(coordinator.job_id)
    assert completed is not None and completed.status == "completed"
    assert first["projected"] == 1
    assert second["projected"] == 0
    assert calls == []
    assert events == [(coordinator.job_id, "completed")]


def test_sidecar_sweep_launches_once_and_restart_detects_dead_worker(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    _capture_value, started = _start(durable_dispatch_ctx)
    calls: list[str] = []

    def spawn(**kwargs: Any) -> VerifyJobSpawnMetadata:
        calls.append(str(kwargs["job_id"]))
        return _spawn_result(**kwargs)

    monkeypatch.setattr(verify_dispatch, "spawn_verify_job", spawn)
    first = RetrySweep(reconcile_internal=True).sweep()

    running = get_job(started["job_id"])
    assert len(first) == 1 and first[0]["success"] is True
    assert running is not None and running.status == "running"
    assert calls == [running.job_id]

    monkeypatch.setattr(verify_dispatch, "is_process_alive", lambda _pid: False)
    second = RetrySweep(reconcile_internal=True).sweep()

    unavailable = get_job(running.job_id)
    assert second == []
    assert unavailable is not None
    assert unavailable.status == "unavailable"
    assert unavailable.error_code == "worker_exited_before_submission"
    assert calls == [running.job_id]


def test_restart_fails_closed_when_launch_outcome_was_not_recorded(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    _capture_value, started = _start(durable_dispatch_ctx)
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    launching, claimed = claim_job_launch(
        started["job_id"],
        launch_owner="dead-sidecar-lease",
        at=old.isoformat(),
        lease_expires_at=(old + timedelta(minutes=1)).isoformat(),
    )
    assert claimed is True and launching.status == "launching"
    calls: list[str] = []
    monkeypatch.setattr(
        verify_dispatch,
        "spawn_verify_job",
        lambda **kwargs: calls.append(str(kwargs["job_id"])),
    )

    outcomes = RetrySweep(reconcile_internal=True).sweep()

    terminal = get_job(started["job_id"])
    assert terminal is not None
    assert terminal.status == "unavailable"
    assert terminal.error_code == "launch_outcome_unknown"
    assert calls == []
    assert len(outcomes) == 1 and outcomes[0]["success"] is True


def test_concurrent_duplicate_dispatch_cannot_spawn_twice(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    _capture_value, started = _start(durable_dispatch_ctx)
    _path, record = _operation(durable_dispatch_ctx, started["job_id"])
    record["lease_token"] = "live-sidecar-lease"
    record["locked_until"] = (
        datetime.now(timezone.utc) + timedelta(minutes=2)
    ).isoformat()
    record["status"] = "running"
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    first_result: list[dict[str, Any]] = []

    def spawn(**kwargs: Any) -> VerifyJobSpawnMetadata:
        calls.append(str(kwargs["job_id"]))
        entered.set()
        assert release.wait(timeout=5)
        return _spawn_result(**kwargs)

    monkeypatch.setattr(verify_dispatch, "spawn_verify_job", spawn)

    def first() -> None:
        first_result.append(
            dispatch_verify_launch(record, spawn_detached=lambda _request: None)
        )

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(timeout=5)
    with pytest.raises(InternalOperationRetry, match="already in progress"):
        dispatch_verify_launch(record, spawn_detached=lambda _request: None)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(first_result) == 1
    assert calls == [started["job_id"]]
    assert get_job(started["job_id"]).status == "running"


def test_expired_authorization_is_terminal_without_model_call(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    _capture_value, started = _start(durable_dispatch_ctx)
    job = get_job(started["job_id"])
    assert job is not None
    real_authorization = verify_dispatch._authorization(job)
    expired = replace(
        real_authorization,
        expires_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
    )
    monkeypatch.setattr(
        verify_dispatch,
        "_authorization",
        lambda _job: expired,
    )

    result = reconcile_verify_launches(
        operations_dir=durable_dispatch_ctx["operations_path"]
    )

    terminal = get_job(job.job_id)
    assert result["expired"] == 1
    assert terminal is not None
    assert terminal.status == "unavailable"
    assert terminal.error_code == "authorization_expired"


def test_concurrent_queue_sweeps_share_one_atomic_lease(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat()
    enqueue_internal_operation(
        COWORK_VERIFY_LAUNCH,
        {"job_id": "queue-lease-fixture"},
        deduplication_key="queue-lease-fixture",
        authorization_expires_at=expires_at,
    )
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def execute(record: dict[str, Any]) -> dict[str, Any]:
        calls.append(str(record["operation_id"]))
        entered.set()
        assert release.wait(timeout=5)
        return {"status": "ok"}

    monkeypatch.setattr(
        internal_operations,
        "execute_internal_operation",
        execute,
    )
    results: list[list[dict[str, Any]]] = []

    def run_sweep() -> None:
        results.append(RetrySweep().sweep())

    first = threading.Thread(target=run_sweep)
    second = threading.Thread(target=run_sweep)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    second.join(timeout=5)
    release.set()
    first.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1
    assert sorted(len(result) for result in results) == [0, 1]


def test_committed_verify_proposal_derives_capture_gated_recheck_intent(
    durable_dispatch_ctx: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    capture, started = _start(
        durable_dispatch_ctx,
        target_text="Co-work scope",
    )
    store = durable_dispatch_ctx["store"]
    source_results = verify_rechecks.verify_store.list_records(
        store,
        EvaluationResult,
        where="source.evaluation_run_id = ?",
        params=(started["run_id"],),
    )
    assert len(source_results) == 1
    offset = BODY.index("Co-work scope")
    proposal = proposals.propose_edit(
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
        rationale="Use the audited preferred term.",
        tldr="Replace the non-preferred label.",
        actor=AGENT,
    )
    record_result_relation(
        store,
        evaluation_result_id=source_results[0].id,
        relation_kind="addresses",
        target_kind="proposal",
        target_ref=proposal.id,
        actor=AGENT,
    )
    sitting, created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=capture["documentId"],
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=capture["projectionSha256"],
        expected_structured_head_sha256=capture[
            "structuredHeadSha256"
        ],
        idempotency_key="durable-verify-recheck-0001",
    )
    assert created is True
    rendered = BODY.replace("Co-work scope", "document target")
    snapshot = b"YDOC-DURABLE-VERIFY-RECHECK:" + rendered.encode("utf-8")
    receipt, _events = sitting_lifecycle.commit_sitting(
        store,
        document_id=capture["documentId"],
        intent_id=sitting.id,
        actor=HUMAN,
        snapshot=snapshot,
        snapshot_sha256=sha256_bytes(snapshot),
        rendered_markdown=rendered,
        rendered_sha256=sha256_bytes(rendered.encode("utf-8")),
    )
    assert receipt["results"][0]["result"] == "applied"

    intents = verify_rechecks.verification_recheck_intents(
        store,
        document_id=capture["documentId"],
    )

    assert len(intents) == 1
    intent = intents[0]
    assert intent.sitting_id == sitting.id
    assert intent.source_run_id == started["run_id"]
    assert intent.proposal_ids == (proposal.id,)
    assert intent.pending_proposal_ids == (proposal.id,)
    assert intent.status == "pending_capture"
    assert intent.original_target_source == "working_target"
    assert intent.original_target_kind == "text_quote"
    assert intent.provider_id == SELECTION.provider_id
    assert intent.model_id == SELECTION.model_id
    projected = intent.to_dict()
    assert projected["user_goal"] == "Use established terminology."
    assert (
        projected["protected_intent"]
        == "Preserve the author's substantive meaning."
    )
    assert projected["original_action_target"]["source"] == "working_target"
    assert projected["requires"]["fresh_action_snapshot"] is True
    assert projected["requires"]["fresh_model_call_authorization"] is True
    assert projected["requires"]["allow_widen_to_whole_document"] is False
    assert (
        projected["original_request"]["user_goal"]
        == "Use established terminology."
    )
    assert (
        projected["original_request"]["protected_intent"]
        == "Preserve the author's substantive meaning."
    )
    assert projected["original_request"]["effective_configuration"]
    assert (
        verify_rechecks.validate_recheck_intent(
            store,
            document_id=capture["documentId"],
            intent_id=intent.id,
            source_run_id=started["run_id"],
            proposal_ids=[proposal.id],
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
        )
        == intent
    )
    with pytest.raises(
        verify_rechecks.VerifyRecheckIntentError,
        match="proposal binding changed",
    ):
        verify_rechecks.validate_recheck_intent(
            store,
            document_id=capture["documentId"],
            intent_id=intent.id,
            source_run_id=started["run_id"],
            proposal_ids=[],
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
        )

    refreshed_document = documents.get_document(
        store,
        capture["documentId"],
    )
    refreshed_head = ydoc_store.current_structured_head(
        store,
        document_id=refreshed_document.id,
        snapshot_sha256=refreshed_document.ydoc_snapshot_sha256,
    )
    refreshed_generation = documents.current_ydoc_generation(
        store,
        refreshed_document.id,
    )
    state_vector = b"durable-recheck-state-vector"
    refreshed_base = {
        **capture,
        "captureId": "durable-recheck-fresh-capture",
        # Client wall-clock time is telemetry only. A backdated browser clock
        # must not make an otherwise fresh server-ledger capture stale.
        "capturedAt": "1900-01-01T00:00:00.000+00:00",
        "ydocGenerationSha256": refreshed_generation,
        "snapshotBase64": base64.b64encode(snapshot).decode("ascii"),
        "snapshotSha256": sha256_bytes(snapshot),
        "stateVectorBase64": base64.b64encode(state_vector).decode("ascii"),
        "stateVectorSha256": sha256_bytes(state_vector),
        "structuredHeadSha256": refreshed_head,
        "projectionMarkdown": rendered,
        "projectionSha256": sha256_bytes(rendered.encode("utf-8")),
    }
    widened_capture = {
        **refreshed_base,
        "target": {
            "source": "whole_document",
            "label": "Whole document",
            "wordCount": len(rendered.split()),
            "proseMirrorRange": None,
            "selector": {"kind": "document"},
            "targetTextSha256": sha256_bytes(rendered.encode("utf-8")),
        },
    }
    with pytest.raises(
        verify_rechecks.VerifyRecheckIntentError,
        match="widening to another target is not allowed",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=widened_capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            recheck_intent_id=intent.id,
            validate_selection=lambda selection: selection,
        )

    start = rendered.index("document target")
    resolved_text = rendered[start : start + len("document target")]
    scoped_capture = {
        **refreshed_base,
        "captureId": "durable-recheck-scoped-fresh-capture",
        "target": {
            "source": "working_target",
            "label": "Working on",
            "wordCount": len(resolved_text.split()),
            "proseMirrorRange": None,
            "selector": {
                "kind": "text_quote",
                "exact": resolved_text,
                "prefix": rendered[max(0, start - 20) : start],
                "suffix": rendered[
                    start
                    + len(resolved_text) : start
                    + len(resolved_text)
                    + 20
                ],
                "start": start,
                "end": start + len(resolved_text),
            },
            "targetTextSha256": sha256_bytes(
                resolved_text.encode("utf-8")
            ),
            "targetReference": capture["target"]["targetReference"],
        },
    }
    with pytest.raises(
        VerifyOrchestrationError,
        match="exact committed-sitting recheck_intent_id",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture={
                **scoped_capture,
                "captureId": "durable-recheck-unbound-manual-capture",
            },
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            validate_selection=lambda selection: selection,
        )
    still_pending = verify_rechecks.verification_recheck_intents(
        store,
        document_id=capture["documentId"],
    )
    assert len(still_pending) == 1
    assert still_pending[0].status == "pending_capture"

    exported = export_store(
        store,
        tmp_path / "portable-recheck.jsonl",
    )
    target = tmp_path / "portable-recheck-target"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store
    monkeypatch.setattr(
        verify_runtime,
        "_DB_PATH",
        tmp_path / "portable-empty-runtime.db",
    )
    restored_intents = verify_rechecks.verification_recheck_intents(
        restored,
        document_id=capture["documentId"],
    )
    assert len(restored_intents) == 1
    assert restored_intents[0].id == intent.id
    assert restored_intents[0].provider_id == SELECTION.provider_id
    assert restored_intents[0].model_id == SELECTION.model_id
    restored_request = restored_intents[0].to_dict()["original_request"]
    assert restored_request["user_goal"] == "Use established terminology."
    assert (
        restored_request["protected_intent"]
        == "Preserve the author's substantive meaning."
    )
    assert restored_request["effective_configuration"]

    for suffix, wrong_goal, wrong_intent in (
        (
            "goal",
            "Use a different terminology policy.",
            "Preserve the author's substantive meaning.",
        ),
        (
            "intent",
            "Use established terminology.",
            "Change the author's substantive meaning.",
        ),
    ):
        with pytest.raises(
            verify_rechecks.VerifyRecheckIntentError,
            match="original user goal and protected intent",
        ):
            start_verify_run(
                store,
                document_id=capture["documentId"],
                capture={
                    **scoped_capture,
                    "captureId": f"durable-recheck-wrong-{suffix}",
                },
                selection=SELECTION,
                actor=HUMAN,
                user_goal=wrong_goal,
                protected_intent=wrong_intent,
                recheck_of_proposal_ids=[proposal.id],
                recheck_of_run_id=started["run_id"],
                recheck_intent_id=intent.id,
                validate_selection=lambda selection: selection,
            )

    rechecked = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=scoped_capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Use established terminology.",
        protected_intent="Preserve the author's substantive meaning.",
        recheck_of_proposal_ids=[proposal.id],
        recheck_of_run_id=started["run_id"],
        recheck_intent_id=intent.id,
        validate_selection=lambda selection: selection,
    )
    fresh_action = verify_rechecks.verify_store.get_record(
        store,
        ActionSnapshot,
        rechecked["action_snapshot_id"],
    )
    assert fresh_action is not None
    assert fresh_action.created_at != scoped_capture["capturedAt"]
    assert (
        json.loads(fresh_action.context_boundary_json)["target_source"]
        == "working_target"
    )
    fulfilled = verify_rechecks.verification_recheck_intents(
        store,
        document_id=capture["documentId"],
    )
    assert len(fulfilled) == 1
    assert fulfilled[0].status == "fulfilled"
    assert fulfilled[0].fulfilled_by_run_ids == (rechecked["run_id"],)
    replayed = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=scoped_capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Use established terminology.",
        protected_intent="Preserve the author's substantive meaning.",
        recheck_of_proposal_ids=[proposal.id],
        recheck_of_run_id=started["run_id"],
        recheck_intent_id=intent.id,
        validate_selection=lambda selection: selection,
    )
    assert replayed["replayed"] is True
    assert replayed["run_id"] == rechecked["run_id"]


def test_unresolved_recheck_requires_bound_affirmation_and_then_fulfills(
    durable_dispatch_ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    capture, started = _start(
        durable_dispatch_ctx,
        target_text="Co-work scope",
    )
    store = durable_dispatch_ctx["store"]
    source_results = verify_rechecks.verify_store.list_records(
        store,
        EvaluationResult,
        where="source.evaluation_run_id = ?",
        params=(started["run_id"],),
    )
    offset = BODY.index("Co-work scope")
    proposal = proposals.propose_edit(
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
        rationale="Use the audited preferred term.",
        tldr="Replace the non-preferred label.",
        actor=AGENT,
    )
    record_result_relation(
        store,
        evaluation_result_id=source_results[0].id,
        relation_kind="addresses",
        target_kind="proposal",
        target_ref=proposal.id,
        actor=AGENT,
    )
    sitting, _created = sitting_lifecycle.prepare_sitting(
        store,
        document_id=capture["documentId"],
        actor=HUMAN,
        items=[
            {
                "proposal_id": proposal.id,
                "verb": "confirm",
                "canonical_sha256": proposal.canonical_sha256,
            }
        ],
        expected_file_sha256=capture["projectionSha256"],
        expected_structured_head_sha256=capture[
            "structuredHeadSha256"
        ],
        idempotency_key="unresolved-verify-recheck-0001",
    )
    rendered = BODY.replace("Co-work scope", "document target")
    snapshot = b"YDOC-UNRESOLVED-VERIFY-RECHECK:" + rendered.encode("utf-8")
    sitting_lifecycle.commit_sitting(
        store,
        document_id=capture["documentId"],
        intent_id=sitting.id,
        actor=HUMAN,
        snapshot=snapshot,
        snapshot_sha256=sha256_bytes(snapshot),
        rendered_markdown=rendered,
        rendered_sha256=sha256_bytes(rendered.encode("utf-8")),
    )

    # Project the source as a pre-target-identity text run: its selector and
    # exact target text remain durable, but the old record cannot attest which
    # UI source/reference the person originally used. Later actions still use
    # the real parser, so fulfillment must validate their persisted evidence.
    original_action_target = verify_rechecks._action_target_for_run

    def legacy_source_target(
        store_arg,
        run,
        *,
        conn=None,
    ):
        target = original_action_target(
            store_arg,
            run,
            conn=conn,
        )
        if target is not None and run.id == started["run_id"]:
            return {
                **target,
                "original_target_source": None,
                "original_target_label": None,
                "original_target_reference_json": None,
                "original_target_reference_sha256": None,
            }
        return target

    monkeypatch.setattr(
        verify_rechecks,
        "_action_target_for_run",
        legacy_source_target,
    )

    intent = verify_rechecks.verification_recheck_intents(
        store,
        document_id=capture["documentId"],
    )[0]
    assert intent.status == "user_action_required"
    projected = intent.to_dict()
    assert projected["user_goal"] == "Use established terminology."
    assert (
        projected["protected_intent"]
        == "Preserve the author's substantive meaning."
    )
    assert projected["requires"]["same_target_source"] is False
    assert projected["requires"]["same_target_reference"] is False
    assert (
        projected["requires"]["user_affirmed_exact_target_required"] is True
    )

    refreshed_document = documents.get_document(
        store,
        capture["documentId"],
    )
    refreshed_head = ydoc_store.current_structured_head(
        store,
        document_id=refreshed_document.id,
        snapshot_sha256=refreshed_document.ydoc_snapshot_sha256,
    )
    refreshed_generation = documents.current_ydoc_generation(
        store,
        refreshed_document.id,
    )
    state_vector = b"unresolved-recheck-state-vector"
    start = rendered.index("document target")
    exact = "document target"
    reference = {
        "schema": "wb.cowork.document-target/v1",
        "storeId": capture["storeId"],
        "documentId": capture["documentId"],
        "kind": "text_range",
        "granularity": "character",
        "relative": {
            "startBase64": "Ag==",
            "endBase64": "Aw==",
        },
        "quote": {
            "exact": exact,
            "prefix": rendered[max(0, start - 20) : start],
            "suffix": rendered[
                start + len(exact) : start + len(exact) + 20
            ],
        },
        "label": "Working on",
        "headingPath": [],
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    _normalized, reference_sha256 = verify_orchestration._target_reference(
        reference,
        store_id=capture["storeId"],
        document_id=capture["documentId"],
    )
    run_capture_id = "unresolved-recheck-run-capture"
    scoped_capture = {
        **capture,
        "captureId": run_capture_id,
        "ydocGenerationSha256": refreshed_generation,
        "snapshotBase64": base64.b64encode(snapshot).decode("ascii"),
        "snapshotSha256": sha256_bytes(snapshot),
        "stateVectorBase64": base64.b64encode(state_vector).decode("ascii"),
        "stateVectorSha256": sha256_bytes(state_vector),
        "structuredHeadSha256": refreshed_head,
        "projectionMarkdown": rendered,
        "projectionSha256": sha256_bytes(rendered.encode("utf-8")),
        "target": {
            "source": "working_target",
            "label": "Working on",
            "wordCount": 2,
            "proseMirrorRange": None,
            "selector": {
                "kind": "text_quote",
                "exact": exact,
                "prefix": rendered[max(0, start - 20) : start],
                "suffix": rendered[
                    start + len(exact) : start + len(exact) + 20
                ],
                "start": start,
                "end": start + len(exact),
            },
            "targetTextSha256": sha256_text(exact),
            "targetReference": reference,
        },
    }
    affirmed_capture_id = "unresolved-recheck-affirmation-capture"
    affirmed_capture = {
        **scoped_capture,
        "captureId": affirmed_capture_id,
    }
    affirmation_receipt = (
        verify_orchestration.affirm_verify_recheck_target(
            store,
            document_id=capture["documentId"],
            capture=affirmed_capture,
            actor=HUMAN,
            recheck_intent_id=intent.id,
            source_run_id=started["run_id"],
            proposal_ids=[proposal.id],
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
        )
    )
    confirmation = {
        "schema": "work-buddy.cowork-recheck-target-confirmation/v1",
        "method": "user_affirmed_working_target",
        "affirmed_capture_id": affirmation_receipt["affirmed_capture_id"],
        "affirmed_action_snapshot_id": affirmation_receipt[
            "affirmed_action_snapshot_id"
        ],
        "run_capture_id": run_capture_id,
        "target_reference_sha256": affirmation_receipt[
            "target_reference_sha256"
        ],
        "target_text_sha256": affirmation_receipt["target_text_sha256"],
    }

    with pytest.raises(
        verify_orchestration.VerifyOrchestrationError,
        match="invalid shape",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=scoped_capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            recheck_intent_id=intent.id,
            recheck_target_confirmation={
                key: value
                for key, value in confirmation.items()
                if key != "affirmed_action_snapshot_id"
            },
            validate_selection=lambda selection: selection,
        )

    with pytest.raises(
        verify_orchestration.VerifyOrchestrationError,
        match="no server-issued affirmation",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=scoped_capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            recheck_intent_id=intent.id,
            recheck_target_confirmation={
                **confirmation,
                "affirmed_action_snapshot_id": "invented-affirmation-action",
            },
            validate_selection=lambda selection: selection,
        )

    with pytest.raises(
        verify_rechecks.VerifyRecheckIntentError,
        match="explicit user affirmation",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture={
                **scoped_capture,
                "captureId": "unresolved-recheck-without-affirmation",
            },
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            recheck_intent_id=intent.id,
            validate_selection=lambda selection: selection,
        )

    with pytest.raises(
        verify_rechecks.VerifyRecheckIntentError,
        match="separate completed affirmation capture",
    ):
        same_capture_id = affirmed_capture_id
        same_capture_confirmation = {
            **confirmation,
            "run_capture_id": same_capture_id,
        }
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture={
                **scoped_capture,
                "captureId": same_capture_id,
            },
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            recheck_intent_id=intent.id,
            recheck_target_confirmation=same_capture_confirmation,
            validate_selection=lambda selection: selection,
        )

    with pytest.raises(
        verify_rechecks.VerifyRecheckIntentError,
        match="changed the affirmed target text",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture={
                **scoped_capture,
                "captureId": "unresolved-recheck-wrong-text",
            },
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            recheck_intent_id=intent.id,
            recheck_target_confirmation={
                **confirmation,
                "run_capture_id": "unresolved-recheck-wrong-text",
                "target_text_sha256": "0" * 64,
            },
            validate_selection=lambda selection: selection,
        )

    block_capture_id = "unresolved-recheck-block-target"
    with pytest.raises(
        verify_rechecks.VerifyRecheckIntentError,
        match="durable target reference",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture={
                **scoped_capture,
                "captureId": block_capture_id,
                "target": {
                    **scoped_capture["target"],
                    "targetReference": {
                        **reference,
                        "granularity": "block",
                    },
                },
            },
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Use established terminology.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            recheck_intent_id=intent.id,
            recheck_target_confirmation={
                **confirmation,
                "run_capture_id": block_capture_id,
            },
            validate_selection=lambda selection: selection,
        )

    rechecked = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=scoped_capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Use established terminology.",
        protected_intent="Preserve the author's substantive meaning.",
        recheck_of_proposal_ids=[proposal.id],
        recheck_of_run_id=started["run_id"],
        recheck_intent_id=intent.id,
        recheck_target_confirmation=confirmation,
        validate_selection=lambda selection: selection,
    )
    job = get_job(rechecked["job_id"])
    persisted_confirmation = job.request["recheck_target_confirmation"]
    assert (
        persisted_confirmation["affirmed_capture_id"]
        == affirmed_capture_id
    )
    assert persisted_confirmation["affirmed_action_snapshot_id"]
    assert (
        persisted_confirmation["target_reference_sha256"]
        == reference_sha256
    )
    affirmed_action = verify_rechecks.verify_store.get_record(
        store,
        ActionSnapshot,
        persisted_confirmation["affirmed_action_snapshot_id"],
    )
    assert affirmed_action is not None
    affirmed_context = json.loads(affirmed_action.context_boundary_json)
    affirmed_egress = json.loads(affirmed_action.egress_boundary_json)
    assert (
        affirmed_context["purpose"]
        == "user_affirmed_exact_recheck_target"
    )
    assert affirmed_context["capture_id"] == affirmed_capture_id
    assert affirmed_egress == {
        "class": "no_external_egress",
        "content": "none",
        "purpose": "recheck_target_affirmation",
    }
    receipt = verify_rechecks.verify_store.get_record(
        store,
        ModelCallAuthorizationReceipt,
        job.authorization_receipt_id,
    )
    assert receipt is not None
    authorization = json.loads(receipt.content_boundary_json)
    assert (
        authorization["authority_context"]["recheck_target_confirmation"]
        == persisted_confirmation
    )
    fulfilled = verify_rechecks.verification_recheck_intents(
        store,
        document_id=capture["documentId"],
    )[0]
    assert fulfilled.status == "fulfilled"
    assert fulfilled.fulfilled_by_run_ids == (rechecked["run_id"],)

    replayed = start_verify_run(
        store,
        document_id=capture["documentId"],
        capture=scoped_capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Use established terminology.",
        protected_intent="Preserve the author's substantive meaning.",
        recheck_of_proposal_ids=[proposal.id],
        recheck_of_run_id=started["run_id"],
        recheck_intent_id=intent.id,
        recheck_target_confirmation=confirmation,
        validate_selection=lambda selection: selection,
    )
    assert replayed["replayed"] is True
    assert replayed["run_id"] == rechecked["run_id"]

    with pytest.raises(
        verify_orchestration.VerifyOrchestrationError,
        match="different Verify inputs",
    ):
        start_verify_run(
            store,
            document_id=capture["documentId"],
            capture=scoped_capture,
            selection=SELECTION,
            actor=HUMAN,
            user_goal="Changed retry goal.",
            protected_intent="Preserve the author's substantive meaning.",
            recheck_of_proposal_ids=[proposal.id],
            recheck_of_run_id=started["run_id"],
            recheck_intent_id=intent.id,
            recheck_target_confirmation=confirmation,
            validate_selection=lambda selection: selection,
        )

    exported = export_store(
        store,
        tmp_path / "fulfilled-legacy-recheck.jsonl",
    )
    target = tmp_path / "fulfilled-legacy-recheck-restored"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store
    monkeypatch.setattr(
        verify_runtime,
        "_DB_PATH",
        tmp_path / "fulfilled-legacy-recheck-empty-runtime.db",
    )
    restored_intent = verify_rechecks.verification_recheck_intents(
        restored,
        document_id=capture["documentId"],
    )[0]
    assert restored_intent.status == "fulfilled"
    assert restored_intent.fulfilled_by_run_ids == (rechecked["run_id"],)
