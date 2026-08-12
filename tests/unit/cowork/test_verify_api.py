from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from work_buddy.cowork import (
    verify_api,
    verify_dispatch,
    verify_orchestration,
    verify_runtime,
)
from work_buddy.cowork.execution_identity import (
    cowork_verify_job_session_id,
)
from work_buddy.cowork.verify_jobs import (
    VerifyJobBinding,
    VerifyJobSpawnMetadata,
)
from work_buddy.cowork.verify import record_cothink_item
from work_buddy.sidecar import internal_operations, retry_sweep
from work_buddy.sidecar.retry_sweep import RetrySweep
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.events import TruthEventEmission

from .conftest import DOC_BODY, NOW

VERIFY_INTENT = {
    "user_goal": "Check this target against the active criteria.",
    "protected_intent": "Preserve the author's intended meaning.",
}


def _capture(seeded):
    store = seeded["store"]
    document = seeded["document"]
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    generation = documents.current_ydoc_generation(store, document.id)
    state_vector = b"throwaway-state-vector"
    return {
        "schema": "wb.cowork.action-snapshot/v1",
        "captureId": "throwaway-capture",
        "storeId": store.store_id,
        "documentId": document.id,
        "capturedAt": NOW,
        "editGeneration": 1,
        "ydocGenerationSha256": generation,
        "snapshotBase64": base64.b64encode(
            seeded["snapshot_bytes"]
        ).decode("ascii"),
        "snapshotSha256": seeded["snapshot_sha256"],
        "stateVectorBase64": base64.b64encode(state_vector).decode("ascii"),
        "stateVectorSha256": sha256_bytes(state_vector),
        "structuredHeadSha256": head,
        "projectionMarkdown": DOC_BODY,
        "projectionSha256": sha256_bytes(DOC_BODY.encode("utf-8")),
        "target": {
            "source": "whole_document",
            "label": "Whole document",
            "wordCount": 7,
            "proseMirrorRange": None,
            "selector": {"kind": "document"},
            "targetTextSha256": sha256_bytes(DOC_BODY.encode("utf-8")),
        },
    }


@pytest.fixture
def fake_verify_host(tmp_path, monkeypatch, seeded):
    monkeypatch.setattr(
        verify_runtime,
        "_DB_PATH",
        tmp_path / "throwaway-verify-runtime.db",
    )
    operations_path = tmp_path / "throwaway-operations"
    operations_path.mkdir()
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
    monkeypatch.setattr(
        verify_dispatch,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    calls = []

    def spawn(**kwargs):
        calls.append(kwargs)
        binding = VerifyJobBinding(
            store_id=kwargs["store_id"],
            document_id=kwargs["document_id"],
            run_id=kwargs["run_id"],
            job_id=kwargs["job_id"],
            role=kwargs["role"],
        )
        return VerifyJobSpawnMetadata(
            status="ok",
            binding=binding,
            session_id=cowork_verify_job_session_id(
                binding.job_id,
                binding.role,
            ),
            selection=kwargs["selection"],
            pid=1234,
        )

    monkeypatch.setattr(verify_dispatch, "spawn_verify_job", spawn)
    return calls


def test_verify_start_is_exact_explicit_and_projects_running_history(
    client,
    seeded,
    fake_verify_host,
):
    response = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/runs"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": _capture(seeded),
            **VERIFY_INTENT,
            "execution": {
                "provider_id": "claude-code",
                "model_id": "sonnet",
                "provider_label": "untrusted caller label",
                "model_label": "untrusted caller label",
            },
        },
        headers={"X-WB-User-Ref": "verify-reviewer"},
    )

    assert response.status_code == 202, response.get_json()
    receipt = response.get_json()
    assert receipt["result_count"] == 1
    assert receipt["coordination_status"] == "pending"
    assert receipt["selection"] == {
        "provider_id": "claude-code",
        "model_id": "sonnet",
        "provider_label": "Claude Code",
        "model_label": "Sonnet",
    }
    assert fake_verify_host == []
    dispatch = RetrySweep().sweep()
    assert len(dispatch) == 1 and dispatch[0]["success"] is True
    assert len(fake_verify_host) == 1
    assert fake_verify_host[0]["role"].value == "coordinator"

    opened = client.get(
        (
            f"/api/truth/doc/{seeded['document'].id}"
            f"?store_id={seeded['store_id']}"
        )
    )
    assert opened.status_code == 200
    payload = opened.get_json()
    assert payload["capabilities"]["cowork_verify"] == {
        "enabled": True,
        "contract_version": 1,
        "can_run": True,
        "can_configure": True,
        "can_cothink": True,
        "disabled_reason": None,
    }
    assert (
        payload["verification_configuration"]["criteria"][0]["stable_key"]
        == "terminology_exact_match"
    )
    execution_plan = payload["verification_configuration"]["execution_plan"]
    assert execution_plan["checker"]["execution_class"] == "in_process"
    assert execution_plan["checker"]["external_egress"] is False
    assert execution_plan["coordination"]["selection"]["provider_id"] is None
    assert execution_plan["coordination"]["content_boundary"] == (
        "entire_frozen_document"
    )
    assert execution_plan["coordination"]["fallback"] == {
        "provider_model_fallback": False,
        "failure_mode": "fail_closed",
    }
    assert payload["evaluation_results"] == []
    assert payload["evaluation_run_summaries"][0]["run_id"] == receipt["run_id"]
    assert payload["evaluation_run_summaries"][0]["status"] == "running"
    assert (
        payload["evaluation_run_summaries"][0]["coordination_status"]
        == "pending"
    )


