from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from work_buddy.tasks import runtime
from work_buddy.tasks import store as native_store
from work_buddy.tasks.store import TaskStore


@pytest.fixture()
def native_runtime(tmp_path, monkeypatch):
    path = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(native_store, "default_task_db_path", lambda: path)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: path)
    monkeypatch.setattr(
        runtime,
        "_canonical_default_latch_path",
        lambda: tmp_path / "task_authority_latch.json",
    )
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "native-test-session")
    store = TaskStore(path)
    store.initialize()
    current = store.system_state()
    runtime.arm_native_authority_latch(
        path,
        cohort_id="native-compatibility-test",
        target_authority_epoch="native:test",
        cutover_receipt_id="cutover-test",
        armed_at=datetime.now(timezone.utc).isoformat(),
    )
    store.set_system_state(
        expected_authority_epoch=current.authority_epoch,
        authority_epoch="native:test",
        updated_at=datetime.now(timezone.utc).isoformat(),
        cutover_receipt_id="cutover-test",
        process_generation=1,
    )
    return store


def test_work_item_port_uses_native_store_without_legacy_mutation(
    native_runtime,
    monkeypatch,
):
    from work_buddy.obsidian.tasks import mutations
    from work_buddy.work_item import task_adapter

    monkeypatch.setattr(
        mutations,
        "create_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy write")),
    )
    created = task_adapter.create(
        "Author in the neutral store",
        urgency="high",
        tags=["systems/tasks"],
        client_mutation_id="compat-create-1",
    )
    assert created["success"] is True
    task_id = created["task_id"]
    task = native_runtime.get(task_id)
    assert task is not None
    assert task.description == "Author in the neutral store"
    assert task.namespace_tags == ("systems/tasks",)

    changed = task_adapter.update(
        task_id,
        state="active",
        urgency="medium",
        expected_revision=task.revision,
        client_mutation_id="compat-update-1",
    )
    assert changed["task"]["state"] == "active"
    assert changed["task"]["urgency"] == "medium"

    completed = task_adapter.toggle(
        task_id,
        done=True,
        expected_revision=changed["revision"],
        client_mutation_id="compat-complete-1",
    )
    assert completed["task"]["state"] == "done"


def test_task_facade_reads_native_authority(native_runtime):
    from work_buddy.tasks.service import TaskApplicationService
    from work_buddy.threads.models import Task

    created = TaskApplicationService(native_runtime).create(
        description="Facade without Obsidian",
        client_mutation_id="facade-native-create",
        actor="agent:test",
    ).task
    loaded = Task.load(created.task_id)
    assert loaded is not None
    assert loaded.description == "Facade without Obsidian"
    assert [task.thread_id for task in Task.query(state="inbox")] == [created.task_id]


def test_task_facade_unfiltered_query_preserves_legacy_state_coverage(native_runtime):
    from work_buddy.tasks.service import TaskApplicationService
    from work_buddy.threads.models import Task

    service = TaskApplicationService(native_runtime)
    done = service.create(
        description="Completed row",
        client_mutation_id="facade-all-done-create",
        actor="agent:test",
    ).task
    service.complete(
        done.task_id,
        expected_revision=done.revision,
        client_mutation_id="facade-all-done-complete",
        actor="agent:test",
    )
    snoozed = service.create(
        description="Snoozed row",
        client_mutation_id="facade-all-snoozed-create",
        actor="agent:test",
    ).task
    service.snooze(
        snoozed.task_id,
        until="2026-08-30",
        expected_revision=snoozed.revision,
        client_mutation_id="facade-all-snoozed-snooze",
        actor="agent:test",
    )

    assert {item.thread_id for item in Task.query()} == {
        done.task_id,
        snoozed.task_id,
    }


