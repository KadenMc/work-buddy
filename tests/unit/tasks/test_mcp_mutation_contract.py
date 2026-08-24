from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from work_buddy.tasks.errors import TaskRevisionConflict
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore


@pytest.fixture()
def native_contract_store(tmp_path, monkeypatch):
    from work_buddy.tasks import events, runtime
    from work_buddy.tasks import store as native_store

    path = tmp_path / "native-tasks.sqlite3"
    monkeypatch.setattr(native_store, "default_task_db_path", lambda: path)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: path)
    monkeypatch.setattr(
        runtime,
        "_canonical_default_latch_path",
        lambda: tmp_path / "task_authority_latch.json",
    )
    monkeypatch.setattr(events, "publish_pending_async", lambda _store: None)
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "mcp-contract-session")

    store = TaskStore(path)
    store.initialize()
    current = store.system_state()
    runtime.arm_native_authority_latch(
        path,
        cohort_id="mcp-contract-test",
        target_authority_epoch="native:mcp-contract-test",
        cutover_receipt_id="mcp-contract-cutover",
        armed_at=datetime.now(timezone.utc).isoformat(),
    )
    store.set_system_state(
        expected_authority_epoch=current.authority_epoch,
        authority_epoch="native:mcp-contract-test",
        updated_at=datetime.now(timezone.utc).isoformat(),
        cutover_receipt_id="mcp-contract-cutover",
        process_generation=1,
    )
    return store


def _create(store: TaskStore, *, task_id: str, description: str = "Native MCP task"):
    return TaskApplicationService(store).create(
        description=description,
        task_id=task_id,
        client_mutation_id=f"create:{task_id}",
        actor="agent:mcp-contract",
    ).task


def test_gateway_create_response_loss_reuses_persisted_mutation_id(
    native_contract_store,
    tmp_path,
    monkeypatch,
):
    from work_buddy.mcp_server import registry
    from work_buddy.mcp_server.tools import gateway
    from work_buddy.obsidian.tasks import mutations as legacy_mutations
    from work_buddy.threads.models import Task

    operations_dir = tmp_path / "create-operations"
    operations_dir.mkdir()
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", operations_dir)
    monkeypatch.setattr(
        legacy_mutations,
        "create_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy write")),
    )
    prepared = gateway._prepare_task_mutation_params(
        "task_create",
        {"task_text": "Create once across a lost response"},
    )
    operation_id = gateway._save_operation(
        "task_create",
        prepared,
        "verify_first",
        lease_seconds=0,
    )

    first = Task.create(**prepared)
    assert first["replayed"] is False
    gateway._complete_operation(operation_id, error="response lost")

    capability = registry.Capability(
        name="task_create",
        description="test create",
        category="tasks",
        parameters={},
        callable=Task.create,
        mutates_state=True,
        retry_policy="verify_first",
    )
    monkeypatch.setattr(registry, "get_entry", lambda _name: capability)
    replay = gateway.retry_operation(operation_id)

    assert replay["result"]["replayed"] is True
    assert replay["result"]["task_id"] == first["task_id"]
    assert native_contract_store.collection_revision() == 1
    assert len(native_contract_store.list()) == 1
    saved = json.loads(
        (operations_dir / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    assert saved["params"]["client_mutation_id"] == prepared["client_mutation_id"]


def test_gateway_response_loss_replays_same_revision_and_mutation_id(
    native_contract_store,
    tmp_path,
    monkeypatch,
):
    """A successful write with a lost response must not execute twice."""
    from work_buddy.mcp_server import registry
    from work_buddy.mcp_server.tools import gateway
    from work_buddy.obsidian.tasks import mutations as legacy_mutations
    from work_buddy.work_item import task_adapter

    task = _create(native_contract_store, task_id="t-mcp-replay")
    operations_dir = tmp_path / "operations"
    operations_dir.mkdir()
    monkeypatch.setattr(gateway, "_OPERATIONS_DIR", operations_dir)
    monkeypatch.setattr(
        legacy_mutations,
        "toggle_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy write")),
    )

    prepared = gateway._prepare_task_mutation_params(
        "task_toggle",
        {"task_id": task.task_id},
    )
    assert prepared["expected_revision"] == task.revision
    assert prepared["client_mutation_id"].startswith("mcp:")

    operation_id = gateway._save_operation(
        "task_toggle",
        prepared,
        "verify_first",
        lease_seconds=0,
    )
    first = task_adapter.toggle(**prepared)
    assert first["replayed"] is False
    assert first["revision"] == 2

    # Model a transport failure after the application service committed but
    # before the gateway could persist/deliver the success response.
    gateway._complete_operation(operation_id, error="response lost")
    capability = registry.Capability(
        name="task_toggle",
        description="test toggle",
        category="tasks",
        parameters={},
        callable=task_adapter.toggle,
        mutates_state=True,
        retry_policy="verify_first",
    )
    monkeypatch.setattr(registry, "get_entry", lambda _name: capability)

    replay = gateway.retry_operation(operation_id)

    assert replay["type"] == "result"
    assert replay["result"]["replayed"] is True
    assert replay["result"]["revision"] == 2
    assert native_contract_store.collection_revision() == 2
    assert len(native_contract_store.history(task.task_id)) == 2
    saved = json.loads(
        (operations_dir / f"{operation_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["params"] == prepared


def test_native_mcp_mutation_rejects_stale_expected_revision(
    native_contract_store,
    monkeypatch,
):
    from work_buddy.mcp_server.tools import gateway
    from work_buddy.obsidian.tasks import mutations as legacy_mutations
    from work_buddy.work_item import task_adapter

    task = _create(native_contract_store, task_id="t-mcp-stale")
    monkeypatch.setattr(
        legacy_mutations,
        "update_task_description",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy write")),
    )
    changed = task_adapter.set_description(
        task.task_id,
        "First concurrent edit",
        expected_revision=task.revision,
        client_mutation_id="mcp-stale:first",
    )
    assert changed["revision"] == 2

    with pytest.raises(TaskRevisionConflict) as conflict:
        task_adapter.set_description(
            task.task_id,
            "Stale overwrite",
            expected_revision=task.revision,
            client_mutation_id="mcp-stale:second",
        )

    assert conflict.value.expected_revision == 1
    assert conflict.value.current_revision == 2
    assert native_contract_store.get(task.task_id).description == "First concurrent edit"
    payload = gateway._task_domain_error_payload(conflict.value, "op-stale")
    assert payload is not None
    assert payload["code"] == "task_revision_conflict"
    assert payload["current_revision"] == 2
    assert payload["current_task"]["description"] == "First concurrent edit"


def test_native_toggle_preserves_retroactive_done_date(
    native_contract_store,
    monkeypatch,
):
    from work_buddy.obsidian.tasks import mutations as legacy_mutations
    from work_buddy.work_item import task_adapter

    task = _create(native_contract_store, task_id="t-mcp-done-date")
    monkeypatch.setattr(
        legacy_mutations,
        "toggle_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy write")),
    )

    completed = task_adapter.toggle(
        task.task_id,
        done=True,
        done_date="2026-08-05",
        expected_revision=task.revision,
        client_mutation_id="mcp-done-date:complete",
    )

    assert completed["task"]["state"] == "done"
    assert completed["task"]["completed_at"] == "2026-08-05"
    assert "done_date_ignored" not in completed


