from __future__ import annotations

import pytest

from work_buddy.tasks.errors import TaskValidationError

from .conftest import create_task


def _auth(task, mutation_id):
    return {
        "expected_revision": task.revision,
        "client_mutation_id": mutation_id,
        "actor": "dashboard:user",
    }


def test_action_item_lifecycle_is_parent_cas_guarded(task_service, task_store):
    task = create_task(task_service).task
    task = task_service.create_action_item(
        task.task_id,
        description="Write schema",
        **_auth(task, "action-create-1"),
    ).task
    first = task.action_items[0]
    assert first.sequence == 1
    assert task.revision == 2

    task = task_service.create_action_item(
        task.task_id,
        description="Run tests",
        authorship="agent_unapproved",
        **_auth(task, "action-create-2"),
    ).task
    second = task.action_items[1]

    task = task_service.reorder_action_items(
        task.task_id,
        action_item_ids=[second.id, first.id],
        **_auth(task, "action-reorder"),
    ).task
    assert [item.id for item in task.action_items] == [second.id, first.id]

    task = task_service.set_current_action_item(
        task.task_id,
        action_item_id=second.id,
        **_auth(task, "action-current"),
    ).task
    assert task.current_action_item_id == second.id

    task = task_service.approve_action_item(
        task.task_id,
        second.id,
        **_auth(task, "action-approve"),
    ).task
    assert task.action_items[0].authorship == "agent_approved"

    task = task_service.update_action_item(
        task.task_id,
        second.id,
        changes={"state": "done", "definition_of_done": "Tests pass"},
        **_auth(task, "action-done"),
    ).task
    assert task.action_items[0].state == "done"
    assert task.action_items[0].completed_at is not None

    task = task_service.delete_action_item(
        task.task_id,
        second.id,
        **_auth(task, "action-delete"),
    ).task
    assert [item.id for item in task.action_items] == [first.id]
    assert task.current_action_item_id is None

    task = task_service.reorder_action_items(
        task.task_id,
        action_item_ids=[first.id],
        **_auth(task, "action-reorder-after-delete"),
    ).task
    task = task_service.restore_action_item(
        task.task_id,
        second.id,
        **_auth(task, "action-restore"),
    ).task
    assert [item.id for item in task.action_items] == [first.id, second.id]
    assert len(task_store.pending_outbox()) == task_store.collection_revision()


def test_create_after_deleting_only_action_restarts_live_sequence(task_service):
    task = create_task(task_service).task
    task = task_service.create_action_item(
        task.task_id,
        description="Discard me",
        **_auth(task, "action-sequence-create-1"),
    ).task
    item = task.action_items[0]
    task = task_service.delete_action_item(
        task.task_id,
        item.id,
        **_auth(task, "action-sequence-delete"),
    ).task
    task = task_service.create_action_item(
        task.task_id,
        description="First live action",
        **_auth(task, "action-sequence-create-2"),
    ).task

    assert [action.sequence for action in task.action_items] == [1]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"definition_of_done": {"unexpected": True}}, "definition_of_done"),
        ({"risk_profile_json": ["not", "text"]}, "risk_profile_json"),
        ({"risk_profile_json": "[]"}, "risk_profile_json"),
        ({"agent_required_contexts": "not-a-list"}, "agent_required_contexts"),
    ],
)
def test_malformed_action_structured_fields_are_typed_validation_errors(
    task_service,
    changes,
    field,
):
    task = create_task(task_service).task
    task = task_service.create_action_item(
        task.task_id,
        description="Validate me",
        **_auth(task, f"action-validation-create-{field}"),
    ).task

    with pytest.raises(TaskValidationError) as caught:
        task_service.update_action_item(
            task.task_id,
            task.action_items[0].id,
            changes=changes,
            **_auth(task, f"action-validation-update-{field}"),
        )

    assert field in caught.value.field_errors