def test_verify_capture_rejects_browser_hash_mismatch_without_launch(
    client,
    seeded,
    fake_verify_host,
):
    capture = _capture(seeded)
    capture["projectionSha256"] = "0" * 64

    response = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/runs"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": capture,
            **VERIFY_INTENT,
            "execution": {
                "provider_id": "claude-code",
                "model_id": "sonnet",
            },
        },
    )

    assert response.status_code == 409
    assert "projection" in response.get_json()["error"].casefold()
    assert fake_verify_host == []


def test_verify_start_requires_explicit_goal_and_protected_intent(
    client,
    seeded,
    fake_verify_host,
):
    response = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/runs"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": _capture(seeded),
            "execution": {
                "provider_id": "claude-code",
                "model_id": "sonnet",
            },
        },
    )

    assert response.status_code == 409
    assert "user_goal" in response.get_json()["error"]
    assert fake_verify_host == []


def test_recheck_target_affirmation_is_a_separate_nonexecuting_request(
    client,
    seeded,
    monkeypatch,
):
    calls = []

    def affirm(store, **kwargs):
        calls.append((store, kwargs))
        return {
            "schema": (
                "work-buddy.cowork-recheck-target-affirmation-receipt/v1"
            ),
            "recheck_intent_id": kwargs["recheck_intent_id"],
            "source_run_id": kwargs["source_run_id"],
            "pending_proposal_ids": list(kwargs["proposal_ids"]),
            "affirmed_capture_id": kwargs["capture"]["captureId"],
            "affirmed_action_snapshot_id": "affirmed-action-1",
            "target_reference_sha256": "a" * 64,
            "target_text_sha256": "b" * 64,
            "affirmed_at": NOW,
        }

    monkeypatch.setattr(
        verify_api,
        "affirm_verify_recheck_target",
        affirm,
    )
    response = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/"
            "recheck-target-affirmations"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": _capture(seeded),
            "recheck_intent_id": "intent-1",
            "source_run_id": "source-run-1",
            "proposal_ids": ["proposal-1"],
            **VERIFY_INTENT,
        },
        headers={"X-WB-User-Ref": "verify-reviewer"},
    )

    assert response.status_code == 201
    assert response.get_json()["affirmed_action_snapshot_id"] == (
        "affirmed-action-1"
    )
    assert len(calls) == 1
    assert calls[0][1]["actor"].kind == "human"
    assert calls[0][1]["recheck_intent_id"] == "intent-1"


def test_cothink_is_a_distinct_explicit_job(
    client,
    seeded,
    fake_verify_host,
):
    response = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": _capture(seeded),
            "execution": {
                "provider_id": "codex",
                "model_id": "gpt-5.6-sol",
            },
        },
    )

    assert response.status_code == 202
    assert response.get_json()["status"] == "prepared"
    assert fake_verify_host == []
    dispatch = RetrySweep().sweep()
    assert len(dispatch) == 1 and dispatch[0]["success"] is True
    assert len(fake_verify_host) == 1
    assert fake_verify_host[0]["role"].value == "cothink"


