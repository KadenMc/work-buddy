from __future__ import annotations

import hashlib

import pytest

from work_buddy.tasks import (
    FieldDerivation,
    TaskCreationCoordinator,
    TaskCreationDecisionVerificationError,
    TaskCreationIntentError,
    TaskDocumentLink,
    verify_published_task_creation_decision,
)


def _link(task_id: str) -> TaskDocumentLink:
    return TaskDocumentLink(
        task_id=task_id,
        note_uuid=f"note-{task_id}",
        store_id="store-task-knowledge",
        document_id=f"document-{task_id}",
        binding_id=f"binding-{task_id}",
        lifecycle="current",
        created_at="2026-08-23T17:00:00+00:00",
        updated_at="2026-08-23T17:00:00+00:00",
    )


def test_task_and_requested_note_publish_as_one_taskstore_commit(
    task_store,
    task_service,
) -> None:
    coordinator = TaskCreationCoordinator(task_store)
    request = {
        "description": "Coordinate the rich task",
        "requested_note_role": "working_document/v1",
        "requested_truth_policy_resolution": "disabled",
        "initial_note": "Human-authored note",
    }
    intent = coordinator.prepare(
        client_mutation_id="rich-create-1",
        task_id="t-rich-1",
        actor="dashboard:user",
        session_id="session-1",
        request=request,
        requested_note_role="working_document/v1",
        requested_truth_policy_resolution="disabled",
    )
    assert task_store.get("t-rich-1") is None
    assert intent.status == "prepared"

    link = _link("t-rich-1")
    prepared = coordinator.record_document_prepared(
        intent.intent_id,
        document=link,
        interaction_contract_id="working_document",
        interaction_contract_revision=1,
        interaction_contract_digest="a" * 64,
        activation_state="disabled",
        activation_revision=1,
        document_content_sha256="b" * 64,
        document_head_sha256="c" * 64,
        document_provenance_sha256="d" * 64,
        document_admission_prepare_receipt_id="pending-seal-1",
    )
    assert prepared.status == "document_prepared"
    committed = coordinator.commit_decision(intent.intent_id)
    assert committed.status == "decision_committed"
    assert committed.coordinator_decision_sha256 != (
        intent.provisional_coordinator_decision_sha256
    )
    admitted = coordinator.acknowledge_document_admission(
        intent.intent_id,
        coordinator_decision_id=intent.coordinator_decision_id,
        coordinator_decision_sha256=committed.coordinator_decision_sha256,
        admission_receipt_id="admission-seal-1",
        activation_revision=1,
    )
    assert admitted.status == "document_admitted"
    assert task_store.get("t-rich-1") is None

    note_hash = hashlib.sha256(b"Human-authored note").hexdigest()
    result = task_service.create(
        description="Coordinate the rich task",
        client_mutation_id="rich-create-1",
        actor="dashboard:user",
        session_id="session-1",
        task_id="t-rich-1",
        creation_intent_id=intent.intent_id,
        initial_document=link,
        field_derivations=(
            FieldDerivation(
                field_name="task_note.initial_body",
                value_sha256=note_hash,
                authorship="human",
            ),
        ),
    )

    assert result.task.note_uuid == link.note_uuid
    assert task_store.get_task_document_link("t-rich-1") == link
    published = coordinator.get(intent.intent_id)
    assert published is not None
    assert published.status == "published"
    assert published.task_receipt_id == result.receipt.receipt_id
    verified = verify_published_task_creation_decision(
        task_store,
        task_id="t-rich-1",
        store_id=link.store_id,
        document_id=link.document_id,
        binding_id=link.binding_id,
        coordinator_decision_id=published.coordinator_decision_id,
        coordinator_decision_sha256=published.coordinator_decision_sha256,
    )
    assert verified.task_id == "t-rich-1"
    assert verified.task_receipt_id == result.receipt.receipt_id
    with pytest.raises(TaskCreationDecisionVerificationError):
        verify_published_task_creation_decision(
            task_store,
            task_id="t-rich-1",
            store_id=link.store_id,
            document_id=link.document_id,
            binding_id=link.binding_id,
            coordinator_decision_id=published.coordinator_decision_id,
            coordinator_decision_sha256="f" * 64,
        )
    conn = task_store.connect()
    try:
        row = conn.execute(
            "SELECT field_name,value_sha256,authorship,review_state "
            "FROM task_field_derivation_receipts WHERE intent_id=?",
            (intent.intent_id,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(row) == (
        "task_note.initial_body",
        note_hash,
        "human",
        "unreviewed",
    )

    replay = task_service.create(
        description="Coordinate the rich task",
        client_mutation_id="rich-create-1",
        actor="dashboard:user",
        session_id="session-1",
        task_id="t-rich-1",
        creation_intent_id=intent.intent_id,
        initial_document=link,
        field_derivations=(
            FieldDerivation(
                field_name="task_note.initial_body",
                value_sha256=note_hash,
                authorship="human",
            ),
        ),
    )
    assert replay.replayed is True
    assert replay.task.task_id == "t-rich-1"


def test_truth_choice_is_bound_before_coordinator_decision(task_store) -> None:
    coordinator = TaskCreationCoordinator(task_store)
    intent = coordinator.prepare(
        client_mutation_id="rich-create-enabled",
        task_id="t-rich-enabled",
        actor="dashboard:user",
        session_id=None,
        request={
            "requested_note_role": "working_document/v1",
            "requested_truth_policy_resolution": "enabled",
        },
        requested_note_role="working_document/v1",
        requested_truth_policy_resolution="enabled",
    )
    coordinator.record_document_prepared(
        intent.intent_id,
        document=_link("t-rich-enabled"),
        interaction_contract_id="working_document",
        interaction_contract_revision=1,
        interaction_contract_digest="b" * 64,
        activation_state="disabled",
        activation_revision=1,
        document_content_sha256="c" * 64,
        document_head_sha256="d" * 64,
        document_provenance_sha256="e" * 64,
        document_admission_prepare_receipt_id="pending-seal-enabled",
    )
    with pytest.raises(TaskCreationIntentError, match="does not match"):
        coordinator.commit_decision(intent.intent_id)


def test_creation_intent_replay_rejects_changed_request(task_store) -> None:
    from work_buddy.tasks import TaskIdempotencyConflict

    coordinator = TaskCreationCoordinator(task_store)
    kwargs = {
        "client_mutation_id": "rich-replay",
        "task_id": "t-rich-replay",
        "actor": "dashboard:user",
        "session_id": None,
        "requested_note_role": "working_document/v1",
        "requested_truth_policy_resolution": "disabled",
    }
    first = coordinator.prepare(request={"description": "first"}, **kwargs)
    second = coordinator.prepare(request={"description": "first"}, **kwargs)
    assert second == first
    with pytest.raises(TaskIdempotencyConflict):
        coordinator.prepare(request={"description": "changed"}, **kwargs)


def test_committed_decision_digest_detects_changed_participant_receipt(task_store) -> None:
    coordinator = TaskCreationCoordinator(task_store)
    intent = coordinator.prepare(
        client_mutation_id="participant-digest",
        task_id="t-participant-digest",
        actor="dashboard:user",
        session_id=None,
        request={"description": "receipt-bound"},
        requested_note_role="working_document/v1",
        requested_truth_policy_resolution="disabled",
    )
    coordinator.record_document_prepared(
        intent.intent_id,
        document=_link(intent.task_id),
        interaction_contract_id="working_document",
        interaction_contract_revision=1,
        interaction_contract_digest="1" * 64,
        activation_state="disabled",
        activation_revision=1,
        document_content_sha256="2" * 64,
        document_head_sha256="3" * 64,
        document_provenance_sha256="4" * 64,
        document_admission_prepare_receipt_id="pending-seal-digest",
    )
    committed = coordinator.commit_decision(intent.intent_id)
    assert committed.decision_payload_json is not None
    assert '"content_sha256":"' + "2" * 64 + '"' in committed.decision_payload_json
    assert '"structured_head_sha256":"' + "3" * 64 + '"' in committed.decision_payload_json
    assert '"provenance_sha256":"' + "4" * 64 + '"' in committed.decision_payload_json
    assert '"admission_prepare_receipt_id":"pending-seal-digest"' in (
        committed.decision_payload_json
    )

    with task_store.transaction() as conn:
        conn.execute(
            "UPDATE task_creation_intents SET document_head_sha256=? WHERE intent_id=?",
            ("5" * 64, intent.intent_id),
        )
    with pytest.raises(TaskCreationIntentError, match="payload changed"):
        coordinator.commit_decision(intent.intent_id)