def test_idempotent_toggle_reopens_to_original_attention_state(
    native_contract_store,
):
    service = TaskApplicationService(native_contract_store)
    task = _create(native_contract_store, task_id="t-mcp-toggle-resume")
    task = service.set_state(
        task.task_id,
        state="mit",
        expected_revision=task.revision,
        client_mutation_id="mcp-toggle-resume:mit",
        actor="agent:mcp-contract",
    ).task
    authority = {
        "expected_revision": task.revision,
        "client_mutation_id": "mcp-toggle-resume:complete",
        "actor": "agent:mcp-contract",
    }
    completed = service.toggle(task.task_id, **authority)
    replay = service.toggle(task.task_id, **authority)
    assert replay.replayed is True
    assert replay.task.revision == completed.task.revision

    reopened = service.toggle(
        task.task_id,
        expected_revision=completed.task.revision,
        client_mutation_id="mcp-toggle-resume:reopen",
        actor="agent:mcp-contract",
    )
    assert reopened.task.state == "mit"


def test_task_change_state_snoozes_with_until_and_exact_receipt_key(
    native_contract_store,
    monkeypatch,
):
    from work_buddy.obsidian.tasks import mutations as legacy_mutations
    from work_buddy.work_item import task_adapter

    task = _create(native_contract_store, task_id="t-mcp-snooze")
    monkeypatch.setattr(
        legacy_mutations,
        "update_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy write")),
    )

    snoozed = task_adapter.update(
        task.task_id,
        state="snoozed",
        snooze_until="2026-09-01T09:00:00-04:00",
        expected_revision=task.revision,
        client_mutation_id="mcp-snooze:one",
    )

    assert snoozed["task"]["state"] == "snoozed"
    assert snoozed["task"]["snooze_until"] == "2026-09-01T09:00:00-04:00"
    assert snoozed["receipt"]["client_mutation_id"] == "mcp-snooze:one"
