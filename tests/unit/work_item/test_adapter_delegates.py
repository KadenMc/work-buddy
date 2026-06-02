"""Delegation tests for the Task write port (``work_item.task_adapter``).

Each adapter function must forward verbatim to its ``mutations.py`` counterpart
and return that result unchanged — the port is a pure pass-through. Patching the
mutation functions keeps these tests free of the bridge and the store: the point
under test is the *delegation*, not the mutation itself.
"""

from __future__ import annotations

from unittest.mock import patch

from work_buddy.obsidian.tasks import mutations
from work_buddy.work_item import task_adapter


def test_create_delegates_to_create_task():
    sentinel = {"success": True, "task_id": "t-abc123"}
    with patch.object(mutations, "create_task", return_value=sentinel) as m:
        result = task_adapter.create(
            "do the thing",
            urgency="high",
            project="work-buddy",
            creation_provenance="agent_inferred_from_email",
            user_involvement="medium",
        )
    assert result is sentinel
    # First seven params forwarded explicitly; the GTD/risk tail rides **kwargs.
    m.assert_called_once_with(
        task_text="do the thing",
        urgency="high",
        project="work-buddy",
        due_date=None,
        contract=None,
        summary=None,
        tags=None,
        creation_provenance="agent_inferred_from_email",
        user_involvement="medium",
    )


def test_toggle_delegates_to_toggle_task():
    sentinel = {"success": True}
    with patch.object(mutations, "toggle_task", return_value=sentinel) as m:
        result = task_adapter.toggle("t-abc123", done=True)
    assert result is sentinel
    m.assert_called_once_with(
        task_id="t-abc123", done=True, file_path=None, done_date=None,
    )


def test_update_delegates_to_update_task_with_task_id_kwarg():
    sentinel = {"success": True}
    with patch.object(mutations, "update_task", return_value=sentinel) as m:
        result = task_adapter.update("t-abc123", urgency="low", reason="re-triage")
    assert result is sentinel
    # update_task is fully keyword-only — task_id forwarded as a kwarg, and the
    # description_match fallback is carried through (None when unused).
    m.assert_called_once_with(
        task_id="t-abc123",
        description_match=None,
        state=None,
        urgency="low",
        complexity=None,
        contract=None,
        snooze_until=None,
        due_date=None,
        reason="re-triage",
        file_path=None,
    )


def test_set_description_delegates():
    sentinel = {"success": True}
    with patch.object(mutations, "update_task_description", return_value=sentinel) as m:
        result = task_adapter.set_description("t-abc123", "new text")
    assert result is sentinel
    m.assert_called_once_with(
        task_id="t-abc123", new_description="new text", file_path=None,
    )


def test_set_tags_delegates():
    sentinel = {"success": True}
    with patch.object(mutations, "set_task_tags_on_line", return_value=sentinel) as m:
        result = task_adapter.set_tags("t-abc123", ["admin/uhn"])
    assert result is sentinel
    m.assert_called_once_with(task_id="t-abc123", namespace_tags=["admin/uhn"])


def test_delete_delegates():
    sentinel = {"success": True}
    with patch.object(mutations, "delete_task", return_value=sentinel) as m:
        result = task_adapter.delete("t-abc123")
    assert result is sentinel
    m.assert_called_once_with(task_id="t-abc123")


def test_assign_delegates():
    sentinel = {"success": True}
    with patch.object(mutations, "assign_task", return_value=sentinel) as m:
        result = task_adapter.assign("t-abc123")
    assert result is sentinel
    m.assert_called_once_with(task_id="t-abc123")
