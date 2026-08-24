from __future__ import annotations

from datetime import datetime, timezone

import pytest

from work_buddy.tasks.errors import (
    TaskAuthorityUnavailable,
    TaskLegacyEffectRetired,
    TaskMutationFenced,
)
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore


def _activate(
    store: TaskStore,
    *,
    fenced: bool = False,
    epoch: str = "native:authority-boundary-test",
) -> None:
    from work_buddy.tasks import runtime

    state = store.system_state()
    runtime.arm_native_authority_latch(
        store.path,
        cohort_id="authority-boundary-test",
        target_authority_epoch=epoch,
        cutover_receipt_id="authority-boundary-cutover",
        armed_at=datetime.now(timezone.utc).isoformat(),
    )
    store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch=epoch,
        updated_at=datetime.now(timezone.utc).isoformat(),
        cutover_receipt_id="authority-boundary-cutover",
        rollback_fence=fenced,
        process_generation=state.process_generation + 1,
    )


def _native_write_counts(store: TaskStore) -> tuple[int, int]:
    conn = store.connect()
    try:
        return (
            int(conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0]),
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM task_mutation_receipts"
                ).fetchone()[0]
            ),
        )
    finally:
        conn.close()


def _stale_legacy_route(boundary: str):
    calls = 0

    def authority() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        if boundary == "activation":
            return True
        raise TaskMutationFenced()

    return authority


def test_service_rejects_direct_native_write_while_legacy_is_authoritative(
    tmp_path,
):
    store = TaskStore(tmp_path / "tasks.db")
    store.initialize()

    with pytest.raises(TaskAuthorityUnavailable):
        TaskApplicationService(store).create(
            description="Must not bypass the native authority fence",
            task_id="t-legacy-blocked",
            client_mutation_id="legacy-blocked",
            actor="agent:test",
        )

    assert _native_write_counts(store) == (0, 0)


@pytest.mark.parametrize("mutation", ["create", "batch_create"])
def test_service_checks_fence_inside_each_native_mutation_transaction(
    tmp_path,
    mutation,
):
    store = TaskStore(tmp_path / f"{mutation}.db")
    store.initialize()
    _activate(store, fenced=True)
    service = TaskApplicationService(store)

    with pytest.raises(TaskMutationFenced):
        if mutation == "create":
            service.create(
                description="Fenced native task",
                task_id="t-fenced",
                client_mutation_id="fenced-create",
                actor="agent:test",
            )
        else:
            service.batch_create(
                [{"description": "Fenced native batch task"}],
                client_mutation_id="fenced-batch",
                actor="agent:test",
            )

    assert _native_write_counts(store) == (0, 0)


@pytest.mark.parametrize(
    ("boundary", "error_type"),
    [
        ("activation", TaskLegacyEffectRetired),
        ("fence", TaskMutationFenced),
    ],
)
def test_mcp_sync_stale_legacy_route_rechecks_authority_before_reconcile(
    monkeypatch,
    boundary,
    error_type,
):
    from work_buddy.tasks import runtime

    monkeypatch.setattr(runtime, "native_authority_active", lambda: False)
    monkeypatch.setattr(
        runtime,
        "native_task_mutation_authority",
        _stale_legacy_route(boundary),
    )
    from work_buddy.mcp_server.ops import tasks_ops
    from work_buddy.obsidian.tasks import markdown_db

    monkeypatch.setattr(
        markdown_db,
        "reconcile_tasks",
        lambda: (_ for _ in ()).throw(AssertionError("legacy reconcile reached")),
    )

    with pytest.raises(error_type):
        tasks_ops._compat_task_sync()


def test_sync_rechecks_at_reconciler_writer_boundary(monkeypatch):
    from work_buddy.obsidian.tasks import markdown_db, sync
    from work_buddy.tasks import runtime

    snapshots = iter((False, True))
    monkeypatch.setattr(
        runtime,
        "native_task_mutation_authority",
        lambda: next(snapshots),
    )
    monkeypatch.setattr(
        markdown_db,
        "TaskMarkdownDB",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy reconciler constructed")
        ),
    )

    with pytest.raises(TaskLegacyEffectRetired):
        sync.task_sync()


@pytest.mark.parametrize(
    ("boundary", "error_type"),
    [
        ("activation", TaskLegacyEffectRetired),
        ("fence", TaskMutationFenced),
    ],
)
def test_work_item_stale_legacy_route_rechecks_authority_before_markdown_write(
    monkeypatch,
    boundary,
    error_type,
):
    from work_buddy.obsidian.tasks import mutations
    from work_buddy.tasks import runtime
    from work_buddy.work_item import task_adapter

    monkeypatch.setattr(
        runtime,
        "native_task_mutation_authority",
        _stale_legacy_route(boundary),
    )
    monkeypatch.setattr(
        mutations,
        "_find_and_replace_task_line",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy Markdown writer reached")
        ),
    )

    with pytest.raises(error_type):
        task_adapter.set_tags("t-stale-route", ["systems/tasks"])


@pytest.mark.parametrize(
    ("boundary", "status", "code"),
    [
        ("activation", 410, "task_legacy_effect_retired"),
        ("fence", 503, "task_mutation_fenced"),
    ],
)
def test_dashboard_sync_stale_legacy_route_fails_before_reconcile(
    monkeypatch,
    boundary,
    status,
    code,
):
    from work_buddy.dashboard import service
    from work_buddy.obsidian.tasks import markdown_db
    from work_buddy.tasks import runtime

    monkeypatch.setattr(service, "_is_read_only", lambda: False)
    monkeypatch.setattr(runtime, "native_authority_active", lambda: False)
    monkeypatch.setattr(runtime, "mutation_fence_active", lambda: False)
    if boundary == "activation":
        monkeypatch.setattr(
            runtime,
            "native_task_mutation_authority",
            lambda: True,
        )
    else:
        monkeypatch.setattr(
            runtime,
            "native_task_mutation_authority",
            lambda: (_ for _ in ()).throw(TaskMutationFenced()),
        )
    monkeypatch.setattr(
        markdown_db,
        "reconcile_tasks",
        lambda: (_ for _ in ()).throw(AssertionError("legacy reconcile reached")),
    )

    response = service.app.test_client().post("/api/task_sync")

    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code