def test_verify_configuration_toggle_is_exact_document_and_conflict_checked(
    client,
    seeded,
    fake_verify_host,
):
    started = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/runs"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": _capture(seeded),
            **VERIFY_INTENT,
            "execution": {
                "provider_id": "claude-code",
                "model_id": "sonnet",
            },
        },
    )
    assert started.status_code == 202
    configuration = client.get(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/configuration"
            f"?store_id={seeded['store_id']}"
        )
    ).get_json()["configuration"]
    criterion = configuration["criteria"][0]

    disabled = client.patch(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/criteria/"
            f"{criterion['stable_key']}?store_id={seeded['store_id']}"
        ),
        json={
            "enabled": False,
            "expected_activation_id": criterion["effective_activation"]["id"],
        },
    )
    assert disabled.status_code == 200
    assert (
        disabled.get_json()["configuration"]["criteria"][0][
            "effective_activation"
        ]["enabled"]
        is False
    )

    stale = client.patch(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/criteria/"
            f"{criterion['stable_key']}?store_id={seeded['store_id']}"
        ),
        json={
            "enabled": True,
            "expected_activation_id": criterion["effective_activation"]["id"],
        },
    )
    assert stale.status_code == 409
    assert "configuration changed" in stale.get_json()["error"]


def test_user_criterion_draft_is_visible_but_not_silently_admitted(
    client,
    seeded,
):
    response = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/verify/criteria/drafts"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "title": "State the positive claim",
            "description": (
                "Prefer direct positive descriptions over statements of what "
                "the concept is not."
            ),
            "evaluation_instructions": (
                "Identify negative-definition framing and assess whether a "
                "positive account preserves the intended meaning."
            ),
            "limitations": ["Substantive contrasts may require negation."],
        },
        headers={"X-WB-User-Ref": "verify-reviewer"},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["status"] == "draft_unadmitted"
    criterion = next(
        item
        for item in payload["configuration"]["criteria"]
        if item["stable_key"] == payload["criterion_key"]
    )
    assert criterion["author_origin"]["definition_origin"] == "user"
    assert criterion["operational_state"] == "inactive"
    assert criterion["mechanism_availability"]["state"] == "unavailable"
    assert criterion["checks"][0]["data_sharing"]["class"] == "not_authorized"


def test_user_verification_check_route_creates_an_active_admitted_check(
    client,
    seeded,
):
    body = {
        "title": "State the positive claim",
        "description": "Prefer direct positive descriptions.",
        "evaluation_instructions": (
            "Identify negative-definition framing and assess whether a direct "
            "positive account preserves the intended meaning."
        ),
        "limitations": ["Substantive contrasts may require negation."],
    }
    url = (
        f"/api/truth/doc/{seeded['document'].id}/verify/checks"
        f"?store_id={seeded['store_id']}"
    )

    response = client.post(
        url,
        json=body,
        headers={"X-WB-User-Ref": "verify-reviewer"},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["created"] is True
    assert payload["status"] == "active"
    criterion = next(
        item
        for item in payload["configuration"]["criteria"]
        if item["stable_key"] == payload["criterion_key"]
    )
    assert criterion["author_origin"]["definition_origin"] == "user"
    assert criterion["operational_state"] == "active"
    assert criterion["effective_activation"]["enabled"] is True
    assert criterion["effective_activation"]["authorized_by"] == {
        "kind": "human",
        "ref": "reviewer-kaden",
        "meta": None,
    }
    assert criterion["checks"][0]["origin"]["definition_origin"] == "system"
    assert criterion["checks"][0]["availability"]["state"] == "available"
    assert criterion["checks"][0]["binding"]["configuration"] == {
        "evaluation_instructions": body["evaluation_instructions"],
        "limitations": body["limitations"],
    }

    repeated = client.post(
        url,
        json=body,
        headers={"X-WB-User-Ref": "verify-reviewer"},
    )
    assert repeated.status_code == 200
    assert repeated.get_json()["created"] is False
    assert repeated.get_json()["criterion_key"] == payload["criterion_key"]


def test_cothink_item_can_be_parked_then_dismissed_by_exact_item_hash(
    client,
    seeded,
    fake_verify_host,
):
    started = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": _capture(seeded),
            "execution": {
                "provider_id": "codex",
                "model_id": "gpt-5.6-sol",
            },
        },
    )
    assert started.status_code == 202
    item = record_cothink_item(
        seeded["store"],
        action_snapshot_id=started.get_json()["action_snapshot_id"],
        subtype="alternative_perspective",
        purpose="Consider another constraint.",
        payload={"text": "What if the dependency is unavailable?"},
        rationale="The current draft assumes availability.",
        provenance={"kind": "test-agent"},
        actor=Actor("agent_run", "cothink-test", {
            "session_id": "cothink-test",
            "harness": "pytest",
            "model": "test-model",
            "model_source": "test",
            "surface": "test",
        }),
    )

    mismatch = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink/items/"
            f"{item.id}/actions?store_id={seeded['store_id']}"
        ),
        json={"action": "park", "canonical_sha256": "0" * 64},
    )
    assert mismatch.status_code == 409

    parked = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink/items/"
            f"{item.id}/actions?store_id={seeded['store_id']}"
        ),
        json={"action": "park", "canonical_sha256": item.canonical_sha256},
    )
    assert parked.status_code == 200
    assert parked.get_json()["status"] == "parked"

    opened = client.get(
        (
            f"/api/truth/doc/{seeded['document'].id}"
            f"?store_id={seeded['store_id']}"
        )
    ).get_json()
    assert opened["cothink_items"][0]["status"] == "parked"

    stale_dismiss = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink/items/"
            f"{item.id}/actions?store_id={seeded['store_id']}"
        ),
        json={"action": "dismiss", "canonical_sha256": "f" * 64},
    )
    assert stale_dismiss.status_code == 409

    dismissed = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink/items/"
            f"{item.id}/actions?store_id={seeded['store_id']}"
        ),
        json={
            "action": "dismiss",
            "canonical_sha256": item.canonical_sha256,
        },
    )
    assert dismissed.status_code == 200
    assert dismissed.get_json()["status"] == "dismissed"
    reopened = client.get(
        (
            f"/api/truth/doc/{seeded['document'].id}"
            f"?store_id={seeded['store_id']}"
        )
    ).get_json()
    assert reopened["cothink_items"][0]["status"] == "dismissed"


