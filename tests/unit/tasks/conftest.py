from __future__ import annotations

from datetime import datetime, timezone

import pytest

from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore


@pytest.fixture(autouse=True)
def _isolate_and_guard_installation_authority_latch(tmp_path, monkeypatch):
    """Keep Tasks unit tests from reading or writing the installation latch.

    Tests that make a temporary database the configured task store exercise the
    canonical-latch branch of the authority router.  Route that branch to the
    test directory and also prove that the real installation latch remains
    byte-for-byte unchanged throughout the test.
    """

    from work_buddy.tasks import runtime

    installation_latch = runtime._canonical_default_latch_path()
    before = installation_latch.read_bytes() if installation_latch.is_file() else None
    isolated_latch = tmp_path / "installation" / "task_authority_latch.json"
    monkeypatch.setattr(
        runtime,
        "_canonical_default_latch_path",
        lambda: isolated_latch,
    )

    yield

    after = installation_latch.read_bytes() if installation_latch.is_file() else None
    assert after == before, "Tasks unit test mutated the installation authority latch"


@pytest.fixture
def task_store(tmp_path):
    store = TaskStore(tmp_path / "task_metadata.db")
    store.initialize()
    return store


@pytest.fixture
def task_service(task_store):
    from work_buddy.tasks import runtime

    state = task_store.system_state()
    runtime.arm_native_authority_latch(
        task_store.path,
        cohort_id="unit-test",
        target_authority_epoch="native:unit-test",
        cutover_receipt_id="unit-test-cutover",
        armed_at="2026-08-23T16:59:59+00:00",
    )
    task_store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:unit-test",
        updated_at="2026-08-23T17:00:00+00:00",
        cutover_receipt_id="unit-test-cutover",
        process_generation=1,
    )
    return TaskApplicationService(
        task_store,
        clock=lambda: datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc),
    )


def create_task(service: TaskApplicationService, *, mutation_id: str = "create-1", **kwargs):
    return service.create(
        description=kwargs.pop("description", "Write migration tests"),
        client_mutation_id=mutation_id,
        actor="dashboard:user",
        task_id=kwargs.pop("task_id", "t-test-1"),
        **kwargs,
    )
