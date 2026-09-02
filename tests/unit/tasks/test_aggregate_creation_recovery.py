from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.tasks.aggregate_creation import TaskAggregateCreationService
from work_buddy.tasks.creation import (
    FieldDerivation,
    TaskCreationCoordinator,
    verify_published_task_creation_decision,
)
from work_buddy.tasks.documents import TaskDocumentService, TaskDocumentStoreManager
from work_buddy.tasks.errors import TaskIdempotencyConflict, TaskValidationError
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore
from work_buddy.tasks.runtime import arm_native_authority_latch
from work_buddy.truth.registry import TruthStoreRegistry


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")


def _runner(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.initialize()
    state = store.system_state()
    arm_native_authority_latch(
        store.path,
        cohort_id="aggregate-recovery-test",
        target_authority_epoch="native:test",
        cutover_receipt_id="aggregate-recovery-cutover",
        armed_at="2026-08-27T18:00:00+00:00",
    )
    store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:test",
        updated_at="2026-08-27T18:00:00+00:00",
        cutover_receipt_id="aggregate-recovery-cutover",
        process_generation=1,
    )
    manager = TaskDocumentStoreManager(
        root=tmp_path / "task-knowledge",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    documents = TaskDocumentService(stores=manager)
    return (
        TaskAggregateCreationService(
            store,
            task_service=TaskApplicationService(store),
            document_service=documents,
        ),
        store,
        documents,
    )


def _request(runner: TaskAggregateCreationService, *, note: str = "Prepared note"):
    return runner.create(
        client_mutation_id="aggregate-crash-request",
        actor="human:test",
        session_id="session-test",
        task_values={
            "description": "Recover aggregate creation",
            "urgency": "medium",
            "summary_text": "Stable scalar summary",
            "tags": (),
            "creation_provenance": "dashboard",
        },
        initial_note=note,
        requested_truth_policy_resolution="enabled",
        field_derivations=(
            FieldDerivation(
                field_name="description",
                value_sha256=hashlib.sha256(
                    b"Recover aggregate creation"
                ).hexdigest(),
                authorship="human",
            ),
            FieldDerivation(
                field_name="task_note.initial_body",
                value_sha256=hashlib.sha256(note.encode("utf-8")).hexdigest(),
                authorship="human",
            ),
        ),
    )


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"urgency": "eventually"},
        {"agent_required_contexts": "not-a-list"},
        {"unexpected_task_field": "not supported"},
    ],
)
def test_invalid_task_request_is_rejected_before_intent_or_document_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_values: dict[str, object],
) -> None:
    runner, store, documents = _runner(tmp_path)
    document_create_called = False
    original_create = documents.create

    def track_document_create(**kwargs):
        nonlocal document_create_called
        document_create_called = True
        return original_create(**kwargs)

    monkeypatch.setattr(documents, "create", track_document_create)
    task_values = {
        "description": "Must not leave an aggregate behind",
        "tags": (),
        **invalid_values,
    }

    with pytest.raises(TaskValidationError):
        runner.create(
            client_mutation_id="invalid-aggregate-request",
            actor="human:test",
            session_id="session-test",
            task_values=task_values,
            initial_note="This document must never be prepared.",
        )

    assert document_create_called is False
    assert store.list() == []
    assert TaskCreationCoordinator(store).recovery_queue() == ()
    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_creation_intents"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_mutation_receipts"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_duplicate_task_id_is_rejected_before_intent_or_document_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, store, documents = _runner(tmp_path)
    runner.task_service.create(
        description="Existing task",
        task_id="t-existing-aggregate-target",
        client_mutation_id="seed-existing-task",
        actor="human:test",
    )
    document_create_called = False

    def fail_if_document_created(**_kwargs):
        nonlocal document_create_called
        document_create_called = True
        raise AssertionError("duplicate task ID reached document preparation")

    monkeypatch.setattr(documents, "create", fail_if_document_created)

    with pytest.raises(TaskValidationError) as raised:
        runner.create(
            client_mutation_id="duplicate-aggregate-request",
            actor="human:test",
            session_id="session-test",
            task_values={
                "task_id": "t-existing-aggregate-target",
                "description": "Must not replace the existing task",
                "tags": (),
            },
            initial_note="This document must never be prepared.",
        )

    assert raised.value.field_errors == {
        "task_id": "That task ID already exists."
    }
    assert document_create_called is False
    assert TaskCreationCoordinator(store).recovery_queue() == ()
    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_creation_intents"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_mutation_receipts "
            "WHERE client_mutation_id='duplicate-aggregate-request'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 1
    finally:
        conn.close()


