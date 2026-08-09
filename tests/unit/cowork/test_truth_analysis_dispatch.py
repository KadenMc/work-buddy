from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.agent_execution.models import AgentSpawnOutcome, AgentSpawnRequest
from work_buddy.cowork import (
    truth_analysis,
    truth_analysis_dispatch,
    truth_analysis_runtime,
)
from work_buddy.cowork.truth_analysis_dispatch import (
    dispatch_truth_analysis_launch,
    enqueue_truth_analysis_launch,
    reconcile_truth_analysis_launches,
)
from work_buddy.sidecar.internal_operations import (
    COWORK_TRUTH_ANALYSIS_LAUNCH,
    internal_operation_id,
)

from .conftest import HUMAN
from .test_truth_analysis import _capture, _selection


@pytest.fixture
def dispatch_env(seeded, tmp_path, monkeypatch):
    runtime_path = tmp_path / "truth-analysis-runtime.db"
    operations_path = tmp_path / "operations"
    operations_path.mkdir()
    monkeypatch.setattr(truth_analysis_runtime, "_DB_PATH", runtime_path)
    monkeypatch.setattr(
        truth_analysis,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    monkeypatch.setattr(
        truth_analysis_dispatch,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    return {**seeded, "operations_path": operations_path}


def _prepared(env):
    view = truth_analysis.prepare_analysis_run(
        env["store"],
        document_id=env["document"].id,
        capture=_capture(env),
        selection=_selection(),
        actor=HUMAN,
        selection_validator=lambda selection: selection,
    )
    run = truth_analysis_runtime.get_run(view["analysis_run_id"])
    assert run is not None
    return run


def _record(env, run_id):
    op_id = internal_operation_id(COWORK_TRUTH_ANALYSIS_LAUNCH, run_id)
    path = env["operations_path"] / f"{op_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["lease_token"] = "queue-lease-1"
    record["locked_until"] = (
        datetime.now(timezone.utc) + timedelta(minutes=1)
    ).isoformat()
    return path, record


def test_enqueue_and_dispatch_launch_exact_bound_account_worker(dispatch_env):
    run = _prepared(dispatch_env)
    queued = enqueue_truth_analysis_launch(
        run,
        store=dispatch_env["store"],
        operations_dir=dispatch_env["operations_path"],
    )
    path, record = _record(dispatch_env, run.run_id)
    requests: list[AgentSpawnRequest] = []

    def _spawn(request: AgentSpawnRequest) -> AgentSpawnOutcome:
        requests.append(request)
        return AgentSpawnOutcome(
            status="ok",
            selection=_selection(),
            pid=987654,
            session_id=request.session_id,
        )

    result = dispatch_truth_analysis_launch(record, spawn_detached=_spawn)
    persisted = truth_analysis_runtime.get_run(run.run_id)

    assert queued["queued"] is True
    assert path.is_file()
    assert record["name"] == COWORK_TRUTH_ANALYSIS_LAUNCH
    assert record["params"] == {"run_id": run.run_id}
    assert result == {"run_id": run.run_id, "status": "running", "pid": 987654}
    assert persisted is not None and persisted.status == "running"
    assert persisted.pid == 987654
    assert len(requests) == 1
    assert requests[0].selection.provider_id == run.selection["provider_id"]
    assert requests[0].selection.model_id == run.selection["model_id"]


def test_reconcile_recreates_missing_prepared_handoff(dispatch_env):
    run = _prepared(dispatch_env)

    result = reconcile_truth_analysis_launches(
        operations_dir=dispatch_env["operations_path"]
    )
    op_id = internal_operation_id(COWORK_TRUTH_ANALYSIS_LAUNCH, run.run_id)

    assert result["queued"] == 1
    assert (dispatch_env["operations_path"] / f"{op_id}.json").is_file()
    assert truth_analysis_runtime.get_run(run.run_id).status == "prepared"


def test_dispatch_rejects_queue_record_bound_to_another_run(dispatch_env):
    run = _prepared(dispatch_env)
    enqueue_truth_analysis_launch(
        run,
        store=dispatch_env["store"],
        operations_dir=dispatch_env["operations_path"],
    )
    _path, record = _record(dispatch_env, run.run_id)
    record["params"] = {"run_id": "f" * 32}

    with pytest.raises(
        truth_analysis_dispatch.TruthAnalysisDispatchError,
        match="identity",
    ):
        dispatch_truth_analysis_launch(record, spawn_detached=lambda _request: None)


def test_reconcile_terminalizes_legacy_provider_without_hard_ceiling(
    dispatch_env,
    monkeypatch,
):
    selection = _selection().__class__(
        provider_id="codex",
        model_id="gpt-5",
        provider_label="Codex",
        model_label="GPT-5",
    )
    monkeypatch.setattr(
        truth_analysis,
        "analysis_provider_capability",
        lambda provider_id: {
            "provider_id": provider_id,
            "analysis_available": True,
            "unavailable_reason": None,
            "applies_to_all_models": True,
            "cost_control": {},
        },
    )
    view = truth_analysis.prepare_analysis_run(
        dispatch_env["store"],
        document_id=dispatch_env["document"].id,
        capture=_capture(dispatch_env),
        selection=selection,
        actor=HUMAN,
        selection_validator=lambda value: value,
    )

    counts = reconcile_truth_analysis_launches(
        operations_dir=dispatch_env["operations_path"]
    )
    run = truth_analysis_runtime.get_run(view["analysis_run_id"])

    assert counts["expired"] == 1
    assert run is not None
    assert run.status == "unavailable"
    assert run.error_code == "analysis_provider_cost_control_unavailable"
    assert not any(dispatch_env["operations_path"].glob("*.json"))


def test_reconcile_fences_overdue_worker_then_same_passage_can_rerun(
    dispatch_env,
    monkeypatch,
):
    run = _prepared(dispatch_env)
    truth_analysis_runtime.update_run(run.run_id, status="running", pid=445566)
    with truth_analysis_runtime._connect() as conn:
        truth_analysis_runtime._ensure_schema(conn)
        conn.execute(
            "UPDATE cowork_truth_analysis_runs SET execution_deadline_at = ? "
            "WHERE run_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                run.run_id,
            ),
        )
    terminated = []
    monkeypatch.setattr(
        "work_buddy.sidecar.dispatch.executor.terminate_detached_process",
        lambda pid, *, owner_token: terminated.append((pid, owner_token)) or True,
    )

    counts = reconcile_truth_analysis_launches(
        operations_dir=dispatch_env["operations_path"]
    )
    expired = truth_analysis_runtime.get_run(run.run_id)
    rerun = truth_analysis.prepare_analysis_run(
        dispatch_env["store"],
        document_id=dispatch_env["document"].id,
        capture=_capture(dispatch_env),
        selection=_selection(),
        actor=HUMAN,
        selection_validator=lambda value: value,
    )

    assert counts["deadline_exceeded"] == 1
    assert counts["terminated"] == 1
    assert terminated == [(445566, run.session_id)]
    assert expired is not None
    assert expired.status == "failed"
    assert expired.error_code == "execution_deadline_exceeded"
    assert rerun["analysis_run_id"] != run.run_id
    with pytest.raises(
        truth_analysis.TruthAnalysisError,
        match="execution deadline",
    ):
        truth_analysis.get_worker_context(
            run_id=run.run_id,
            agent_session_id=run.session_id,
        )
