from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from work_buddy.collectors.obsidian_collector import _get_tasks
from work_buddy.projects.sync import _scan_task_projects
from work_buddy.tasks import runtime
from work_buddy.tasks import capabilities as native_capabilities
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore


def test_project_and_context_collectors_ignore_frozen_task_markdown_after_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    legacy = vault / "tasks" / "master-task-list.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "- [ ] Frozen legacy task #projects/legacy\n",
        encoding="utf-8",
    )
    task_path = tmp_path / "tasks.db"
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: task_path)
    monkeypatch.setattr(
        runtime,
        "_canonical_default_latch_path",
        lambda: tmp_path / "task_authority_latch.json",
    )
    monkeypatch.setattr(
        "work_buddy.tasks.store.default_task_db_path",
        lambda: task_path,
    )
    store = TaskStore(task_path)
    store.initialize()
    state = store.system_state()
    runtime.arm_native_authority_latch(
        store.path,
        cohort_id="secondary-consumers-test",
        target_authority_epoch="native:1",
        cutover_receipt_id="secondary-consumers-cutover",
        armed_at="2026-08-23T11:59:59+00:00",
    )
    store.set_system_state(
        expected_authority_epoch=state.authority_epoch,
        authority_epoch="native:1",
        updated_at="2026-08-23T12:00:00+00:00",
        cutover_receipt_id="secondary-consumers-cutover",
        process_generation=1,
    )
    service = TaskApplicationService(store)
    service.create(
        description="Native open task",
        task_id="t-native-open",
        state="active",
        tags=[("projects/native", True)],
        client_mutation_id="create-native-open",
        actor="human:test",
    )
    completed = service.create(
        description="Native completed task",
        task_id="t-native-done",
        state="inbox",
        tags=[("projects/native", True)],
        client_mutation_id="create-native-done",
        actor="human:test",
    ).task
    service.complete(
        completed.task_id,
        expected_revision=completed.revision,
        client_mutation_id="complete-native-done",
        actor="human:test",
    )

    monkeypatch.setattr(runtime, "default_task_db_path", lambda: store.path)
    monkeypatch.setattr(
        "work_buddy.tasks.store.default_task_db_path",
        lambda: store.path,
    )

    summary = _get_tasks(vault)
    assert "Native open task" in summary
    assert "Native completed task" not in summary
    assert "Frozen legacy task" not in summary
    assert _scan_task_projects(vault) == {
        "native": {"open": 1, "done": 1}
    }


def test_task_new_enrichment_uses_native_tag_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = Counter(
        {
            "projects/work-buddy/systems/tasks": 4,
            "admin/uhn": 2,
        }
    )
    monkeypatch.setattr(native_capabilities, "_namespace_counts", lambda: counts)
    monkeypatch.setattr(
        "work_buddy.projects.store.list_projects",
        lambda: [{"slug": "work-buddy", "name": "Work Buddy", "status": "active"}],
    )

    result = native_capabilities.enrich_plan(
        {
            "task_text": "Improve the task system",
            "project": "work-buddy",
            "proposed_tags": ["projects/work-buddy/systems/tasks", "admin/new"],
        }
    )

    assert result["project_status"]["slug_exists"] is True
    assert result["project_status"]["near_subtrees"] == [
        "projects/work-buddy/systems/tasks"
    ]
    assert result["tag_status"]["projects/work-buddy/systems/tasks"]["exists"] is True
    assert result["tag_status"]["admin/new"]["exists"] is False