def test_native_mcp_registration_keeps_markdown_effects_dormant(native_runtime):
    from work_buddy.mcp_server import op_registry
    from work_buddy.mcp_server.ops import tasks_ops
    from work_buddy.obsidian.tasks import mutations

    op_registry.clear_ops()
    try:
        op_registry.load_builtin_ops()
        assert op_registry.get_op("op.wb.task_read") is tasks_ops._task_read
        assert op_registry.get_op("op.wb.task_sync") is tasks_ops._compat_task_sync
        assert op_registry.get_op("op.wb.task_scattered") is tasks_ops._task_scattered
        effects = op_registry.get_op_effects("op.wb.task_create")
        assert len(effects) == 2
        with patch.object(
            mutations,
            "create_task_effects_resolver",
            side_effect=AssertionError("native runtime reached legacy resolver"),
        ) as legacy_resolver:
            assert all(
                effect.call_resolver({"task_text": "Native task"}) is None
                for effect in effects
            )
        legacy_resolver.assert_not_called()
    finally:
        op_registry.clear_ops()
        # The fixture's path remains active until teardown. Restore a loaded
        # legacy registry explicitly for tests that assume built-ins exist.
        original = runtime.native_authority_active
        runtime.native_authority_active = lambda _path=None: False
        try:
            op_registry.load_builtin_ops()
        finally:
            runtime.native_authority_active = original


def test_registry_built_before_cutover_routes_reads_native_after_cutover(
    native_runtime,
    monkeypatch,
):
    """A stale registry callable must not retain a legacy reader."""
    from work_buddy.mcp_server.ops import tasks_ops
    from work_buddy.tasks import capabilities

    expected = {"task_id": "t-native", "source": "native"}
    monkeypatch.setattr(capabilities, "task_read", lambda task_id: expected)
    monkeypatch.setattr(
        tasks_ops,
        "import_module",
        lambda _name: (_ for _ in ()).throw(AssertionError("legacy import")),
    )

    assert tasks_ops._task_read("t-native") == expected


def test_task_facade_discards_legacy_cache_when_authority_changes(
    native_runtime,
    monkeypatch,
):
    from work_buddy.tasks.service import TaskApplicationService
    from work_buddy.threads.models import Task

    created = TaskApplicationService(native_runtime).create(
        task_id="t-cacheboundary",
        description="Native row",
        client_mutation_id="native-cache-row",
        actor="agent:test",
    ).task
    facade = Task.from_store_row(
        {"task_id": created.task_id, "description": "Frozen legacy row"},
        authority_epoch="legacy",
    )
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.store.get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy read")),
    )

    assert facade.description == "Native row"


def test_native_authority_rejects_unstamped_legacy_retry(native_runtime):
    from work_buddy.tasks.errors import TaskReplayAuthorityMismatch

    with pytest.raises(TaskReplayAuthorityMismatch):
        runtime.assert_task_replay_authority(None)
    runtime.assert_task_replay_authority("native:1")


def test_proposal_maintenance_obeys_the_existing_native_replay_authority_guard(
    native_runtime,
):
    from work_buddy.sidecar.retry_sweep import _assert_task_replay_boundary
    from work_buddy.tasks.errors import TaskReplayAuthorityMismatch

    assert runtime.is_task_mutation_capability("task_proposals_reconcile")
    with pytest.raises(TaskReplayAuthorityMismatch):
        _assert_task_replay_boundary({"name": "task_proposals_reconcile"})
    _assert_task_replay_boundary(
        {"name": "task_proposals_reconcile", "task_authority_epoch": "native:1"}
    )


def test_maintenance_fence_never_falls_back_to_legacy_writer(
    native_runtime,
    monkeypatch,
):
    from work_buddy.obsidian.tasks import mutations
    from work_buddy.tasks.errors import TaskMutationFenced
    from work_buddy.work_item import task_adapter

    current = native_runtime.system_state()
    native_runtime.set_system_state(
        expected_authority_epoch=current.authority_epoch,
        authority_epoch=current.authority_epoch,
        updated_at=datetime.now(timezone.utc).isoformat(),
        rollback_fence=True,
        process_generation=current.process_generation,
    )
    monkeypatch.setattr(
        mutations,
        "create_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy write")),
    )

    with pytest.raises(TaskMutationFenced):
        task_adapter.create(
            "Must remain blocked",
            client_mutation_id="fenced-create",
        )

    # Reads remain on the native authority while writes are fenced.
    from work_buddy.threads.models import Task

    assert Task.query(state="inbox") == []


