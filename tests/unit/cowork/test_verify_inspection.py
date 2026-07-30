"""Safe inspection projection for Co-work Verify runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.cowork import verify_runtime
from work_buddy.cowork.execution_identity import CoworkVerifyRole
from work_buddy.cowork.verify import (
    record_model_call_authorization,
    record_routing_disposition,
    run_terminology_exact_match,
)
from work_buddy.cowork.verify_inspection import (
    VerifyInspectionError,
    verify_run_detail,
)
from work_buddy.cowork.verify_runtime import create_job
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import sha256_text

from .conftest import HUMAN, NOW
from .test_verify_persistence import _capture


SYSTEM = Actor("system", "verify-inspection-test")


def test_inspection_contains_typed_records_without_private_worker_output(
    store_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        verify_runtime,
        "_DB_PATH",
        tmp_path / "agents" / "verify-inspection.db",
    )
    document, _, action = _capture(store_ctx)
    evaluation = run_terminology_exact_match(
        store_ctx["store"],
        action_snapshot_id=action.id,
        actor=SYSTEM,
        at=NOW,
    )
    result = evaluation.results[0]
    disposition = record_routing_disposition(
        store_ctx["store"],
        evaluation_result_id=result.id,
        decision="suppress",
        rationale="The clean result should remain quiet.",
        policy_snapshot_sha256="a" * 64,
        actor=SYSTEM,
        at=NOW,
    )
    receipt = record_model_call_authorization(
        store_ctx["store"],
        action_snapshot_id=action.id,
        plan_snapshot_id=evaluation.plan.id,
        provider="codex",
        model="gpt-test",
        context_sha256="b" * 64,
        content_boundary={"document": "complete_permitted_frozen_projection"},
        egress_class="account_backed_agent",
        cost_ceiling_usd=2,
        retry_limit=0,
        expires_at="2026-07-30T00:00:00.000+00:00",
        actor=HUMAN,
        at=NOW,
    )
    create_job(
        job_id="inspection-job",
        store_id=store_ctx["store"].store_id,
        document_id=document.id,
        evaluation_run_id=evaluation.run.id,
        action_snapshot_id=action.id,
        plan_snapshot_id=evaluation.plan.id,
        role=CoworkVerifyRole.COORDINATOR,
        selection={
            "provider_id": "codex",
            "model_id": "gpt-test",
            "provider_label": "Codex",
            "model_label": "GPT Test",
        },
        authorization_receipt_id=receipt.id,
        context_sha256=sha256_text("typed-context"),
        request={"private_prompt": "must not escape"},
        session_id="inspection-job-session",
        at=NOW,
    )

    detail = verify_run_detail(
        store_ctx["store"],
        document_id=document.id,
        run_id=evaluation.run.id,
    )

    assert detail["plan"]["plan_snapshot_id"] == evaluation.plan.id
    assert detail["results"][0]["dispositions"][0]["id"] == disposition.id
    assert detail["coordination"][0]["provider"] == "codex"
    assert detail["coordination"][0]["cost_ceiling_usd"] == 2
    assert "output" not in detail["coordination"][0]
    assert "request" not in detail["coordination"][0]
    assert "private_prompt" not in str(detail)


def test_inspection_rejects_cross_document_run(store_ctx):
    _, _, action = _capture(store_ctx)
    evaluation = run_terminology_exact_match(
        store_ctx["store"],
        action_snapshot_id=action.id,
        actor=SYSTEM,
        at=NOW,
    )

    with pytest.raises(VerifyInspectionError, match="does not belong"):
        verify_run_detail(
            store_ctx["store"],
            document_id="another-document",
            run_id=evaluation.run.id,
        )
