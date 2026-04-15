"""Unit tests for task mutation separation of concerns.

Verifies:
- update_task rejects state='done' with a clear error directing to task_toggle
- update_task still handles non-done state transitions and metadata updates
- toggle_task respects the optional `done` parameter (True/False/None)
- toggle_task no-ops when `done` matches current state
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from work_buddy.obsidian.tasks import mutations


@pytest.fixture(autouse=True)
def _bypass_consent_and_retry():
    """Bypass consent checks and bridge_retry for all tests."""
    with patch("work_buddy.consent._cache") as mock_cache:
        mock_cache.is_granted.return_value = True
        mock_cache.get_mode.return_value = "always"
        yield


@pytest.fixture(autouse=True)
def _patch_bridge_and_store():
    """Patch bridge and store for all tests in this module."""
    with patch.object(mutations, "bridge") as mock_bridge, \
         patch.object(mutations, "store") as mock_store:
        mock_bridge.read_file.return_value = None
        mock_bridge.write_file.return_value = True
        mock_store.update.return_value = {"changed": True}
        mock_store.get.return_value = {
            "task_id": "t-abc123",
            "state": "inbox",
            "urgency": "medium",
        }
        yield mock_bridge, mock_store


# ── update_task: state='done' rejected ──────────────────────────

class TestUpdateTaskRejectsDone:
    """update_task must refuse state='done' and direct to task_toggle."""

    def test_state_done_rejected(self, _patch_bridge_and_store):
        """state='done' returns failure with helpful message."""
        result = mutations.update_task(task_id="t-abc123", state="done")

        assert result["success"] is False
        assert "task_toggle" in result["message"]
        assert "done" in result["message"].lower()

    def test_state_done_skips_store(self, _patch_bridge_and_store):
        """state='done' must not touch the store at all."""
        _, mock_store = _patch_bridge_and_store
        mutations.update_task(task_id="t-abc123", state="done")
        mock_store.update.assert_not_called()

    def test_state_done_with_urgency_still_rejected(self, _patch_bridge_and_store):
        """state='done' + urgency='high' — entire call rejected, not partial."""
        result = mutations.update_task(
            task_id="t-abc123", state="done", urgency="high"
        )
        assert result["success"] is False
        assert "task_toggle" in result["message"]


# ── update_task: non-done state transitions still work ──────────

class TestUpdateTaskNonDoneStates:
    """Non-done state transitions proceed normally."""

    def test_state_focused_updates_store(self, _patch_bridge_and_store):
        _, mock_store = _patch_bridge_and_store
        result = mutations.update_task(task_id="t-abc123", state="focused")

        mock_store.update.assert_called_once()
        kwargs = mock_store.update.call_args.kwargs
        assert kwargs.get("state") == "focused"

    def test_urgency_updates_without_state(self, _patch_bridge_and_store):
        _, mock_store = _patch_bridge_and_store
        result = mutations.update_task(task_id="t-abc123", urgency="high")

        mock_store.update.assert_called_once()
        kwargs = mock_store.update.call_args.kwargs
        assert kwargs.get("urgency") == "high"

    def test_state_snoozed_updates_store(self, _patch_bridge_and_store):
        _, mock_store = _patch_bridge_and_store
        result = mutations.update_task(task_id="t-abc123", state="snoozed")

        mock_store.update.assert_called_once()
        kwargs = mock_store.update.call_args.kwargs
        assert kwargs.get("state") == "snoozed"


# ── toggle_task: `done` parameter behavior ──────────────────────

UNCHECKED_LINE = "- [ ] #todo Fix the bug 🆔 t-abc123\n"
CHECKED_LINE = "- [x] #todo Fix the bug 🆔 t-abc123 ✅ 2026-04-14\n"


class TestToggleTaskDoneParam:
    """toggle_task respects the optional `done` parameter."""

    def test_done_true_on_unchecked_task(self, _patch_bridge_and_store):
        """done=True on an unchecked task should toggle it to done."""
        mock_bridge, mock_store = _patch_bridge_and_store
        mock_bridge.read_file.return_value = UNCHECKED_LINE

        with patch.object(mutations, "_run_js", return_value=None):
            result = mutations.toggle_task(task_id="t-abc123", done=True)

        assert result["success"] is True
        assert result["new_state"] == "done"
        mock_bridge.write_file.assert_called_once()

    def test_done_true_on_already_checked_noop(self, _patch_bridge_and_store):
        """done=True on an already-checked task should no-op."""
        mock_bridge, _ = _patch_bridge_and_store
        mock_bridge.read_file.return_value = CHECKED_LINE

        result = mutations.toggle_task(task_id="t-abc123", done=True)

        assert result["success"] is True
        assert result["new_state"] == "done"
        assert "already" in result.get("message", "").lower()
        mock_bridge.write_file.assert_not_called()

    def test_done_false_on_checked_task(self, _patch_bridge_and_store):
        """done=False on a checked task should toggle it to incomplete."""
        mock_bridge, mock_store = _patch_bridge_and_store
        mock_bridge.read_file.return_value = CHECKED_LINE

        with patch.object(mutations, "_run_js", return_value=None):
            result = mutations.toggle_task(task_id="t-abc123", done=False)

        assert result["success"] is True
        assert result["new_state"] == "inbox"
        mock_bridge.write_file.assert_called_once()

    def test_done_false_on_already_unchecked_noop(self, _patch_bridge_and_store):
        """done=False on an unchecked task should no-op."""
        mock_bridge, _ = _patch_bridge_and_store
        mock_bridge.read_file.return_value = UNCHECKED_LINE

        result = mutations.toggle_task(task_id="t-abc123", done=False)

        assert result["success"] is True
        assert result["new_state"] == "inbox"
        assert "already" in result.get("message", "").lower()
        mock_bridge.write_file.assert_not_called()

    def test_done_none_toggles(self, _patch_bridge_and_store):
        """done=None (default) should toggle the current state."""
        mock_bridge, mock_store = _patch_bridge_and_store
        mock_bridge.read_file.return_value = UNCHECKED_LINE

        with patch.object(mutations, "_run_js", return_value=None):
            result = mutations.toggle_task(task_id="t-abc123", done=None)

        assert result["success"] is True
        assert result["new_state"] == "done"
        mock_bridge.write_file.assert_called_once()

    def test_bridge_down_returns_failure(self, _patch_bridge_and_store):
        """Bridge down should return clean failure, not silent success."""
        mock_bridge, _ = _patch_bridge_and_store
        mock_bridge.read_file.return_value = None

        result = mutations.toggle_task(task_id="t-abc123", done=True)

        assert result["success"] is False
