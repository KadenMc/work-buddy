"""Document-isolation regressions for personal Co-work Verify checks."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from work_buddy.agent_execution.models import (
    AgentExecutionSelection,
    AgentSpawnOutcome,
    AgentSpawnRequest,
)
from work_buddy.cowork import verify_orchestration, verify_runtime
from work_buddy.cowork.execution_identity import CoworkVerifyRole
from work_buddy.cowork.verify_configuration import (
    create_user_verification_check,
    list_effective_verification_configuration,
    set_document_criterion_enabled,
)
from work_buddy.cowork.verify_orchestration import (
    get_worker_job,
    start_verify_run,
)
from work_buddy.cowork.verify_runtime import get_job
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.identity import sha256_bytes, sha256_text

from .conftest import HUMAN, NOW


SELECTION = AgentExecutionSelection(
    provider_id="codex",
    model_id="gpt-5.6-sol",
    provider_label="Codex",
    model_label="GPT-5.6 Sol",
)


def _register_document(
    store_ctx: dict[str, Any],
    *,
    slug: str,
    title: str,
    body: str,
) -> tuple[Any, bytes, bytes]:
    store = store_ctx["store"]
    projection = body.encode("utf-8")
    path = f"docs/{slug}.md"
    target = store_ctx["root"] / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(projection)
    snapshot = b"YDOC-VERIFY-SCOPE:" + slug.encode("ascii") + b":" + projection
    snapshot_sha256 = ydoc_store.write_snapshot(store, snapshot=snapshot)
    document = documents.register_document(
        store,
        path=path,
        title=title,
        document_class="co_authored",
        content_sha256=sha256_bytes(projection),
        ydoc_snapshot_sha256=snapshot_sha256,
        actor=HUMAN,
        at=NOW,
    )
    return document, projection, snapshot


def _whole_document_capture(
    store_ctx: dict[str, Any],
    *,
    document: Any,
    projection: bytes,
    snapshot: bytes,
    capture_id: str,
) -> dict[str, Any]:
    store = store_ctx["store"]
    snapshot_sha256 = sha256_bytes(snapshot)
    state_vector = f"state-vector:{capture_id}".encode("utf-8")
    structured_head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=snapshot_sha256,
    )
    projection_text = projection.decode("utf-8")
    return {
        "schema": "wb.cowork.action-snapshot/v1",
        "captureId": capture_id,
        "storeId": store.store_id,
        "documentId": document.id,
        "capturedAt": NOW,
        "editGeneration": 1,
        "ydocGenerationSha256": documents.current_ydoc_generation(
            store,
            document.id,
        ),
        "snapshotBase64": base64.b64encode(snapshot).decode("ascii"),
        "snapshotSha256": snapshot_sha256,
        "stateVectorBase64": base64.b64encode(state_vector).decode("ascii"),
        "stateVectorSha256": sha256_bytes(state_vector),
        "structuredHeadSha256": structured_head,
        "projectionMarkdown": projection_text,
        "projectionSha256": sha256_bytes(projection),
        "target": {
            "source": "whole_document",
            "label": "Whole document",
            "wordCount": len(projection_text.split()),
            "proseMirrorRange": None,
            "selector": {"kind": "document"},
            "targetTextSha256": sha256_text(projection_text),
        },
    }


@pytest.fixture
def verify_scope_ctx(
    store_ctx: dict[str, Any],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    monkeypatch.setattr(
        verify_runtime,
        "_DB_PATH",
        tmp_path / "runtime" / "verify-jobs.db",
    )
    monkeypatch.setattr(
        verify_orchestration,
        "TruthStoreRegistry",
        lambda: store_ctx["registry"],
    )
    spawned: list[AgentSpawnRequest] = []

    def _spawn(request: AgentSpawnRequest) -> AgentSpawnOutcome:
        spawned.append(request)
        return AgentSpawnOutcome(
            status="ok",
            selection=request.selection,
            pid=900 + len(spawned),
            session_id=request.session_id,
        )

    return {**store_ctx, "spawn": _spawn, "spawned": spawned}


def test_personal_check_does_not_cross_document_configuration_or_coordination(
    verify_scope_ctx: dict[str, Any],
):
    store = verify_scope_ctx["store"]
    document_a, projection_a, snapshot_a = _register_document(
        verify_scope_ctx,
        slug="personal-check-owner",
        title="Personal check owner",
        body="# Document A\n\nThis document owns a private verification check.\n",
    )
    document_b, projection_b, snapshot_b = _register_document(
        verify_scope_ctx,
        slug="personal-check-neighbor",
        title="Personal check neighbor",
        body="# Document B\n\nThis document must not inherit Document A checks.\n",
    )
    del projection_a, snapshot_a

    private_title = "A-only positive-framing check"
    private_instructions = (
        "A-ONLY-INSTRUCTIONS: flag definitions framed primarily by negation."
    )
    created = create_user_verification_check(
        store,
        document_id=document_a.id,
        title=private_title,
        description="This check belongs only to Document A.",
        evaluation_instructions=private_instructions,
        actor=HUMAN,
        at=NOW,
    )
    private_key = created["criterion_key"]

    configuration_a = list_effective_verification_configuration(
        store,
        document_id=document_a.id,
        execution_selection=SELECTION,
    )
    configuration_b = list_effective_verification_configuration(
        store,
        document_id=document_b.id,
        execution_selection=SELECTION,
    )
    assert private_key in {
        item["stable_key"] for item in configuration_a["criteria"]
    }
    assert private_key not in {
        item["stable_key"] for item in configuration_b["criteria"]
    }
    serialized_configuration_b = json.dumps(
        configuration_b,
        sort_keys=True,
    )
    assert private_title not in serialized_configuration_b
    assert private_instructions not in serialized_configuration_b

    capture_b = _whole_document_capture(
        verify_scope_ctx,
        document=document_b,
        projection=projection_b,
        snapshot=snapshot_b,
        capture_id="capture-document-b",
    )
    started = start_verify_run(
        store,
        document_id=document_b.id,
        capture=capture_b,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Verify Document B with its applicable checks.",
        protected_intent="Preserve Document B's meaning.",
        validate_selection=lambda selection: selection,
        spawn_detached=verify_scope_ctx["spawn"],
    )
    coordinator = get_job(started["job_id"])
    assert coordinator is not None
    assert coordinator.role is CoworkVerifyRole.COORDINATOR
    context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]

    assert context["policy"]["effective_configuration"] == configuration_b
    serialized_context = json.dumps(context, sort_keys=True)
    assert private_key not in serialized_context
    assert private_title not in serialized_context
    assert private_instructions not in serialized_context


def test_disabled_personal_check_stays_in_menu_but_not_run_context(
    verify_scope_ctx: dict[str, Any],
):
    store = verify_scope_ctx["store"]
    document, projection, snapshot = _register_document(
        verify_scope_ctx,
        slug="disabled-personal-check",
        title="Disabled personal check",
        body="# Document\n\nOnly active checks belong in a Verify run.\n",
    )
    sentinel = "DISABLED-SENTINEL-MUST-NOT-EGRESS"
    created = create_user_verification_check(
        store,
        document_id=document.id,
        title="Disabled sentinel check",
        description="A personal check which will be disabled.",
        evaluation_instructions=sentinel,
        actor=HUMAN,
        at=NOW,
    )
    personal_key = created["criterion_key"]
    created_criterion = next(
        item
        for item in created["configuration"]["criteria"]
        if item["stable_key"] == personal_key
    )
    menu_configuration = set_document_criterion_enabled(
        store,
        document_id=document.id,
        criterion_key=personal_key,
        enabled=False,
        expected_activation_id=created_criterion["effective_activation"]["id"],
        actor=HUMAN,
        at=NOW,
    )["configuration"]
    disabled = next(
        item
        for item in menu_configuration["criteria"]
        if item["stable_key"] == personal_key
    )
    assert disabled["operational_state"] == "inactive"
    assert sentinel in json.dumps(menu_configuration, sort_keys=True)

    capture = _whole_document_capture(
        verify_scope_ctx,
        document=document,
        projection=projection,
        snapshot=snapshot,
        capture_id="capture-disabled-personal-check",
    )
    started = start_verify_run(
        store,
        document_id=document.id,
        capture=capture,
        selection=SELECTION,
        actor=HUMAN,
        user_goal="Run only the selected checks.",
        protected_intent="Preserve the document's meaning.",
        validate_selection=lambda selection: selection,
        spawn_detached=verify_scope_ctx["spawn"],
    )
    coordinator = get_job(started["job_id"])
    assert coordinator is not None
    assert coordinator.role is CoworkVerifyRole.COORDINATOR
    context = get_worker_job(
        job_id=coordinator.job_id,
        agent_session_id=coordinator.session_id,
    )["context"]

    serialized_context = json.dumps(context, sort_keys=True)
    serialized_request = json.dumps(coordinator.request, sort_keys=True)
    assert personal_key not in serialized_context
    assert personal_key not in serialized_request
    assert sentinel not in serialized_context
    assert sentinel not in serialized_request