def test_cothink_discuss_saves_exact_non_evidential_context_in_chat(
    client,
    seeded,
    fake_document_agent,
    fake_verify_host,
    monkeypatch,
):
    from work_buddy.conversations import store as conversation_store
    from work_buddy.cowork import chat_targets, document_agent
    from work_buddy.truth import registry as truth_registry

    monkeypatch.setattr(
        chat_targets,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    monkeypatch.setattr(
        truth_registry,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    started = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": _capture(seeded),
            "execution": {
                "provider_id": "codex",
                "model_id": "gpt-5.6-sol",
            },
        },
    )
    item = record_cothink_item(
        seeded["store"],
        action_snapshot_id=started.get_json()["action_snapshot_id"],
        subtype="alternative_perspective",
        purpose="Invite a useful challenge.",
        payload={"text": "What if this choice should remain reversible?"},
        rationale="The draft treats the choice as permanent.",
        provenance={"kind": "test-agent"},
        actor=Actor(
            "agent_run",
            "cothink-discuss-test",
            {
                "session_id": "cothink-discuss-test",
                "harness": "pytest",
                "model": "test-model",
                "model_source": "test",
                "surface": "test",
            },
        ),
    )

    discussed = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink/items/"
            f"{item.id}/actions?store_id={seeded['store_id']}"
        ),
        json={
            "action": "discuss",
            "canonical_sha256": item.canonical_sha256,
        },
    )

    assert discussed.status_code == 200
    receipt = discussed.get_json()
    assert receipt["status"] == "discussing"
    bundle = conversation_store.get_conversation_with_messages(
        receipt["conversation_id"]
    )
    assert bundle is not None
    turn = bundle["messages"][-1]
    assert turn["message_id"] == receipt["message_id"]
    assert turn["context"]["action_snapshot_id"] == item.action_snapshot_id
    assert turn["context"]["discussion"] == {
        "kind": "cothink_item",
        "item_id": item.id,
        "canonical_sha256": item.canonical_sha256,
        "content": "What if this choice should remain reversible?",
        "rationale": "The draft treats the choice as permanent.",
        "non_evidential": True,
    }
    assert len(fake_document_agent) == 1
    assert fake_document_agent[0]["conversation_id"] == receipt["conversation_id"]
    assert fake_document_agent[0]["store_id"] == seeded["store_id"]
    assert fake_document_agent[0]["document_id"] == seeded["document"].id
    opened = client.get(
        (
            f"/api/truth/doc/{seeded['document'].id}"
            f"?store_id={seeded['store_id']}"
        )
    ).get_json()
    assert opened["cothink_items"][0]["status"] == "open"

    messages_before_failed_wake = len(bundle["messages"])

    def _wake_failure(_conversation_id: str):
        raise RuntimeError("throwaway Co-think wake failure")

    monkeypatch.setattr(
        document_agent,
        "ensure_bound_document_agent",
        _wake_failure,
    )
    discussed_with_failed_wake = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink/items/"
            f"{item.id}/actions?store_id={seeded['store_id']}"
        ),
        json={
            "action": "discuss",
            "canonical_sha256": item.canonical_sha256,
        },
    )

    assert discussed_with_failed_wake.status_code == 200
    failed_wake_receipt = discussed_with_failed_wake.get_json()
    after_failed_wake = conversation_store.get_conversation_with_messages(
        receipt["conversation_id"]
    )
    assert after_failed_wake is not None
    assert len(after_failed_wake["messages"]) == messages_before_failed_wake + 1
    assert (
        after_failed_wake["messages"][-1]["message_id"]
        == failed_wake_receipt["message_id"]
    )