def test_published_aggregate_replay_allows_its_existing_task_without_new_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, store, documents = _runner(tmp_path)
    document_create_calls = 0
    original_create = documents.create

    def track_document_create(**kwargs):
        nonlocal document_create_calls
        document_create_calls += 1
        return original_create(**kwargs)

    monkeypatch.setattr(documents, "create", track_document_create)
    first = _request(runner)
    replay = _request(runner)

    assert replay.replayed is True
    assert replay.task.task_id == first.task.task_id
    assert replay.receipt.receipt_id == first.receipt.receipt_id
    assert document_create_calls == 1
    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_creation_intents"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_mutation_receipts"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 1
    finally:
        conn.close()


def test_aggregate_reservation_commits_before_direct_create_and_blocks_it(
    tmp_path: Path,
) -> None:
    runner, store, _documents = _runner(tmp_path)
    coordinator = TaskCreationCoordinator(store)
    intent = coordinator.prepare(
        client_mutation_id="aggregate-reserves-first",
        task_id="t-reserved-before-direct-create",
        actor="human:aggregate",
        session_id="session-aggregate",
        request={"description": "Reserved aggregate task"},
        requested_note_role=None,
        requested_truth_policy_resolution=None,
    )

    with pytest.raises(TaskValidationError) as raised:
        runner.task_service.create(
            description="Competing direct task",
            task_id=intent.task_id,
            client_mutation_id="direct-create-after-reservation",
            actor="human:direct",
        )

    assert raised.value.field_errors == {
        "task_id": "That task ID is reserved by an aggregate creation."
    }
    assert store.get(intent.task_id, include_deleted=True) is None
    assert coordinator.get(intent.intent_id) == intent
    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_mutation_receipts "
            "WHERE client_mutation_id='direct-create-after-reservation'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_direct_create_commits_before_aggregate_prepare_and_blocks_reservation(
    tmp_path: Path,
) -> None:
    runner, store, _documents = _runner(tmp_path)
    runner.task_service.create(
        description="Direct task wins the serialization order",
        task_id="t-direct-before-reservation",
        client_mutation_id="direct-create-before-reservation",
        actor="human:direct",
    )
    coordinator = TaskCreationCoordinator(store)

    with pytest.raises(TaskValidationError) as raised:
        coordinator.prepare(
            client_mutation_id="aggregate-prepare-after-direct",
            task_id="t-direct-before-reservation",
            actor="human:aggregate",
            session_id="session-aggregate",
            request={"description": "Losing aggregate task"},
            requested_note_role=None,
            requested_truth_policy_resolution=None,
        )

    assert raised.value.field_errors == {
        "task_id": "That task ID already exists."
    }
    assert store.get("t-direct-before-reservation") is not None
    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_creation_intents "
            "WHERE client_mutation_id='aggregate-prepare-after-direct'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_aggregate_reservation_blocks_batch_and_rolls_back_every_item(
    tmp_path: Path,
) -> None:
    runner, store, _documents = _runner(tmp_path)
    coordinator = TaskCreationCoordinator(store)
    intent = coordinator.prepare(
        client_mutation_id="aggregate-reserves-before-batch",
        task_id="t-reserved-before-batch",
        actor="human:aggregate",
        session_id="session-aggregate",
        request={"description": "Reserved before batch"},
        requested_note_role=None,
        requested_truth_policy_resolution=None,
    )

    with pytest.raises(TaskValidationError) as raised:
        runner.task_service.batch_create(
            (
                {
                    "task_id": "t-free-item-before-reserved-item",
                    "description": "This earlier item must roll back",
                },
                {
                    "task_id": intent.task_id,
                    "description": "This item conflicts with the reservation",
                },
            ),
            client_mutation_id="batch-after-aggregate-reservation",
            actor="human:batch",
        )

    assert raised.value.field_errors == {
        "items": (
            "Task ID 't-reserved-before-batch' is reserved by an aggregate creation."
        )
    }
    assert store.get("t-free-item-before-reserved-item", include_deleted=True) is None
    assert store.get(intent.task_id, include_deleted=True) is None
    assert coordinator.get(intent.intent_id) == intent
    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_mutation_receipts "
            "WHERE client_mutation_id='batch-after-aggregate-reservation'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 0
    finally:
        conn.close()


