from __future__ import annotations

from unittest.mock import Mock

import pytest

from work_buddy.tasks import runtime
from work_buddy.tasks.errors import TaskAuthorityUnavailable


def _route_default_store_to_native(monkeypatch, task_store) -> None:
    """Select native authority and point default TaskStore reads at the fixture."""

    monkeypatch.setattr(runtime, "native_authority_active", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "work_buddy.tasks.store.default_task_db_path",
        lambda: task_store.path,
    )


def test_task_match_reads_native_descriptions_without_legacy_surfaces(
    task_store,
    task_service,
    monkeypatch,
) -> None:
    from work_buddy.clarify import task_match

    _route_default_store_to_native(monkeypatch, task_store)
    task_service.create(
        description="Native focus description",
        task_id="t-native-match",
        state="focused",
        contract="native-project",
        summary_text="Match this focused task during triage.",
        client_mutation_id="native-match-create",
        actor="human:test",
    )

    legacy_markdown = Mock(side_effect=AssertionError("legacy Markdown read"))
    legacy_store = Mock(side_effect=AssertionError("legacy task store read"))
    monkeypatch.setattr(
        "work_buddy.obsidian.bridge.read_file_raw", legacy_markdown
    )
    monkeypatch.setattr("work_buddy.obsidian.tasks.store.query", legacy_store)

    assert task_match._read_task_texts() == {
        "t-native-match": "Native focus description"
    }
    assert task_match._load_active_tasks(["focused"]) == [
        {
            "task_id": "t-native-match",
            "text": "Native focus description",
            "state": "focused",
            "project": "native-project",
        }
    ]
    legacy_markdown.assert_not_called()
    legacy_store.assert_not_called()


def test_task_authority_error_never_falls_back_to_master_markdown(
    monkeypatch,
) -> None:
    from work_buddy.clarify import task_match

    def unavailable(*_args, **_kwargs):
        raise TaskAuthorityUnavailable()

    legacy_markdown = Mock(side_effect=AssertionError("legacy fallback"))
    monkeypatch.setattr(runtime, "native_authority_active", unavailable)
    monkeypatch.setattr(
        "work_buddy.obsidian.bridge.read_file_raw", legacy_markdown
    )

    with pytest.raises(TaskAuthorityUnavailable):
        task_match._read_task_texts()
    legacy_markdown.assert_not_called()


def test_tasks_context_queries_native_store_and_cowork_reader_only(
    task_store,
    task_service,
    monkeypatch,
) -> None:
    from work_buddy.context.sources import tasks as tasks_source
    from work_buddy.tasks import capabilities as native_capabilities

    _route_default_store_to_native(monkeypatch, task_store)
    task_service.create(
        description="Native context task",
        task_id="t-native-context",
        state="mit",
        contract="context-contract",
        client_mutation_id="native-context-create",
        actor="human:test",
    )

    legacy_query = Mock(side_effect=AssertionError("legacy task store read"))
    legacy_bridge = Mock(side_effect=AssertionError("legacy task note read"))
    monkeypatch.setattr("work_buddy.obsidian.tasks.store.query", legacy_query)
    monkeypatch.setattr(
        "work_buddy.obsidian.bridge.read_file_raw", legacy_bridge
    )

    rows = tasks_source._collect_tasks(states=("mit",), target_date=None)
    assert rows == [
        {
            "task_id": "t-native-context",
            "state": "mit",
            "text": "Native context task",
            "contract": "context-contract",
        }
    ]

    monkeypatch.setattr(
        native_capabilities,
        "task_read",
        lambda task_id: {
            "success": True,
            "task_id": task_id,
            "note_content": "Live Co-work knowledge",
        },
    )
    assert tasks_source._read_task_note("t-native-context") == (
        "Live Co-work knowledge"
    )
    legacy_query.assert_not_called()
    legacy_bridge.assert_not_called()


def test_completeness_native_path_does_not_load_legacy_task_facade(
    monkeypatch,
) -> None:
    from work_buddy import task_completeness
    from work_buddy.tasks import capabilities as native_capabilities
    from work_buddy.threads.models import Task as TransitionalTask

    monkeypatch.setattr(runtime, "native_authority_active", lambda: True)
    monkeypatch.setattr(
        native_capabilities,
        "task_read",
        lambda task_id: {
            "success": True,
            "task_id": task_id,
            "assigned_sessions": [],
            "note_uuid": "cowork-doc-1",
            "metadata": {"note_uuid": "cowork-doc-1"},
        },
    )
    monkeypatch.setattr(
        native_capabilities,
        "task_provenance",
        lambda task_id: {
            "task_id": task_id,
            "created_by": None,
            "assigned": [],
            "developed_by": [],
        },
    )
    captured: dict[str, object] = {}

    def native_readers(task_id, *, note_uuid=None, include_saw_id=False):
        captured.update(
            task_id=task_id,
            note_uuid=note_uuid,
            include_saw_id=include_saw_id,
        )
        return []

    monkeypatch.setattr(native_capabilities, "task_note_readers", native_readers)
    legacy_facade = Mock(side_effect=AssertionError("legacy facade read"))
    monkeypatch.setattr(TransitionalTask, "load", legacy_facade)

    result = task_completeness.gather_completeness_evidence("t-native-proof")

    assert result["status"] == "ok"
    assert captured == {
        "task_id": "t-native-proof",
        "note_uuid": "cowork-doc-1",
        "include_saw_id": False,
    }
    legacy_facade.assert_not_called()


def test_task_me_native_briefing_and_pure_planner_need_no_obsidian_task_read(
    monkeypatch,
) -> None:
    from work_buddy import contracts
    from work_buddy import task_me
    from work_buddy.dashboard import service as dashboard_service
    from work_buddy.obsidian import bridge
    from work_buddy.obsidian.tasks import manager as legacy_manager
    from work_buddy.tasks import capabilities as native_capabilities

    monkeypatch.setattr(runtime, "native_authority_active", lambda: True)
    monkeypatch.setattr(
        native_capabilities,
        "daily_briefing",
        lambda: {"focused": [], "authority": "native"},
    )
    legacy_briefing = Mock(side_effect=AssertionError("legacy task briefing"))
    monkeypatch.setattr(legacy_manager, "daily_briefing", legacy_briefing)
    monkeypatch.setattr(
        dashboard_service,
        "_build_engage_view_payload",
        lambda current_contexts=None: {
            "status": "ok",
            "items": [],
            "current_contexts": list(current_contexts or []),
        },
    )
    monkeypatch.setattr(contracts, "active_contracts", lambda: [])
    monkeypatch.setattr(contracts, "get_constraints", lambda: [])
    monkeypatch.setattr(
        contracts, "check_wip_limit", lambda: {"within_limit": True}
    )

    context = task_me.load_context_for_task_me()
    assert context["task_briefing"]["authority"] == "native"

    obsidian_runtime = Mock(side_effect=AssertionError("Obsidian runtime read"))
    monkeypatch.setattr(bridge, "require_available", obsidian_runtime)
    plan = task_me.build_now_plan(
        context={
            "engage": {
                "items": [
                    {
                        "task_id": "t-plan-native",
                        "text": "Plan native work",
                        "state": "focused",
                        "who_can_act": {"agent": True},
                        "user_now": {"satisfied": True},
                    }
                ]
            },
            "calendar": [],
        },
        config={"clamp_to_now": False, "work_hours": [9, 17]},
    )
    assert plan["status"] == "ok"
    assert plan["focused_count"] == 1
    legacy_briefing.assert_not_called()
    obsidian_runtime.assert_not_called()
