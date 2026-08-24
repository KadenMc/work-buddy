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
def loaded_ops(monkeypatch):
    """Reload the built-in ops into a clean registry (the established pattern:
    clear_ops + load_builtin_ops, so every ops module re-registers without
    tripping the duplicate-registration guard)."""
    from work_buddy.mcp_server import op_registry
    from work_buddy.tasks import runtime

    monkeypatch.setattr(runtime, "native_authority_active", lambda: False)
    monkeypatch.setattr(runtime, "native_task_mutation_authority", lambda: False)

    op_registry.clear_ops()
    op_registry.load_builtin_ops()
    yield op_registry
    # Restore a *loaded* registry rather than leaving it empty: some tests
    # assume the built-in ops are already registered and don't reload them
    # themselves, so an empty-registry teardown is a cross-test landmine.
    op_registry.clear_ops()
    op_registry.load_builtin_ops()


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


def test_reads_and_archive_resolve_authority_at_invocation(loaded_ops):
    """Pre-cutover registration cannot retain a direct legacy callable."""
    from work_buddy.mcp_server.ops import tasks_ops
    from work_buddy.obsidian.tasks import mutations

    assert loaded_ops.get_op("op.wb.task_read") is tasks_ops._task_read
    assert loaded_ops.get_op("op.wb.task_archive") is tasks_ops._compat_archive_completed

    archive = loaded_ops.get_op("op.wb.task_archive")
    with (
        patch(
            "work_buddy.tasks.runtime.native_task_mutation_authority",
            return_value=False,
        ) as authority,
        patch.object(mutations, "archive_completed", return_value={"success": True}) as legacy,
    ):
        result = archive(days=7)
    assert result == {"success": True}
    authority.assert_called_once_with()
    legacy.assert_called_once_with(days=7)


def test_create_effect_manifest_preserved(loaded_ops):
    """The task_create effect manifest survives the repoint (keyed by op id),
    and both effects lazily resolve via the mutation layer's idempotency-cache
    resolver — registry construction never imports or probes the retired
    writer."""
    from work_buddy.mcp_server.ops import tasks_ops
    from work_buddy.obsidian.tasks import mutations

    effects = loaded_ops.get_op_effects("op.wb.task_create")
    assert len(effects) == 2
    for spec in effects:
        assert spec.resolver is tasks_ops._compat_task_create_effects_resolver

    expected = {"task_id": "t-effect", "note_uuid": "note-effect"}
    with patch.object(
        mutations,
        "create_task_effects_resolver",
        return_value=expected,
    ) as legacy_resolver:
        assert effects[0].resolver({"task_text": "Legacy task"}) == expected
    legacy_resolver.assert_called_once_with({"task_text": "Legacy task"})


def test_registration_does_not_probe_task_authority(loaded_ops):
    """A corrupt/mismatched latch cannot leave the op registry half-built."""
    from work_buddy.mcp_server.ops import tasks_ops

    loaded_ops.clear_ops()
    with (
        patch(
            "work_buddy.tasks.runtime.native_authority_active",
            side_effect=AssertionError("authority read during registration"),
        ),
        patch(
            "work_buddy.tasks.runtime.native_task_mutation_authority",
            side_effect=AssertionError("mutation authority read during registration"),
        ),
    ):
        tasks_ops._register()

    assert loaded_ops.get_op("op.wb.task_read") is tasks_ops._task_read
    assert len(loaded_ops.get_op_effects("op.wb.task_create")) == 2


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
        task_id="t-route01", done=True, file_path=None, done_date=None,
    )
