"""Slice 5a: verify the executor forwards context fields to create_task.

Mirrors ``test_clarify_risk_profile_forwarding.py`` for the
agent_required_contexts / user_required_contexts / required_contexts_source
proposal fields.
"""

from __future__ import annotations

import json
from typing import Any


def _make_group_with_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": 0,
        "intent": "test",
        "rationale": "rationale",
        "suggested_action": "create_task",
        "items": [{"id": "i0", "label": "item"}],
        "records": [{"destination": "task", "task_proposal": proposal}],
        "is_multi_record": True,
    }


def _make_presentation(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "journal",
        "groups_by_action": {
            "close": [], "group": [], "create_task": [group],
            "record_into_task": [], "leave": [],
        },
        "total_groups": 1,
        "total_items": 1,
    }


def test_context_lists_serialize_to_json_kwargs(monkeypatch) -> None:
    """Both lists land on create_task as JSON-encoded TEXT kwargs."""
    from work_buddy.clarify import execute

    captured: dict[str, Any] = {}

    def fake_create_task(**kw):
        captured.update(kw)
        return {"task_id": "t-ctx-001"}

    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task", fake_create_task,
    )

    group = _make_group_with_proposal({
        "suggested_task_text": "Send the dean an email",
        "agent_required_contexts": ["@email_send"],
        "user_required_contexts": ["@email_send", "@user_workstation"],
    })
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    summary = execute.execute_triage_decisions(decisions, _make_presentation(group))

    assert summary["tasks_created"] == 1
    assert "agent_required_contexts" in captured
    assert "user_required_contexts" in captured
    # JSON-encoded
    assert json.loads(captured["agent_required_contexts"]) == ["@email_send"]
    assert json.loads(captured["user_required_contexts"]) == [
        "@email_send", "@user_workstation",
    ]
    # Source defaults to agent_inferred when not explicit.
    assert captured["required_contexts_source"] == "agent_inferred"


def test_explicit_source_preserved(monkeypatch) -> None:
    from work_buddy.clarify import execute

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task",
        lambda **kw: (captured.update(kw), {"task_id": "t-002"})[1],
    )

    group = _make_group_with_proposal({
        "suggested_task_text": "Edit code",
        "agent_required_contexts": ["@filesystem"],
        "user_required_contexts": [],
        "required_contexts_source": "user_authored",
    })
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    execute.execute_triage_decisions(decisions, _make_presentation(group))

    assert captured["required_contexts_source"] == "user_authored"


def test_no_context_fields_no_kwargs(monkeypatch) -> None:
    """Verdict without context lists → no kwargs forwarded → store NULL."""
    from work_buddy.clarify import execute

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task",
        lambda **kw: (captured.update(kw), {"task_id": "t-003"})[1],
    )

    group = _make_group_with_proposal({
        "suggested_task_text": "Generic task",
    })
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    execute.execute_triage_decisions(decisions, _make_presentation(group))

    assert "agent_required_contexts" not in captured
    assert "user_required_contexts" not in captured
    assert "required_contexts_source" not in captured


def test_only_user_side_populated(monkeypatch) -> None:
    """Phone-call task: agent has no role, user side populated."""
    from work_buddy.clarify import execute

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.mutations.create_task",
        lambda **kw: (captured.update(kw), {"task_id": "t-004"})[1],
    )

    group = _make_group_with_proposal({
        "suggested_task_text": "Call the bank",
        "user_required_contexts": ["@phone_voice", "@user_creds"],
    })
    decisions = {"group_decisions": [
        {"group_index": 0, "action": "create_task", "item_overrides": []},
    ]}
    execute.execute_triage_decisions(decisions, _make_presentation(group))

    # Agent side stays None; user side populated.
    assert "agent_required_contexts" not in captured
    assert json.loads(captured["user_required_contexts"]) == [
        "@phone_voice", "@user_creds",
    ]
    assert captured["required_contexts_source"] == "agent_inferred"
