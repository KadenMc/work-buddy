from __future__ import annotations

import sqlite3
import uuid

import pytest

from work_buddy.tasks.errors import (
    TaskIdempotencyConflict,
    TaskRevisionConflict,
    TaskValidationError,
)
from work_buddy.tasks.models import Tag, TaskDocumentLink, TaskQuery
from work_buddy.tasks.service import TaskApplicationService

from .conftest import create_task


def _authority(revision: int, mutation_id: str) -> dict:
    return {
        "expected_revision": revision,
        "client_mutation_id": mutation_id,
        "actor": "dashboard:user",
        "session_id": "session-1",
    }


def test_create_is_durable_idempotent_and_emits_one_atomic_event(task_service, task_store):
    created = create_task(
        task_service,
        tags=[Tag("research/native", True), "projects/work-buddy"],
        due_date="2026-08-30",
        deadline_date="2026-09-15",
    )
    replay = create_task(
        task_service,
        tags=[Tag("research/native", True), "projects/work-buddy"],
        due_date="2026-08-30",
        deadline_date="2026-09-15",
    )

    assert created.task.revision == 1
    assert created.collection_revision == 1
    assert created.task.due_date == "2026-08-30"
    assert created.task.deadline_date == "2026-09-15"
    assert created.task.project == "work-buddy"
    assert created.task.namespace_tags == ("research/native",)
    assert replay.replayed is True
    assert replay.task.task_id == created.task.task_id
    assert replay.receipt.receipt_id == created.receipt.receipt_id
    assert task_store.collection_revision() == 1
    assert len(task_store.history(created.task.task_id)) == 1
    assert len(task_store.pending_outbox()) == 1


def test_idempotency_key_mismatch_is_rejected(task_service):
    create_task(task_service)
    with pytest.raises(TaskIdempotencyConflict):
        create_task(task_service, description="Different request")


def test_update_is_cas_guarded_and_dates_remain_distinct(task_service, task_store):
    task = create_task(task_service).task
    updated = task_service.update(
        task.task_id,
        expected_revision=task.revision,
        client_mutation_id="update-1",
        actor="agent:codex",
        changes={
            "description": "Write and verify migration tests",
            "due_date": "2026-08-31",
            "deadline_date": "2026-09-30",
            "urgency": "high",
        },
    )
    assert updated.task.revision == 2
    assert updated.task.due_date == "2026-08-31"
    assert updated.task.deadline_date == "2026-09-30"
    assert updated.collection_revision == 2
    with pytest.raises(TaskRevisionConflict) as conflict:
        task_service.update(
            task.task_id,
            expected_revision=1,
            client_mutation_id="stale-update",
            actor="agent:codex",
            changes={"urgency": "low"},
        )
    assert conflict.value.current_revision == 2
    assert task_store.get(task.task_id).urgency == "high"


def test_complete_reopen_focus_snooze_and_resume_restore_attention(task_service):
    current = create_task(
        task_service,
        outcome_text="A tested neutral task domain",
    ).task
    current = task_service.focus(current.task_id, **_authority(current.revision, "focus-1")).task
    assert current.state == "focused"

    current = task_service.snooze(
        current.task_id,
        until="2026-08-25",
        **_authority(current.revision, "snooze-1"),
    ).task
    assert current.state == "snoozed"
    assert current.snooze_resume_state == "focused"

    current = task_service.complete(
        current.task_id,
        **_authority(current.revision, "complete-1"),
    ).task
    assert current.state == "done"
    assert current.completed_at is not None
    assert current.snooze_until is None

    current = task_service.reopen(
        current.task_id,
        **_authority(current.revision, "reopen-1"),
    ).task
    assert current.state == "focused"
    assert current.completed_at is None

    current = task_service.snooze(
        current.task_id,
        until="2026-08-26T09:00:00-04:00",
        **_authority(current.revision, "snooze-2"),
    ).task
    current = task_service.resume(
        current.task_id,
        **_authority(current.revision, "resume-1"),
    ).task
    assert current.state == "focused"
    assert current.snooze_until is None
    assert current.snooze_resume_state is None


