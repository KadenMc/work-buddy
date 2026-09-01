"""Regression coverage for the Obsidian retirement boundary.

These tests use synthetic probes/registries and repository declarations only.
They never inspect the user's vault, open a live database, or contact a bridge.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import threading
from unittest.mock import AsyncMock, MagicMock, patch
import weakref

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class _FakeSession:
    pass


class _FakeContext:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session


def _frontmatter(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    _, block, _ = raw.split("---", 2)
    return yaml.safe_load(block) or {}


def test_file_authority_jobs_are_inert_and_project_sync_is_authority_aware():
    from work_buddy.sidecar.scheduler.jobs import load_jobs

    jobs = {job.name: job for job in load_jobs(REPO / "sidecar_jobs", source="system")}
    assert jobs["vault-recon"].enabled is False
    assert jobs["inline-sync"].enabled is False
    assert jobs["journal-triage-scan"].enabled is False

    project_body = (REPO / "sidecar_jobs" / "project-sync.md").read_text(
        encoding="utf-8",
    )
    assert jobs["project-sync"].enabled is False
    assert "reconcile_projects_authoritatively" in project_body
    assert "zero writes" in project_body
    assert "final explicit reconciliation" in project_body


def test_retired_slash_commands_and_inline_workflow_are_not_declared():
    for name in (
        "wb-datacore-query.md",
        "wb-vault-recon.md",
        "wb-inline-todos.md",
        "wb-journal-backlog.md",
    ):
        assert not (REPO / ".claude" / "commands" / name).exists()

    assert not (REPO / "knowledge" / "store" / "tasks" / "inline-todos.md").exists()
    assert not (
        REPO / "knowledge" / "store" / "daily-journal" / "process-backlog.md"
    ).exists()
    from work_buddy.pipelines.capability import PIPELINES

    assert "journal_backlog" not in PIPELINES

    migration = _frontmatter(
        REPO
        / "knowledge"
        / "store"
        / "journal"
        / "journal-content-migration-operator.md"
    )
    assert migration["kind"] == "concept"
    assert "capability_name" not in migration
    for name in (
        "inline_cancel_watcher.md",
        "inline_invoke.md",
        "inline_list_commands.md",
        "inline_list_watchers.md",
        "inline_menu_manifest.md",
        "inline_sync.md",
        "inline_tag_removed.md",
    ):
        assert not (REPO / "knowledge" / "store" / "inline" / name).exists()

    for path in (
        REPO / "knowledge" / "store" / "obsidian" / "datacore-query-directions.md",
        REPO / "knowledge" / "store" / "vault" / "recon-directions.md",
        REPO / "knowledge" / "store" / "tasks" / "inline-todos-directions.md",
    ):
        assert "command" not in _frontmatter(path)

    overview = (REPO / "knowledge" / "store" / "inline" / "overview.md").read_text(
        encoding="utf-8",
    )
    assert "retired" in overview.lower()
    assert "Do not register new handlers" in overview

    obsidian = (REPO / "knowledge" / "store" / "obsidian.md").read_text(
        encoding="utf-8",
    )
    assert "legacy compatibility" in obsidian.lower()
    assert "does not\nprobe the bridge" in obsidian


def test_contract_capabilities_require_no_obsidian_and_name_sqlite_authority():
    declarations = (
        "active_contracts.md",
        "contract_constraints.md",
        "contract_health.md",
        "contract_wip_check.md",
        "contracts_summary.md",
        "overdue_contracts.md",
        "stale_contracts.md",
    )
    root = REPO / "knowledge" / "store" / "contracts"
    for name in declarations:
        frontmatter = _frontmatter(root / name)
        assert "obsidian" not in (frontmatter.get("requires") or [])
        description = str(frontmatter.get("description") or "").lower()
        assert "sqlite" in description


def test_retirement_manifest_is_complete_enough_to_audit():
    path = REPO / "docs" / "architecture" / "obsidian-retirement-manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "work-buddy/obsidian-retirement-manifest/v1"
    allowed = set(manifest["decisions"])
    assert allowed == {"keep", "retarget", "disable", "remove"}

    items = manifest["items"]
    assert len({item["id"] for item in items}) == len(items)
    for item in items:
        assert item["decision"] in allowed
        assert item["owner"]
        assert item["prerequisite"]
        assert item["status"]
        assert item["evidence"] and all(str(e).strip() for e in item["evidence"])

    by_id = {item["id"]: item for item in items}
    expected = {
        "job-vault-recon",
        "job-inline-sync",
        "job-project-sync",
        "job-journal-triage-scan",
        "journal-markdown-write-fence",
        "slash-datacore-query",
        "slash-vault-recon",
        "slash-inline-todos",
        "slash-journal-backlog",
        "journal-backlog-workflow",
        "journal-backlog-source-pipeline",
        "journal-content-migration-operator",
        "journal-backlog-actions",
        "inline-todos-workflow",
        "inline-capability-family",
        "health-calendar-native",
        "health-native-authorities",
        "notification-obsidian",
        "bridge-process-port",
        "retry-sidecar-replay",
        "obsidian-package",
        "context-bundle-legacy-sources",
        "day-planner-family",
        "hot-files-family",
        "activity-timeline-family",
        "keep-the-rhythm",
        "tag-wrangler",
        "vault-event-ledger",
        "native-search-partitions",
        "vault-index-authority-detachment",
    }
    assert expected <= by_id.keys()
    assert by_id["health-calendar-native"]["decision"] == "keep"
    assert by_id["obsidian-package"]["decision"] == "keep"
    assert by_id["native-search-partitions"]["decision"] == "retarget"
    assert by_id["vault-index-authority-detachment"]["status"] == "implemented"
    activity = by_id["activity-timeline-family"]
    assert activity["decision"] == "retarget"
    assert activity["status"] == "implemented"
    assert activity["owner"] == "journal-sqlite"
    assert activity["prerequisite"] == "journal-native-authority"
    assert all("requires obsidian" not in str(item).lower() for item in activity["evidence"])

    legacy_threads_js = (
        REPO / "work_buddy" / "dashboard" / "frontend" / "scripts" / "tabs"
        / "threads" / "main.py"
    ).read_text(encoding="utf-8")
    assert "threadsRunJournalScan" not in legacy_threads_js
    assert "source: 'journal_backlog'" not in legacy_threads_js


def test_retired_journal_migration_operator_fences_before_legacy_discovery(
    monkeypatch,
):
    from work_buddy.journal_capture.authority import JournalAuthorityStateError
    from work_buddy.mcp_server.ops import journal_migration_ops

    monkeypatch.setattr(
        "work_buddy.journal_capture.authority.existing_authority_mode",
        lambda: "database_only",
    )
    load = MagicMock(side_effect=AssertionError("configuration was accessed"))
    monkeypatch.setattr("work_buddy.config.load_config", load)

    with pytest.raises(
        JournalAuthorityStateError, match="retired Journal content migration"
    ):
        journal_migration_ops.journal_content_migration_operator("inventory")
    load.assert_not_called()


def test_app_only_capabilities_are_explicitly_obsidian_gated():
    declarations = (
        REPO / "knowledge" / "store" / "context" / "context_obsidian.md",
        REPO / "knowledge" / "store" / "context" / "context_wellness.md",
        REPO / "knowledge" / "store" / "journal" / "hot_files.md",
    )
    for path in declarations:
        frontmatter = _frontmatter(path)
        assert "obsidian" in (frontmatter.get("requires") or []), path
        assert "legacy" in str(frontmatter.get("description") or "").lower(), path


def test_update_journal_workflow_reads_through_native_authority_adapter():
    declaration = _frontmatter(
        REPO / "knowledge" / "store" / "daily-journal" / "update-journal.md"
    )

    read_step = declaration["steps"][0]
    assert read_step["id"] == "read-journal"
    assert (
        read_step["auto_run"]["callable"]
        == "work_buddy.journal_capture.native_ops.journal_state"
    )
    assert "work_buddy.journal.read_journal_state" not in str(declaration)


def test_hot_files_opt_out_stops_before_config_or_bridge_import(monkeypatch):
    from work_buddy.mcp_server import context_wrappers

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    load = MagicMock(side_effect=AssertionError("config accessed after opt-out"))
    monkeypatch.setattr("work_buddy.config.load_config", load)

    with pytest.raises(RuntimeError, match="disabled by preference"):
        context_wrappers._gather_hot_files("2026-08-01", "2026-08-31", None)

    load.assert_not_called()


def test_hot_files_filters_all_sealed_roots_before_bridge_aggregation(
    tmp_path: Path,
    monkeypatch,
):
    from work_buddy.mcp_server import context_wrappers
    from work_buddy.obsidian import ktr, vault_events

    vault = tmp_path / "vault"
    roots = (
        vault / "Daily",
        vault / "projects",
        vault / "contracts",
        vault / "Personal" / "Knowledge",
    )
    cfg = {"vault_root": str(vault), "obsidian": {"exclude_folders": []}}
    observed: list[set[str]] = []

    def _capture_excludes(*_args, exclude_folders=None, **_kwargs):
        observed.append(set(exclude_folders or []))

    def _ledger(*_args, exclude_folders=None, **_kwargs):
        observed.append(set(exclude_folders or []))
        return {
            "files": [
                {"path": "Daily/entry.md", "hot_score": 100},
                {"path": "projects/one.md", "hot_score": 90},
                {"path": "contracts/one.md", "hot_score": 80},
                {"path": "Personal/Knowledge/one.md", "hot_score": 70},
                {"path": "notes/open.md", "hot_score": 5},
            ]
        }

    def _ktr(*_args, exclude_folders=None, **_kwargs):
        observed.append(set(exclude_folders or []))
        return {
            "files": [
                {"filePath": "Daily/entry.md", "hot_score": 100},
                {"filePath": "notes/open.md", "hot_score": 4},
            ]
        }

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted", lambda _component_id: True
    )
    monkeypatch.setattr("work_buddy.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "work_buddy.vault_index.authority_exclusions.sealed_legacy_roots",
        lambda _cfg, allow_default_data_root=True: roots,
    )
    monkeypatch.setattr(vault_events, "bootstrap", _capture_excludes)
    monkeypatch.setattr(vault_events, "get_hot_files", _ledger)
    monkeypatch.setattr(ktr, "get_hot_files", _ktr)

    result = context_wrappers._gather_hot_files(
        "2026-08-01", "2026-08-31", None
    )

    assert [item["path"] for item in result] == ["notes/open.md"]
    required = {"Daily", "projects", "contracts", "Personal/Knowledge"}
    assert len(observed) == 3
    assert all(required <= exclusions for exclusions in observed)

    bridge_call = MagicMock(side_effect=AssertionError("bridge activity queried"))
    monkeypatch.setattr(vault_events, "bootstrap", bridge_call)
    monkeypatch.setattr(vault_events, "get_hot_files", bridge_call)
    monkeypatch.setattr(ktr, "get_hot_files", bridge_call)
    assert context_wrappers._gather_hot_files(
        "2026-08-01", "2026-08-31", "Daily"
    ) == []
    bridge_call.assert_not_called()


def test_activity_timeline_is_native_and_not_obsidian_gated():
    declaration = _frontmatter(
        REPO / "knowledge" / "store" / "journal" / "activity_timeline.md"
    )

    assert "obsidian" not in (declaration.get("requires") or [])
    assert "native Journal SQLite" in declaration["description"]


def test_context_bundle_skips_app_sources_before_collector_access(
    tmp_path: Path,
    monkeypatch,
):
    from work_buddy.context import cache as cache_mod
    from work_buddy.context import registry
    from work_buddy.context.collector import ContextCollector
    from work_buddy.context.types import BaseContextSource, ContextRequest, ContextSection

    class _Source(BaseContextSource):
        def __init__(self, name: str, calls: list[str]):
            self.name = name
            self._calls = calls

        def collect(self, request):
            self._calls.append(self.name)
            if self.name != "native":
                raise AssertionError(f"legacy source invoked: {self.name}")
            return ContextSection(source=self.name, items=["ok"])

        def render(self, section, depth):
            return "ok"

    calls: list[str] = []
    snapshot = registry.all_sources()
    registry.clear()
    try:
        for name in (
            "obsidian",
            "obsidian_tasks",
            "obsidian_wellness",
            "day_planner",
            "datacore",
            "native",
        ):
            registry.register(_Source(name, calls))
        monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path)
        monkeypatch.setattr(
            "work_buddy.health.preferences.is_wanted",
            lambda component_id: False if component_id == "obsidian" else None,
        )

        ctx = ContextCollector().collect(ContextRequest())
        explicit = ContextCollector().collect(ContextRequest(sources=["datacore"]))
    finally:
        registry.clear()
        for name, source in snapshot.items():
            registry.register(source)

    assert calls == ["native"]
    assert set(ctx.sections) == {"native"}
    assert explicit.sections == {}


def test_native_calendar_unavailable_report_has_no_obsidian_setup_advice():
    from work_buddy.collectors.calendar_collector import _unavailable_report

    report = _unavailable_report("provider disabled")
    assert "Obsidian" not in report
    assert "plugin" not in report.lower()
    assert "selected Calendar provider" in report


def test_probe_all_cascades_obsidian_opt_out_without_bridge_or_plugin_probe(
    tmp_path: Path,
    monkeypatch,
):
    from work_buddy import tools

    calls: list[str] = []
    probes = {
        "obsidian": tools.ToolProbe(
            id="obsidian",
            display_name="legacy bridge",
            probe_fn=lambda: calls.append("obsidian") or True,
        ),
        "datacore": tools.ToolProbe(
            id="datacore",
            display_name="legacy plugin",
            probe_fn=lambda: calls.append("datacore") or True,
            depends_on=["obsidian"],
        ),
        "native": tools.ToolProbe(
            id="native",
            display_name="native service",
            probe_fn=lambda: calls.append("native") or True,
        ),
    }
    monkeypatch.setattr(tools, "_TOOL_PROBES", probes)
    monkeypatch.setattr(tools, "_TOOL_STATUS", None)
    monkeypatch.setattr(tools, "_TOOL_STATUS_FILE", tmp_path / "tool_status.json")
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )

    result = tools.probe_all(force=True)

    assert calls == ["native"]
    assert result["obsidian"]["user_opted_out"] is True
    assert result["datacore"]["user_opted_out"] is True
    assert result["datacore"]["blocked_by_opt_out"] == ["obsidian"]
    assert result["native"]["available"] is True


def test_force_reprobe_and_lazy_recovery_never_override_opt_out(monkeypatch):
    from work_buddy import recovery, tools

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    monkeypatch.setattr(tools, "_TOOL_PROBES", {
        "obsidian": tools.ToolProbe(
            id="obsidian",
            display_name="legacy bridge",
            probe_fn=lambda: (_ for _ in ()).throw(AssertionError("bridge probed")),
        ),
    })
    monkeypatch.setattr(tools, "_TOOL_STATUS", None)
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    with patch("work_buddy.tools._persist_tool_status"):
        entry = tools.reprobe_one("obsidian")
    assert entry and entry["user_opted_out"] is True

    with patch("work_buddy.tools.reprobe_one") as reprobe:
        assert recovery.recheck_tool("obsidian", force=True) is False
    reprobe.assert_not_called()


def test_diagnostics_return_disabled_without_checks_or_advice(monkeypatch):
    from work_buddy.health.diagnostics import DiagnosticRunner

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    with patch(
        "work_buddy.health.diagnostics._import_check_fn",
        side_effect=AssertionError("diagnostic check imported"),
    ) as importer:
        direct = DiagnosticRunner().diagnose("obsidian")
        child = DiagnosticRunner().diagnose("datacore")

    importer.assert_not_called()
    assert direct.status == "disabled"
    assert child.status == "disabled"
    assert direct.steps_run == child.steps_run == []
    assert direct.fix_suggestion is None
    assert child.fix_suggestion is None


def test_setup_wizard_skips_requirement_checks_for_opted_out_dependency(monkeypatch):
    from work_buddy.health.wizard import SetupWizard

    wizard = SetupWizard()
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    monkeypatch.setattr(
        "work_buddy.health.preferences.get_preference",
        lambda component_id: MagicMock(wanted=None, reason=None),
    )
    with patch.object(
        wizard.requirements,
        "check_component",
        side_effect=AssertionError("requirements checked"),
    ) as checker:
        result = wizard.diagnose("datacore")
    checker.assert_not_called()
    assert result["diagnostics"]["status"] == "disabled"
    assert result["requirements"]["results"] == []


def test_notification_dispatcher_and_palette_skip_obsidian_clients(monkeypatch):
    from work_buddy.notifications.dispatcher import SurfaceDispatcher
    from work_buddy.dashboard.api import _obsidian_commands

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    with patch(
        "work_buddy.notifications.surfaces.obsidian.ObsidianSurface",
        side_effect=AssertionError("surface constructed"),
    ) as surface:
        dispatcher = SurfaceDispatcher.from_config()
    surface.assert_not_called()
    assert "obsidian" not in dispatcher.surface_names

    with patch(
        "work_buddy.obsidian.commands.ObsidianCommands",
        side_effect=AssertionError("command bridge constructed"),
    ) as commands:
        assert _obsidian_commands({}) == []
    commands.assert_not_called()


def test_forged_palette_execute_is_fenced_after_obsidian_opt_out(monkeypatch):
    from work_buddy.dashboard import service

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    with patch(
        "work_buddy.obsidian.commands.ObsidianCommands",
        side_effect=AssertionError("command bridge constructed"),
    ) as commands:
        response = service.app.test_client().post(
            "/api/palette/execute",
            json={"command_id": "obsidian::app:open-settings", "params": {}},
        )

    assert response.status_code == 403
    assert response.get_json()["error_code"] == "feature_opted_out"
    commands.assert_not_called()


def test_cached_workbuddy_palette_entry_is_rechecked_before_listing_and_call(
    monkeypatch,
):
    from work_buddy.dashboard import api, service
    from work_buddy.mcp_server.registry import Capability

    call = MagicMock(side_effect=AssertionError("cached callable invoked"))
    capability = Capability(
        name="legacy_bridge_call",
        description="legacy",
        category="test",
        parameters={},
        callable=call,
        requires=["obsidian"],
    )
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry",
        lambda: {capability.name: capability},
    )
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )

    assert api._workbuddy_commands({}) == []
    response = service.app.test_client().post(
        "/api/palette/execute",
        json={"command_id": f"work-buddy::{capability.name}", "params": {}},
    )

    assert response.status_code == 403
    assert response.get_json()["error_code"] == "feature_opted_out"
    call.assert_not_called()


def test_cached_sidecar_entries_are_rechecked_before_every_callable(monkeypatch):
    from work_buddy.mcp_server.registry import (
        Capability,
        WorkflowDefinition,
        WorkflowStep,
    )
    from work_buddy.sidecar.dispatch import executor

    call = MagicMock(side_effect=AssertionError("cached callable invoked"))
    capability = Capability(
        name="legacy_bridge_call",
        description="legacy",
        category="test",
        parameters={},
        callable=call,
        requires=["obsidian"],
    )
    workflow = WorkflowDefinition(
        name="legacy_workflow",
        description="legacy",
        workflow_file="synthetic.md",
        execution="main",
        steps=[
            WorkflowStep(
                id=capability.name,
                name="legacy step",
                instruction="",
                step_type="code",
            )
        ],
        requires=["obsidian"],
    )
    registry = {capability.name: capability, workflow.name: workflow}
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry", lambda: registry
    )
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )

    capability_result = executor._execute_capability(capability.name, {})
    workflow_result = executor._execute_workflow(workflow.name, {})
    step_result = executor._execute_code_step(capability.name, "legacy step")

    for result in (capability_result, workflow_result, step_result):
        assert result["error_code"] == "feature_opted_out"
        assert result["opted_out"] == ["obsidian"]
    call.assert_not_called()


def test_telegram_obs_command_and_stale_callback_are_fenced_after_opt_out(
    monkeypatch,
) -> None:
    from work_buddy.telegram import handlers

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    monkeypatch.setattr(handlers, "_log_inbound", MagicMock())
    monkeypatch.setattr(handlers, "_is_authorized", lambda *_args: True)
    monkeypatch.setattr(handlers, "_TRUNC", 500, raising=False)
    reply = AsyncMock()
    monkeypatch.setattr(handlers, "_reply", reply)
    context = MagicMock()
    context.bot_data = {"state": object()}
    context.args = ["settings"]
    command_update = MagicMock()

    callback = MagicMock()
    callback.data = "obs:app:open-settings"
    callback.answer = AsyncMock()
    callback.edit_message_text = AsyncMock()
    callback_update = MagicMock()
    callback_update.callback_query = callback
    callback_update.effective_chat.id = 123

    with patch(
        "work_buddy.obsidian.commands.ObsidianCommands",
        side_effect=AssertionError("command bridge constructed"),
    ) as commands:
        asyncio.run(handlers.cmd_obs(command_update, context))
        asyncio.run(handlers.on_button(callback_update, context))

    assert "disabled" in reply.await_args.args[1].lower()
    callback.answer.assert_awaited_once()
    assert "disabled" in callback.edit_message_text.await_args.args[0].lower()
    commands.assert_not_called()


def test_telegram_status_skips_bridge_probe_after_obsidian_opt_out(monkeypatch):
    from work_buddy.telegram import handlers

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    monkeypatch.setattr(handlers, "_log_inbound", MagicMock())
    monkeypatch.setattr(handlers, "_is_authorized", lambda *_args: True)
    reply = AsyncMock()
    monkeypatch.setattr(handlers, "_reply", reply)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        MagicMock(side_effect=OSError("messaging unavailable")),
    )
    context = MagicMock()
    context.bot_data = {"state": object()}
    update = MagicMock()

    with patch(
        "work_buddy.obsidian.bridge.is_available",
        side_effect=AssertionError("retired Obsidian bridge was probed"),
    ) as bridge:
        asyncio.run(handlers.cmd_status(update, context))

    rendered = reply.await_args.args[1]
    assert "Obsidian bridge: disabled" in rendered
    bridge.assert_not_called()


def test_retry_replay_suppresses_opted_out_capability_without_invocation(monkeypatch):
    from work_buddy.mcp_server.registry import Capability
    from work_buddy.sidecar.retry_sweep import RetrySweep

    call = MagicMock(return_value={"success": True})
    capability = Capability(
        name="legacy_bridge_call",
        description="legacy",
        category="test",
        parameters={},
        callable=call,
        # Exercise the transitive preference boundary: Datacore is backed by
        # the Obsidian bridge even when it has no independent preference row.
        requires=["datacore"],
    )
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry",
        lambda: {"legacy_bridge_call": capability},
    )
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_disabled_registry",
        lambda: {},
    )

    result = RetrySweep()._replay({
        "name": "legacy_bridge_call",
        "params": {},
        "operation_id": "op-opted-out",
    })

    assert result["suppressed"] is True
    assert result["error_code"] == "feature_opted_out"
    assert result["opted_out_tools"] == ["obsidian"]
    call.assert_not_called()


def test_gateway_manual_retry_rechecks_inner_cached_entry(monkeypatch):
    from work_buddy.mcp_server.registry import Capability
    from work_buddy.mcp_server.tools import gateway

    call = MagicMock(side_effect=AssertionError("cached retry callable invoked"))
    capability = Capability(
        name="legacy_bridge_call",
        description="legacy",
        category="test",
        parameters={},
        callable=call,
        requires=["datacore"],
    )
    record = {
        "operation_id": "op-legacy-retry",
        "name": capability.name,
        "params": {},
        "type": "capability",
        "retry_policy": "replay",
        "status": "failed",
        "result": None,
        "error": "bridge unavailable",
        "attempt": 1,
        "locked_until": None,
    }
    monkeypatch.setattr(gateway, "_load_operation", lambda _operation_id: record)
    monkeypatch.setattr(gateway, "_update_operation", MagicMock())
    complete = MagicMock()
    monkeypatch.setattr(gateway, "_complete_operation", complete)
    monkeypatch.setattr(
        gateway.registry,
        "get_entry",
        lambda name: capability if name == capability.name else None,
    )
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )

    result = gateway.retry_operation(record["operation_id"])

    assert result["error_code"] == "feature_opted_out"
    assert result["opted_out"] == ["obsidian"]
    assert result["suppressed"] is True
    complete.assert_called_once()
    call.assert_not_called()


def test_gateway_suppresses_admitted_obsidian_capability_after_opt_out(monkeypatch):
    from work_buddy.mcp_server.registry import Capability
    from work_buddy.mcp_server.tools import gateway

    call = MagicMock(return_value={"success": True})
    capability = Capability(
        name="legacy_bridge_call",
        description="legacy",
        category="test",
        parameters={},
        callable=call,
        requires=["obsidian"],
    )
    monkeypatch.setattr(gateway, "_SESSION_REGISTRY", weakref.WeakKeyDictionary())
    monkeypatch.setattr(gateway, "ensure_listeners_registered", lambda: None)
    monkeypatch.setattr(
        gateway.registry,
        "get_entry",
        lambda name: capability if name == capability.name else None,
    )
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    mcp = _FakeMCP()
    gateway.register_tools(mcp)
    session = _FakeSession()
    gateway._SESSION_REGISTRY[session] = "obsidian-opt-out-test-session"

    result = asyncio.run(
        mcp.tools["wb_run"](
            capability.name,
            params={},
            ctx=_FakeContext(session),
        )
    )

    assert result["disabled"] is True
    assert result["opted_out"] == ["obsidian"]
    call.assert_not_called()


def test_direct_legacy_journal_actions_stop_before_markdown_after_seal(
    tmp_path, monkeypatch
):
    from work_buddy.journal_backlog.route import (
        _append_to_note_impl,
        _create_consideration_impl,
    )
    from work_buddy.journal_capture.authority import JournalAuthorityStateError

    vault = tmp_path / "vault"
    note = vault / "notes" / "existing.md"
    note.parent.mkdir(parents=True)
    note.write_text("archive", encoding="utf-8")
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda _component_id: None,
    )
    monkeypatch.setattr(
        "work_buddy.journal_capture.authority.existing_authority_mode",
        lambda *_args, **_kwargs: "database_only",
    )
    read_text = MagicMock(side_effect=AssertionError("retired Markdown was read"))
    monkeypatch.setattr(Path, "read_text", read_text)

    with pytest.raises(JournalAuthorityStateError, match="retired"):
        _create_consideration_impl("Retired", vault, "inbox")
    with pytest.raises(JournalAuthorityStateError, match="retired"):
        _append_to_note_impl("new text", vault, "notes/existing.md")

    read_text.assert_not_called()
    assert note.read_bytes() == b"archive"


def test_direct_legacy_action_holds_cutover_lock_for_complete_file_operation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.journal_backlog.route import _append_to_note_impl
    from work_buddy.journal_capture.authority import JournalAuthorityStateError
    from work_buddy.journal_capture.store import JournalCaptureStore

    # The autouse authority fixture routes default Journal guards to this exact
    # isolated path.  Initializing it gives the action and the sealing thread a
    # real shared SQLite writer barrier.
    database = tmp_path / "journal_authority_fence.db"
    JournalCaptureStore(database)
    vault = tmp_path / "vault"
    note = vault / "notes" / "entry.md"
    note.parent.mkdir(parents=True)
    note.write_text("archive\n", encoding="utf-8")

    read_started = threading.Event()
    allow_file_operation = threading.Event()
    seal_attempted = threading.Event()
    seal_finished = threading.Event()
    action_results: list[dict] = []
    failures: list[BaseException] = []
    reads = 0
    original_read_text = Path.read_text

    def blocking_read_text(path: Path, *args, **kwargs):
        nonlocal reads
        if path == note:
            reads += 1
            read_started.set()
            if not allow_file_operation.wait(5.0):
                raise AssertionError("test did not release the legacy file operation")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocking_read_text)
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda _component_id: None,
    )

    def run_action() -> None:
        try:
            action_results.append(
                _append_to_note_impl("new text", vault, "notes/entry.md")
            )
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            failures.append(exc)

    def seal() -> None:
        try:
            with sqlite3.connect(database, timeout=5.0) as conn:
                conn.execute("PRAGMA busy_timeout = 5000")
                seal_attempted.set()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE journal_authority_control "
                    "SET mode='database_only' WHERE singleton=1"
                )
                conn.commit()
            seal_finished.set()
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            failures.append(exc)

    action_thread = threading.Thread(target=run_action, daemon=True)
    seal_thread = threading.Thread(target=seal, daemon=True)
    action_thread.start()
    assert read_started.wait(2.0)
    seal_thread.start()
    assert seal_attempted.wait(2.0)
    assert not seal_finished.wait(0.15)

    allow_file_operation.set()
    action_thread.join(5.0)
    seal_thread.join(5.0)
    assert not action_thread.is_alive()
    assert not seal_thread.is_alive()
    assert failures == []
    assert action_results and action_results[0]["success"] is True
    assert seal_finished.is_set()

    reads_before_rejected_action = reads
    with pytest.raises(JournalAuthorityStateError, match="retired"):
        _append_to_note_impl("must not land", vault, "notes/entry.md")
    assert reads == reads_before_rejected_action
    assert "must not land" not in original_read_text(note, encoding="utf-8")


def test_postseal_context_sources_use_native_journal_without_archive_or_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from work_buddy.context import ContextCollector, ContextRequest

    vault = tmp_path / "vault"
    roots = {
        "journal": vault / "journal",
        "projects": vault / "projects",
        "contracts": vault / "contracts",
        "personal": vault / "personal",
    }
    for root in roots.values():
        root.mkdir(parents=True)
        (root / "archive.md").write_text("must not be read", encoding="utf-8")
    cfg = {
        "vault_root": str(vault),
        "obsidian": {"journal_dir": "journal", "journal_days": 2},
        "projects": {"markdown_dir": "projects"},
        "contracts": {"vault_path": "contracts"},
        "personal_knowledge": {"vault_path": "personal"},
        "tasks": {"event_lookback_hours": 1},
    }
    native_states = MagicMock(
        side_effect=lambda target=None, create_on_read=False: {
            "target_date": target or "2026-08-27",
            "exists": False,
            "items": [],
            "fields": [],
        }
    )
    monkeypatch.setattr(
        "work_buddy.collectors.obsidian_collector._native_journal_authority",
        lambda _cfg: True,
    )
    monkeypatch.setattr(
        "work_buddy.collectors.obsidian_collector._sealed_legacy_roots",
        lambda _cfg: tuple(roots.values()),
    )
    monkeypatch.setattr(
        "work_buddy.journal_capture.native_ops.journal_state",
        native_states,
    )
    monkeypatch.setattr(
        "work_buddy.journal_capture.native_ops.day_planner",
        lambda **_kwargs: {
            "entries": [],
            "target_date": "2026-08-27",
            "authority": "journal_sqlite",
        },
    )
    monkeypatch.setattr(
        "work_buddy.collectors.obsidian_collector._get_tasks",
        lambda _vault: "",
    )
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.store.get_events_in_range",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: True if component_id == "obsidian" else None,
    )

    custom = {
        source: cfg
        for source in (
            "obsidian",
            "obsidian_tasks",
            "obsidian_wellness",
            "day_planner",
        )
    }
    with patch(
        "work_buddy.journal_capture.content_adapter.JournalContentAdapter",
        side_effect=AssertionError("retired Journal archive adapter constructed"),
    ) as archive, patch(
        "work_buddy.obsidian.bridge.is_available",
        side_effect=AssertionError("Obsidian bridge probed"),
    ) as bridge:
        result = ContextCollector().collect(
            ContextRequest(
                sources=list(custom),
                custom=custom,
                max_age_seconds=None,
            )
        )

    assert set(result.sections) == set(custom)
    assert native_states.call_count >= 3
    archive.assert_not_called()
    bridge.assert_not_called()


def test_suppressed_retry_is_cancelled_without_success_or_exhaustion_hooks():
    from work_buddy.sidecar.retry_sweep import RetrySweep

    record = {
        "name": "legacy_bridge_call",
        "operation_id": "op-opted-out",
        "queued": True,
        "queued_for_retry": True,
        "status": "running",
    }
    result = {
        "success": False,
        "suppressed": True,
        "error": "Feature opted out: obsidian",
        "opted_out_tools": ["obsidian"],
    }
    sweep = RetrySweep()
    with patch("work_buddy.sidecar.retry_sweep._write_record") as write:
        sweep._on_suppressed(record, result)

    write.assert_called_once_with(record)
    assert record["status"] == "cancelled"
    assert record["queued"] is False
    assert record["queued_for_retry"] is False
    assert record["cancelled_reason"] == "feature_opted_out"


def test_native_control_domains_have_no_obsidian_edges():
    from work_buddy.control.graph_static import DOMAINS, SUBSYSTEMS
    from work_buddy.health.components import COMPONENT_CATALOG

    domains = {item["id"]: item for item in DOMAINS}
    subsystems = {item["id"]: item for item in SUBSYSTEMS}
    assert domains["domain:calendar"]["children_components"] == [
        "google_calendar_native",
    ]
    assert "obsidian" not in domains["domain:notifications"]["children_components"]
    assert subsystems["subsystem:daily-notes"].get("component_deps") == [
        "journal_native"
    ]
    assert subsystems["subsystem:daily-notes"].get("requirement_ids") == []
    assert "obsidian" not in COMPONENT_CATALOG["dashboard"].soft_depends_on
    assert COMPONENT_CATALOG["google_calendar_native"].depends_on == []
    for component_id in (
        "journal_native",
        "projects_native",
        "contracts_native",
        "personal_knowledge_native",
    ):
        assert COMPONENT_CATALOG[component_id].is_core is True
        assert COMPONENT_CATALOG[component_id].health_source == "custom"


def test_disabled_control_help_is_explanatory_and_advice_free():
    from work_buddy.control.help_briefs import _component_brief
    from work_buddy.control.nodes import ControlNode

    node = ControlNode(
        id="component:obsidian",
        kind="component",
        label="Obsidian Bridge (legacy compatibility)",
        description="legacy",
        preference="unwanted",
        effective_state="disabled",
        component_id="obsidian",
        requirement_ids=["obsidian/plugins/work-buddy-plugin"],
    )
    brief = _component_brief(node)
    assert "Diagnostic checks are suppressed" in brief
    assert "Do not probe" in brief
    assert "walk the user through" not in brief.lower()
    assert "fixing" not in brief.lower()