def test_batch_commits_before_prepare_and_ordinary_batch_replay_stays_idempotent(
    tmp_path: Path,
) -> None:
    runner, store, _documents = _runner(tmp_path)
    items = (
        {
            "task_id": "t-batch-before-aggregate-prepare",
            "description": "Batch wins the serialization order",
        },
    )
    first = runner.task_service.batch_create(
        items,
        client_mutation_id="batch-before-aggregate-prepare",
        actor="human:batch",
    )
    coordinator = TaskCreationCoordinator(store)

    with pytest.raises(TaskValidationError) as raised:
        coordinator.prepare(
            client_mutation_id="aggregate-after-batch",
            task_id="t-batch-before-aggregate-prepare",
            actor="human:aggregate",
            session_id="session-aggregate",
            request={"description": "Aggregate loses to batch"},
            requested_note_role=None,
            requested_truth_policy_resolution=None,
        )

    replay = runner.task_service.batch_create(
        items,
        client_mutation_id="batch-before-aggregate-prepare",
        actor="human:batch",
    )
    assert raised.value.field_errors == {
        "task_id": "That task ID already exists."
    }
    assert replay.replayed is True
    assert replay.receipt.receipt_id == first.receipt.receipt_id
    assert [task.task_id for task in replay.tasks] == [
        "t-batch-before-aggregate-prepare"
    ]
    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_creation_intents "
            "WHERE client_mutation_id='aggregate-after-batch'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_mutation_receipts"
        ).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("boundary", "pending_status"),
    [
        ("document_prepared", "document_prepared"),
        ("decision_committed", "decision_committed"),
        ("document_admitted", "document_admitted"),
        ("published", "published"),
    ],
)
def test_recovery_rolls_every_crash_boundary_forward_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    pending_status: str,
) -> None:
    runner, store, documents = _runner(tmp_path)

    if boundary == "document_prepared":
        original = runner.coordinator.record_document_prepared

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash after document prepare")

        monkeypatch.setattr(runner.coordinator, "record_document_prepared", crash)
    elif boundary == "decision_committed":
        original = runner.coordinator.commit_decision

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash after coordinator decision")

        monkeypatch.setattr(runner.coordinator, "commit_decision", crash)
    elif boundary == "document_admitted":
        original = runner._admit_document

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash after document admission")

        monkeypatch.setattr(runner, "_admit_document", crash)
    else:
        original = runner.task_service.create

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("response lost after TaskStore publication")

        monkeypatch.setattr(runner.task_service, "create", crash)

    with pytest.raises(RuntimeError, match="crash|response lost"):
        _request(runner)

    intent = TaskCreationCoordinator(store).recovery_queue()
    if pending_status == "published":
        assert intent == ()
        published = store.connect()
        try:
            status = published.execute(
                "SELECT status FROM task_creation_intents"
            ).fetchone()[0]
        finally:
            published.close()
        assert status == "published"
    else:
        assert len(intent) == 1
        assert intent[0].status == pending_status
        assert store.list() == []
        with pytest.raises(TaskIdempotencyConflict):
            _request(TaskAggregateCreationService(
                store,
                task_service=TaskApplicationService(store),
                document_service=documents,
            ), note="Changed retry note")

    recovered_runner = TaskAggregateCreationService(
        store,
        task_service=TaskApplicationService(store),
        document_service=documents,
    )
    if pending_status == "published":
        result = _request(recovered_runner)
        assert result.replayed is True
    else:
        report = recovered_runner.reconcile_pending(limit=10)
        assert report["recovered"], report
        assert report["failed"] == []
        assert report["remaining"] == 0
        assert recovered_runner.reconcile_pending(limit=10)["examined"] == 0
        result = _request(recovered_runner)
        assert result.replayed is True

    task = result.task
    link = store.get_task_document_link(task.task_id)
    assert link is not None
    conn = store.connect()
    try:
        intent_id = str(
            conn.execute("SELECT intent_id FROM task_creation_intents").fetchone()[0]
        )
    finally:
        conn.close()
    decision = TaskCreationCoordinator(store).get(intent_id)
    assert decision is not None and decision.status == "published"
    verify_published_task_creation_decision(
        store,
        task_id=task.task_id,
        store_id=link.store_id,
        document_id=link.document_id,
        binding_id=link.binding_id,
        coordinator_decision_id=decision.coordinator_decision_id,
        coordinator_decision_sha256=decision.coordinator_decision_sha256,
    )

    conn = store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_document_links").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_field_derivation_receipts"
        ).fetchone()[0] == 2
    finally:
        conn.close()
    cowork = documents.stores.open_existing()
    conn = cowork.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM document_truth_activation_transitions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM document_truth_admission_seal_events"
        ).fetchone()[0] == 3
    finally:
        conn.close()
    causality = DocumentCausalityStore(cowork.paths.sidecar)
    bindings = causality.list_bindings()
    assert len(bindings) == 1
    assert bindings[0].binding_id == link.binding_id
    assert bindings[0].document_id == link.document_id