def test_focus_requires_handoff_context(task_service):
    task = create_task(task_service).task
    with pytest.raises(TaskValidationError) as error:
        task_service.focus(task.task_id, **_authority(task.revision, "focus-invalid"))
    assert "state" in error.value.field_errors


def test_archive_delete_restore_and_exact_tags(task_service, task_store):
    task = create_task(task_service, tags=["alpha", Tag("ns/old", True)]).task
    replaced = task_service.replace_tags(
        task.task_id,
        tags=["beta", Tag("ns/new", True)],
        **_authority(task.revision, "tags-1"),
    )
    task = replaced.task
    assert [tag.name for tag in task.tags] == ["beta", "ns/new"]
    assert task_store.search("alpha") == []

    task = task_service.archive(task.task_id, **_authority(task.revision, "archive-1")).task
    assert task.archived_at is not None
    task = task_service.unarchive(task.task_id, **_authority(task.revision, "unarchive-1")).task
    assert task.archived_at is None

    task = task_service.delete(task.task_id, **_authority(task.revision, "delete-1")).task
    assert task.deleted_at is not None
    assert task_store.get(task.task_id) is None
    assert [tag.name for tag in task_store.get(task.task_id, include_deleted=True).tags] == ["beta", "ns/new"]

    task = task_service.restore(task.task_id, **_authority(task.revision, "restore-1")).task
    assert task.deleted_at is None
    assert task.restored_at is not None


def test_query_and_search_are_sqlite_authoritative(task_service):
    create_task(
        task_service,
        task_id="t-alpha",
        mutation_id="create-alpha",
        description="Alpha compiler task",
        tags=[Tag("research/compiler", True), "projects/work-buddy"],
    )
    beta = create_task(
        task_service,
        task_id="t-beta",
        mutation_id="create-beta",
        description="Beta dashboard task",
        urgency="high",
    ).task
    task_service.snooze(beta.task_id, until="2026-09-01", **_authority(beta.revision, "snooze-beta"))

    assert [task.task_id for task in task_service.search("compiler")] == ["t-alpha"]
    assert [task.task_id for task in task_service.list(TaskQuery(project="work-buddy"))] == ["t-alpha"]
    assert task_service.list(TaskQuery(urgency="high")) == []
    assert [task.task_id for task in task_service.list(TaskQuery(state="snoozed"))] == ["t-beta"]


def test_authoring_fields_states_and_tags_save_in_one_revision(task_service):
    task = create_task(task_service).task
    saved = task_service.update(
        task.task_id,
        expected_revision=task.revision,
        client_mutation_id="authoring-save",
        actor="dashboard:user",
        changes={
            "summary_text": "A compact handoff",
            "outcome_text": "A complete implementation",
            "next_action_text": "Run the suite",
            "definition_of_done": "All checks pass",
            "dependencies": ["React provider", "Co-work store"],
        },
        tags=["projects/work-buddy", Tag("task/backend", True)],
        state="focused",
    )
    assert saved.task.revision == 2
    assert saved.task.summary_text == "A compact handoff"
    assert saved.task.dependencies == ("React provider", "Co-work store")
    assert saved.task.project == "work-buddy"
    assert saved.task.namespace_tags == ("task/backend",)
    assert saved.task.state == "focused"

    active = task_service.set_state(
        task.task_id,
        state="active",
        **_authority(saved.task.revision, "state-active"),
    ).task
    waiting = task_service.set_state(
        task.task_id,
        state="waiting",
        **_authority(active.revision, "state-waiting"),
    ).task
    assert waiting.state == "waiting"