def test_authority_probe_fails_closed_for_a_corrupt_existing_database(tmp_path):
    from work_buddy.tasks.errors import TaskAuthorityUnavailable

    path = tmp_path / "corrupt-tasks.db"
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(TaskAuthorityUnavailable):
        runtime.native_authority_active(path)

    with pytest.raises(TaskAuthorityUnavailable):
        runtime.assert_task_mutations_allowed(path)


def test_missing_database_without_activation_latch_remains_legacy(tmp_path):
    path = tmp_path / "never-activated.db"

    assert runtime.authority_epoch(path) == "legacy"
    assert runtime.native_authority_active(path) is False
    runtime.assert_task_mutations_allowed(path)


def test_marker_before_database_commit_is_a_safe_fail_closed_state(tmp_path):
    from work_buddy.tasks.errors import TaskAuthorityUnavailable

    path = tmp_path / "pending-activation.db"
    TaskStore(path).initialize()
    runtime.arm_native_authority_latch(
        path,
        cohort_id="pending-cohort",
        target_authority_epoch="native:1",
        cutover_receipt_id="pending-cutover-receipt",
        armed_at="2026-08-23T12:00:00+00:00",
    )

    with pytest.raises(TaskAuthorityUnavailable):
        runtime.native_authority_active(path)
    with pytest.raises(TaskAuthorityUnavailable):
        runtime.assert_task_mutations_allowed(path)

    runtime.clear_pending_authority_latch(
        path,
        cohort_id="pending-cohort",
        target_authority_epoch="native:1",
    )
    assert runtime.authority_epoch(path) == "legacy"


def test_configured_database_relocation_cannot_escape_native_latch(
    tmp_path,
    monkeypatch,
):
    from work_buddy.tasks.errors import TaskAuthorityUnavailable

    path = tmp_path / "configured-tasks.db"
    latch_path = tmp_path / "installation-authority-latch.json"
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: path)
    monkeypatch.setattr(runtime, "_canonical_default_latch_path", lambda: latch_path)
    store = TaskStore(path)
    store.initialize()
    runtime.arm_native_authority_latch(
        path,
        cohort_id="activated-cohort",
        target_authority_epoch="native:7",
        cutover_receipt_id="configured-cutover-receipt",
        armed_at="2026-08-23T12:00:00+00:00",
    )
    current = store.system_state()
    store.set_system_state(
        expected_authority_epoch=current.authority_epoch,
        authority_epoch="native:7",
        updated_at="2026-08-23T12:00:01+00:00",
        cutover_receipt_id="configured-cutover-receipt",
    )
    assert runtime.native_authority_active() is True

    moved = tmp_path / "relocated" / "tasks.db"
    moved.parent.mkdir()
    path.replace(moved)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: moved)

    with pytest.raises(TaskAuthorityUnavailable):
        runtime.assert_task_mutations_allowed()


@pytest.mark.parametrize("latch_bytes", [None, b"{not-json"])
def test_explicit_configured_database_requires_valid_canonical_latch(
    tmp_path,
    monkeypatch,
    latch_bytes,
):
    from work_buddy.tasks.errors import TaskAuthorityUnavailable

    path = tmp_path / "configured-tasks.db"
    latch_path = tmp_path / "installation-authority-latch.json"
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: path)
    monkeypatch.setattr(runtime, "_canonical_default_latch_path", lambda: latch_path)
    store = TaskStore(path)
    store.initialize()
    runtime.arm_native_authority_latch(
        path,
        cohort_id="explicit-path-cohort",
        target_authority_epoch="native:8",
        cutover_receipt_id="explicit-path-receipt",
        armed_at="2026-08-23T12:00:00+00:00",
    )
    current = store.system_state()
    store.set_system_state(
        expected_authority_epoch=current.authority_epoch,
        authority_epoch="native:8",
        updated_at="2026-08-23T12:00:01+00:00",
        cutover_receipt_id="explicit-path-receipt",
    )
    assert runtime.native_authority_active(path) is True

    if latch_bytes is None:
        latch_path.unlink()
    else:
        latch_path.write_bytes(latch_bytes)

    with pytest.raises(TaskAuthorityUnavailable):
        runtime.native_authority_active(path)
    with pytest.raises(TaskAuthorityUnavailable):
        runtime.native_task_mutation_authority(path)
