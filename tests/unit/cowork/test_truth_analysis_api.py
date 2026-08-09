from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.cowork import (
    api as cowork_api,
    truth_analysis,
    truth_analysis_api,
    truth_analysis_runtime,
)
from work_buddy.truth import documents

from .conftest import HUMAN
from .test_truth_analysis import _capture, _candidate, _payload


@pytest.fixture
def analysis_api_env(client, seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(
        truth_analysis_runtime,
        "_DB_PATH",
        tmp_path / "truth-analysis-api.db",
    )
    monkeypatch.setattr(
        truth_analysis,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    enqueued = []
    monkeypatch.setattr(
        truth_analysis_api,
        "enqueue_truth_analysis_launch",
        lambda run, **_kwargs: enqueued.append(run.run_id) or {"queued": True},
    )
    return {**seeded, "client": client, "enqueued": enqueued}


def _base(env):
    return f"/api/truth/doc/{env['document'].id}/truth/analysis-runs"


def _url(env, suffix: str = ""):
    return f"{_base(env)}{suffix}?store_id={env['store'].store_id}"


def _capabilities_url(env):
    return (
        f"/api/truth/doc/{env['document'].id}/truth/analysis-capabilities"
        f"?store_id={env['store'].store_id}"
    )


def test_capabilities_report_only_enforceable_model_session_ceiling(
    analysis_api_env,
):
    response = analysis_api_env["client"].get(
        _capabilities_url(analysis_api_env)
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["schema"] == "wb.cowork.truth-analysis-capabilities/v1"
    assert payload["required_cost_control"] == {
        "enforcement_class": "hard_ceiling",
        "scope": "worker_model_session",
        "maximum_usd_per_model_session": 2.0,
    }
    assert payload["research_cost_control"] == {
        "enforcement_class": "unavailable",
        "scope": "web_search_and_fetch",
        "ceiling_usd": None,
        "basis": "research_provider_cost_not_enforced",
    }
    providers = {item["provider_id"]: item for item in payload["providers"]}
    assert providers["claude-code"]["analysis_available"] is True
    assert providers["claude-code"]["cost_control"][
        "ceiling_usd_per_worker_session"
    ] == 2.0
    assert providers["codex"]["analysis_available"] is False
    assert providers["codex"]["cost_control"][
        "ceiling_usd_per_worker_session"
    ] is None


def test_start_rejects_provider_without_enforceable_ceiling_before_launch(
    analysis_api_env,
    monkeypatch,
):
    env = analysis_api_env
    monkeypatch.setattr(
        truth_analysis,
        "_default_selection_validator",
        lambda selection: selection,
    )

    response = env["client"].post(
        _url(env),
        json={
            "capture": _capture(env),
            "execution": {"provider_id": "codex", "model_id": "gpt-5"},
        },
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "ok": False,
        "error": "Truth analysis requires a provider-enforced hard spending ceiling.",
        "code": "analysis_provider_cost_control_unavailable",
        "retryable": False,
        "details": {
            "provider_capability": truth_analysis.analysis_provider_capability(
                "codex"
            )
        },
    }
    assert env["enqueued"] == []
    assert truth_analysis_runtime.runs_for_document(
        env["store"].store_id, env["document"].id
    ) == ()


def test_start_load_and_dismiss_candidate_over_http(analysis_api_env):
    env = analysis_api_env
    started_response = env["client"].post(
        _url(env),
        json={
            "capture": _capture(env),
            "execution": {
                "provider_id": "claude-code",
                "model_id": "sonnet",
            },
        },
    )

    assert started_response.status_code == 202
    started = started_response.get_json()
    assert started["status"] == "queued"
    assert started["target_choice"] == "current_selection"
    assert env["enqueued"] == [started["analysis_run_id"]]
    current = env["client"].get(_url(env, "/current"))
    specific = env["client"].get(
        _url(env, f"/{started['analysis_run_id']}")
    )
    assert current.status_code == 200
    assert specific.status_code == 200
    assert current.get_json()["analysis_run_id"] == started["analysis_run_id"]

    run = truth_analysis_runtime.get_run(started["analysis_run_id"])
    context = truth_analysis.get_worker_context(
        run_id=run.run_id,
        agent_session_id=run.session_id,
    )
    receipt = truth_analysis.submit_worker_output(
        run_id=run.run_id,
        agent_session_id=run.session_id,
        payload=_payload(context, [_candidate("A staged HTTP candidate.")]),
    )
    assert receipt["schema"] == "wb.cowork.truth-analysis-submit-receipt/v1"
    completed = env["client"].get(
        _url(env, f"/{started['analysis_run_id']}")
    ).get_json()
    candidate = completed["candidates"][0]
    decision_url = (
        f"{_base(env)}/{run.run_id}/candidates/{candidate['candidate_id']}/"
        f"decisions?store_id={env['store'].store_id}"
    )
    dismissed = env["client"].post(
        decision_url,
        json={
            "decision": "dismiss",
            "expected_canonical_sha256": candidate["canonical_sha256"],
        },
    )

    assert dismissed.status_code == 200
    assert dismissed.get_json() == {
        **dismissed.get_json(),
        "ok": True,
        "analysis_run_id": run.run_id,
        "candidate_id": candidate["candidate_id"],
        "candidate_status": "dismissed",
        "claim_id": None,
        "expression_id": None,
    }


def test_current_returns_404_when_document_has_no_analysis(analysis_api_env):
    response = analysis_api_env["client"].get(_url(analysis_api_env, "/current"))

    assert response.status_code == 404
    assert response.get_json()["ok"] is False


def test_start_requires_write_authority_while_existing_runs_remain_readable(
    analysis_api_env,
    monkeypatch,
):
    env = analysis_api_env
    monkeypatch.setattr(cowork_api, "_is_read_only", lambda: True)

    blocked = env["client"].post(
        _url(env),
        json={
            "capture": _capture(env),
            "execution": {
                "provider_id": "claude-code",
                "model_id": "sonnet",
            },
        },
    )
    observable = env["client"].get(_url(env, "/current"))

    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "Dashboard is in read-only mode"
    assert observable.status_code == 404
    assert "No Truth analysis run" in observable.get_json()["error"]
    assert env["enqueued"] == []


def test_retired_document_cannot_start_new_paid_analysis(analysis_api_env):
    env = analysis_api_env
    documents.retire_document(
        env["store"],
        document_id=env["document"].id,
        actor=HUMAN,
    )

    blocked = env["client"].post(
        _url(env),
        json={
            "capture": _capture(env),
            "execution": {
                "provider_id": "claude-code",
                "model_id": "sonnet",
            },
        },
    )

    assert blocked.status_code == 409
    assert "retired document" in blocked.get_json()["error"]
    assert env["enqueued"] == []


def test_second_target_is_rejected_while_current_analysis_is_open(
    analysis_api_env,
):
    env = analysis_api_env
    execution = {"provider_id": "claude-code", "model_id": "sonnet"}
    first_response = env["client"].post(
        _url(env),
        json={"capture": _capture(env), "execution": execution},
    )
    first = first_response.get_json()

    blocked = env["client"].post(
        _url(env),
        json={
            "capture": _capture(env, capture_id="second-browser-tab-capture"),
            "execution": execution,
        },
    )
    current = env["client"].get(_url(env, "/current"))

    assert first_response.status_code == 202
    assert blocked.status_code == 409
    assert "Finish reviewing the current Truth analysis" in (
        blocked.get_json()["error"]
    )
    assert current.get_json()["analysis_run_id"] == first["analysis_run_id"]
    assert env["enqueued"] == [first["analysis_run_id"]]


def test_current_terminalizes_overdue_run_and_same_passage_can_restart(
    analysis_api_env,
):
    env = analysis_api_env
    request_body = {
        "capture": _capture(env),
        "execution": {"provider_id": "claude-code", "model_id": "sonnet"},
    }
    first = env["client"].post(_url(env), json=request_body).get_json()
    with truth_analysis_runtime._connect() as conn:
        truth_analysis_runtime._ensure_schema(conn)
        conn.execute(
            "UPDATE cowork_truth_analysis_runs SET status = 'running', pid = ?, "
            "execution_deadline_at = ? WHERE run_id = ?",
            (
                778899,
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                first["analysis_run_id"],
            ),
        )

    current = env["client"].get(_url(env, "/current"))
    restarted_response = env["client"].post(_url(env), json=request_body)
    restarted = restarted_response.get_json()

    assert current.status_code == 200
    assert current.get_json()["status"] == "failed"
    assert current.get_json()["error_code"] == "execution_deadline_exceeded"
    assert restarted_response.status_code == 202
    assert restarted["analysis_run_id"] != first["analysis_run_id"]
    assert restarted["status"] == "queued"
    assert env["enqueued"] == [
        first["analysis_run_id"],
        restarted["analysis_run_id"],
    ]
