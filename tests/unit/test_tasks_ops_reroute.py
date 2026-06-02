"""The 7 task-mutator ops route through the WorkItem write port.

The dominant task-mutation surface — the MCP op registry — no longer points at
``obsidian.tasks.mutations`` directly: the mutator ops resolve to the Task-owned
write port (``work_item.task_adapter``) and the ``Task.create`` classmethod, so
no task mutation bypasses the WorkItem family. Reads, the bulk archive sweep,
and aggregates stay on the mutation layer. The ``task_create`` effect manifest
must remain registered (it is keyed by op id, independent of the callable).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def loaded_ops():
    """Reload the built-in ops into a clean registry (the established pattern:
    clear_ops + load_builtin_ops, so every ops module re-registers without
    tripping the duplicate-registration guard)."""
    from work_buddy.mcp_server import op_registry

    op_registry.clear_ops()
    op_registry.load_builtin_ops()
    yield op_registry
    op_registry.clear_ops()


def test_mutator_ops_route_through_the_work_item_port(loaded_ops):
    from work_buddy.threads.models import Task
    from work_buddy.work_item import task_adapter

    # Task.create is a classmethod — compare by equality (each attribute access
    # binds a fresh bound-method object that is == but not `is`).
    assert loaded_ops.get_op("op.wb.task_create") == Task.create
    # The verb ops resolve to the port's plain module functions (identity holds).
    assert loaded_ops.get_op("op.wb.task_toggle") is task_adapter.toggle
    assert loaded_ops.get_op("op.wb.task_change_state") is task_adapter.update
    assert loaded_ops.get_op("op.wb.task_update_description") is task_adapter.set_description
    assert loaded_ops.get_op("op.wb.task_set_tags") is task_adapter.set_tags
    assert loaded_ops.get_op("op.wb.task_delete") is task_adapter.delete
    assert loaded_ops.get_op("op.wb.task_assign") is task_adapter.assign


def test_read_and_archive_ops_stay_on_mutations(loaded_ops):
    """Reads and the bulk archive sweep are NOT rerouted — they are not per-task
    mutations through the Task write surface."""
    from work_buddy.obsidian.tasks import mutations

    assert loaded_ops.get_op("op.wb.task_read") is mutations.read_task
    assert loaded_ops.get_op("op.wb.task_archive") is mutations.archive_completed


def test_create_effect_manifest_preserved(loaded_ops):
    """The task_create effect manifest survives the repoint (keyed by op id),
    and both effects still resolve via the mutation layer's idempotency-cache
    resolver — the create path still flows through mutations.create_task."""
    from work_buddy.obsidian.tasks import mutations

    effects = loaded_ops.get_op_effects("op.wb.task_create")
    assert len(effects) == 2
    for spec in effects:
        assert spec.resolver is mutations.create_task_effects_resolver


def test_toggle_op_dispatches_through_to_mutations(loaded_ops):
    """End-to-end: invoking the registered toggle op reaches
    ``mutations.toggle_task`` (the port is a pass-through) — proving the reroute
    is wired, not merely named."""
    from work_buddy.obsidian.tasks import mutations

    op = loaded_ops.get_op("op.wb.task_toggle")
    with patch.object(mutations, "toggle_task", return_value={"success": True}) as m:
        result = op(task_id="t-route01", done=True)
    assert result == {"success": True}
    m.assert_called_once_with(
        "t-route01", done=True, file_path=None, done_date=None,
    )