def test_attach_document_is_atomic_and_retry_safe(task_service, task_store):
    task = create_task(task_service).task
    link = TaskDocumentLink(
        task_id=task.task_id,
        note_uuid="note-uuid-1",
        store_id="store-1",
        document_id="doc-1",
        binding_id="binding-1",
        lifecycle="current",
        created_at="2026-08-23T17:00:00+00:00",
        updated_at="2026-08-23T17:00:00+00:00",
    )
    attached = task_service.attach_document(
        task.task_id,
        link,
        expected_revision=task.revision,
        client_mutation_id="attach-doc",
        actor="dashboard:user",
    )
    replay = task_service.attach_document(
        task.task_id,
        link,
        expected_revision=task.revision,
        client_mutation_id="attach-doc",
        actor="dashboard:user",
    )
    assert attached.task.revision == 2
    assert attached.task.note_uuid == "note-uuid-1"
    assert task_store.get_task_document_link(task.task_id) == link
    assert replay.replayed is True
    assert replay.receipt.receipt_id == attached.receipt.receipt_id


def test_session_assignment_has_receipt_history_and_reverse_query(task_service, task_store):
    task = create_task(task_service).task
    assigned = task_service.assign(
        task.task_id,
        "session-assignee",
        expected_revision=task.revision,
        client_mutation_id="assign-1",
        actor="agent:codex",
        actor_session_id="session-actor",
    )
    replay = task_service.assign(
        task.task_id,
        "session-assignee",
        expected_revision=task.revision,
        client_mutation_id="assign-1",
        actor="agent:codex",
        actor_session_id="session-actor",
    )
    assert assigned.task.revision == 2
    assert replay.replayed is True
    assert task_store.get_tasks_for_session("session-assignee") == [
        {
            "task_id": task.task_id,
            "assigned_at": "2026-08-23T17:00:00+00:00",
        }
    ]
    history = task_store.history(task.task_id)[0]
    assert history.mutation == "assign"
    assert history.session_id == "session-actor"


def test_batch_create_is_atomic_deterministic_and_retry_safe(task_service, task_store):
    items = [
        {
            "title": "First pasted task",
            "summary": "First summary",
            "desired_outcome": "First outcome",
            "project": "work-buddy",
            "namespaces": ["task/imported"],
        },
        {
            "description": "Second pasted task",
            "state": "waiting",
            "dependencies": ["First pasted task"],
        },
    ]
    result = task_service.batch_create(
        items,
        client_mutation_id="batch-1",
        actor="dashboard:user",
    )
    replay = task_service.batch_create(
        items,
        client_mutation_id="batch-1",
        actor="dashboard:user",
    )
    assert len(result.tasks) == 2
    expected_id = "t-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        "work-buddy:task-batch:batch-1:0",
    ).hex[:8]
    assert result.tasks[0].task_id == expected_id
    assert result.tasks[0].summary_text == "First summary"
    assert result.tasks[1].state == "waiting"
    assert result.tasks[1].dependencies == ("First pasted task",)
    assert result.collection_revision == 2
    assert replay.replayed is True
    assert [task.task_id for task in replay.tasks] == [task.task_id for task in result.tasks]
    assert task_store.collection_revision() == 2
    assert len(task_store.pending_outbox()) == 2


def test_event_failure_rolls_back_task_history_receipt_and_collection(task_store):
    from work_buddy.tasks import runtime

    state = task_store.system_state()
    runtime.arm_native_authority_latch(
        task_store.path,
        cohort_id="event-failure-test",
        target_authority_epoch="native:event-failure-test",
        cutover_receipt_id="event-failure-cutover",
        armed_at="2026-08-23T16:59:59+00:00",
    )
    task_store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:event-failure-test",
        updated_at="2026-08-23T17:00:00+00:00",
        cutover_receipt_id="event-failure-cutover",
        process_generation=1,
    )
    service = TaskApplicationService(
        task_store,
        event_id_factory=lambda: "same-event",
    )
    task = service.create(
        description="Atomic task",
        client_mutation_id="atomic-create",
        actor="agent:test",
        task_id="t-atomic",
    ).task
    with pytest.raises(sqlite3.IntegrityError):
        service.update(
            task.task_id,
            expected_revision=task.revision,
            client_mutation_id="atomic-update",
            actor="agent:test",
            changes={"urgency": "high"},
        )
    current = task_store.get(task.task_id)
    assert current.revision == 1
    assert current.urgency == "medium"
    assert task_store.collection_revision() == 1
    conn = task_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_mutation_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_state_history").fetchone()[0] == 1
    finally:
        conn.close()