def test_cothink_no_item_and_unavailable_outcomes_survive_doc_reload(
    client,
    seeded,
    fake_verify_host,
    monkeypatch,
):
    monkeypatch.setattr(
        verify_orchestration,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    first_capture = _capture(seeded)
    first_capture["captureId"] = "cothink-none-capture"
    first = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": first_capture,
            "execution": {
                "provider_id": "codex",
                "model_id": "gpt-5.6-sol",
            },
        },
    ).get_json()
    no_item_job = verify_orchestration.submit_worker_job(
        job_id=first["job_id"],
        payload={
            "outcome": "none",
            "rationale": "No useful alternative was found.",
        },
        agent_session_id=(
            verify_runtime.get_job(first["job_id"]).session_id
        ),
    )
    assert no_item_job["status"] == "completed"

    second_capture = _capture(seeded)
    second_capture["captureId"] = "cothink-unavailable-capture"
    second = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/cothink"
            f"?store_id={seeded['store_id']}"
        ),
        json={
            "capture": second_capture,
            "execution": {
                "provider_id": "codex",
                "model_id": "gpt-5.6-sol",
            },
        },
    ).get_json()
    unavailable_job = verify_runtime.get_job(second["job_id"])
    assert unavailable_job is not None
    verify_dispatch._mark_unavailable(
        unavailable_job,
        error_code="provider_unavailable",
        error="The selected account-backed agent is unavailable.",
    )

    reloaded = client.get(
        (
            f"/api/truth/doc/{seeded['document'].id}"
            f"?store_id={seeded['store_id']}"
        )
    ).get_json()
    outcomes = {
        item["outcome_id"]: item
        for item in reloaded["cothink_outcomes"]
    }
    assert outcomes[first["job_id"]]["status"] == (
        "completed_no_useful_item"
    )
    assert outcomes[first["job_id"]]["rationale"] == (
        "No useful alternative perspective was found."
    )
    assert outcomes[second["job_id"]]["status"] == "unavailable"


def test_cothink_no_item_emits_outcome_event_not_item_added(monkeypatch):
    from work_buddy.cowork import ops as cowork_ops
    from work_buddy.cowork import verify_events

    job = SimpleNamespace(
        job_id="cothink-no-item-job",
        role=verify_orchestration.CoworkVerifyRole.COTHINK,
        store_id="store-1",
        document_id="doc-1",
        evaluation_run_id="action-1",
    )
    monkeypatch.setattr(verify_runtime, "get_job", lambda _job_id: job)
    monkeypatch.setattr(
        verify_orchestration,
        "submit_worker_job",
        lambda **_kwargs: {
            "status": "completed",
            "output_sha256": "a" * 64,
            "cothink_item_id": None,
        },
    )
    emitted: list[tuple[str, dict]] = []

    def _emit(event_type, **kwargs):
        emitted.append((event_type, kwargs))
        return TruthEventEmission("event-1", True)

    monkeypatch.setattr(verify_events, "emit_truth_event", _emit)
    result = cowork_ops.cowork_verify_job_submit(
        job.job_id,
        {"outcome": "none", "rationale": "Nothing useful."},
        agent_session_id="ignored-by-test-double",
    )

    assert result["event"]["published"] is True
    assert [event_type for event_type, _kwargs in emitted] == [
        "truth.doc_cothink_outcome_recorded"
    ]
    assert emitted[0][1]["data"]["outcome"] == "none"
