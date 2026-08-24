"""Native task authority disconnects health and legacy dashboard surfaces."""

from __future__ import annotations


def test_native_health_checks_do_not_inspect_vault(monkeypatch):
    from work_buddy.health import requirement_checks as checks

    monkeypatch.setattr(checks, "frozen_task_compatibility_required", lambda: False)

    def touched_vault():
        raise AssertionError("native task health must not inspect the vault")

    monkeypatch.setattr(checks, "_vault_root", touched_vault)

    master = checks.check_master_task_list()
    plugin = checks.check_tasks_plugin()

    assert master["ok"] is True
    assert plugin["ok"] is True
    assert "not inspected" in master["detail"]
    assert "not inspected" in plugin["detail"]


def test_native_health_sweeps_omit_legacy_task_requirements(monkeypatch):
    from work_buddy.health import requirement_checks as checks
    from work_buddy.health.requirements import RequirementChecker

    monkeypatch.setattr(checks, "frozen_task_compatibility_required", lambda: False)

    task_results = RequirementChecker().check_group("tasks")
    obsidian_results = RequirementChecker().check_component("obsidian")
    ids = {result.id for result in [*task_results, *obsidian_results]}

    assert "obsidian/tasks/master-list-exists" not in ids
    assert "obsidian/plugins/tasks-plugin" not in ids


def test_native_master_list_fixer_is_a_noop(monkeypatch):
    from work_buddy.health import fixers
    from work_buddy.health import requirement_checks as checks

    monkeypatch.setattr(checks, "frozen_task_compatibility_required", lambda: False)

    def touched_vault():
        raise AssertionError("native task fixer must not inspect or write the vault")

    monkeypatch.setattr(fixers, "_vault_root", touched_vault)

    result = fixers.fix_master_task_list()

    assert result["ok"] is True
    assert result["side_effects"] == []
    assert "No action" in result["detail"]


def test_native_tasks_plugin_fix_does_not_spawn_agent(monkeypatch):
    from work_buddy.control.fix_runner import run_fix
    from work_buddy.health import requirement_checks as checks
    from work_buddy import session_launcher

    monkeypatch.setattr(checks, "frozen_task_compatibility_required", lambda: False)

    def spawned(**_kwargs):
        raise AssertionError("retired Tasks-plugin fixer must not spawn")

    monkeypatch.setattr(session_launcher, "begin_session", spawned)

    result = run_fix("obsidian/plugins/tasks-plugin")

    assert result["ok"] is True
    assert result["spawned"] is None
    assert "not applicable" in result["detail"]


def test_health_topology_has_no_obsidian_task_dependency():
    from work_buddy.control.graph_static import SUBSYSTEMS
    from work_buddy.health.components import COMPONENT_CATALOG

    obsidian_requirements = set(COMPONENT_CATALOG["obsidian"].requirements)
    assert "obsidian/tasks/master-list-exists" not in obsidian_requirements
    assert "obsidian/plugins/tasks-plugin" not in obsidian_requirements

    lifecycle = next(
        node for node in SUBSYSTEMS if node["id"] == "subsystem:task-lifecycle"
    )
    assert lifecycle.get("component_deps") == []
    assert lifecycle.get("requirement_ids") == []
    assert "TaskStore" in lifecycle["description"]
    assert "Obsidian" not in lifecycle["description"]


def test_native_dashboard_summary_never_reads_frozen_markdown(monkeypatch, tmp_path):
    from work_buddy.dashboard import api as dashboard_api
    from work_buddy.tasks import store as task_store

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "master-task-list.md").write_text(
        "- [ ] must stay frozen #todo/inbox\n",
        encoding="utf-8",
    )

    class EmptyStore:
        def list(self, _query):
            return []

    monkeypatch.setattr(dashboard_api, "_task_authority_mode", lambda: "native")
    monkeypatch.setattr(dashboard_api, "_cfg", {"vault_root": str(tmp_path)})
    monkeypatch.setattr(task_store, "TaskStore", EmptyStore)

    result = dashboard_api.get_tasks_summary()

    assert result["authority"] == "native"
    assert result["tasks"] == []


def test_unavailable_dashboard_authority_does_not_open_task_store(monkeypatch):
    from work_buddy.dashboard import api as dashboard_api
    from work_buddy.tasks import store as task_store

    class ForbiddenStore:
        def __init__(self):
            raise AssertionError("unavailable authority must not open TaskStore")

    monkeypatch.setattr(
        dashboard_api,
        "_task_authority_mode",
        lambda: "unavailable",
    )
    monkeypatch.setattr(task_store, "TaskStore", ForbiddenStore)

    result = dashboard_api.get_tasks_summary()

    assert result["authority"] == "unavailable"
    assert result["tasks"] == []
    assert result["error"] == "Task data is temporarily unavailable."


def test_native_dashboard_sync_is_retired_before_legacy_import(monkeypatch):
    from work_buddy.dashboard import service
    from work_buddy.obsidian.tasks import sync as legacy_sync
    from work_buddy.tasks import runtime

    monkeypatch.setattr(service, "_is_read_only", lambda: False)
    monkeypatch.setattr(runtime, "native_authority_active", lambda: True)
    monkeypatch.setattr(runtime, "mutation_fence_active", lambda: False)

    def called_legacy_sync():
        raise AssertionError("native authority must not import or run task_sync")

    monkeypatch.setattr(legacy_sync, "task_sync", called_legacy_sync)

    response = service.app.test_client().post("/api/task_sync")

    assert response.status_code == 410
    assert response.get_json()["error"]["code"] == "task_legacy_sync_retired"


def test_unavailable_dashboard_sync_fails_closed(monkeypatch):
    from work_buddy.dashboard import service
    from work_buddy.tasks import runtime

    monkeypatch.setattr(service, "_is_read_only", lambda: False)

    def unavailable():
        raise RuntimeError("native task store is unavailable")

    monkeypatch.setattr(runtime, "native_authority_active", unavailable)

    response = service.app.test_client().post("/api/task_sync")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "task_authority_unavailable"
    assert response.get_json()["error"]["message"] == (
        "Task data is temporarily unavailable."
    )
