"""Slice 3 / Slice C: atomic read-modify-write via app.vault.process().

Tests the atomic path in `_find_and_replace_task_line` and the
conflict-detection-and-retry semantics. The bridge layer (which talks
to actual Obsidian) is mocked — we're testing the orchestration logic,
not the JS itself.

Coverage:
  1. Happy path: atomic write succeeds, legacy write_file NOT called.
  2. Conflict: first attempt sees a stale-read; retry with fresh content
     succeeds.
  3. Conflict-retry-conflict: two consecutive conflicts → escalated.
  4. Bridge-down: atomic returns "bridge_returned_none" → fall back to
     legacy.
  5. Atomic raises ObsidianError → fall back to legacy.
  6. Found, no replace, no conflict (line already at desired state) →
     no-op success.
  7. Description-match (no task_id) → still uses legacy path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.obsidian.tasks import mutations


UNCHECKED = "- [ ] #todo Fix the bug 🆔 t-abc123\n"


@pytest.fixture(autouse=True)
def _bypass_consent_and_retry():
    with patch("work_buddy.consent._cache") as mock_cache:
        mock_cache.is_granted.return_value = True
        mock_cache.get_mode.return_value = "always"
        yield


@pytest.fixture
def patched_bridge_store():
    """Default mock — read returns UNCHECKED, no other behaviors set."""
    with patch.object(mutations, "bridge") as mock_bridge, \
         patch.object(mutations, "store") as mock_store:
        mock_bridge.read_file.return_value = UNCHECKED
        mock_bridge.write_file.return_value = True
        mock_store.update.return_value = {"changed": True}
        mock_store.get.return_value = {
            "task_id": "t-abc123",
            "description": "Fix the bug",
        }
        yield mock_bridge, mock_store


# ---------------------------------------------------------------------------
# Happy path: atomic succeeds, legacy NOT used
# ---------------------------------------------------------------------------


def test_atomic_success_skips_legacy_write_file(patched_bridge_store):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_replace_line_by_task_id.return_value = {
        "found": True,
        "conflict": False,
        "replaced": True,
        "line_number": 1,
        "old_line": "- [ ] #todo Fix the bug 🆔 t-abc123",
        "new_line": "- [ ] #todo Fix the auth bug 🆔 t-abc123",
    }

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is True
    assert result.get("atomic") is True
    # Legacy write_file must NOT have been called.
    mock_bridge.write_file.assert_not_called()
    # Atomic was called.
    mock_bridge.atomic_replace_line_by_task_id.assert_called()


# ---------------------------------------------------------------------------
# Conflict-then-retry succeeds
# ---------------------------------------------------------------------------


def test_atomic_conflict_then_retry_succeeds(patched_bridge_store):
    """First atomic call returns conflict (user edited the line);
    re-applying the transform to the fresh line and retrying succeeds."""
    mock_bridge, _ = patched_bridge_store
    fresh_old = "- [ ] #todo Fix the bug urgently 🆔 t-abc123"
    fresh_new_after_transform = "- [ ] #todo Fix the auth bug 🆔 t-abc123"

    mock_bridge.atomic_replace_line_by_task_id.side_effect = [
        # First: conflict — fresh content has a different line
        {
            "found": True,
            "conflict": True,
            "replaced": False,
            "line_number": 1,
            "old_line": fresh_old,
        },
        # Second (retry with fresh content): success
        {
            "found": True,
            "conflict": False,
            "replaced": True,
            "line_number": 1,
            "old_line": fresh_old,
            "new_line": fresh_new_after_transform,
        },
    ]

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is True
    assert result.get("conflict_resolved") is True
    assert result.get("atomic") is True
    assert mock_bridge.atomic_replace_line_by_task_id.call_count == 2
    mock_bridge.write_file.assert_not_called()


# ---------------------------------------------------------------------------
# Two consecutive conflicts → escalated
# ---------------------------------------------------------------------------


def test_atomic_double_conflict_escalates(patched_bridge_store):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_replace_line_by_task_id.side_effect = [
        {
            "found": True,
            "conflict": True,
            "replaced": False,
            "line_number": 1,
            "old_line": "- [ ] #todo Fix the bug urgently 🆔 t-abc123",
        },
        {
            "found": True,
            "conflict": True,
            "replaced": False,
            "line_number": 1,
            "old_line": "- [ ] #todo Different again 🆔 t-abc123",
        },
    ]

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is False
    assert "concurrent edit" in result["message"].lower()
    assert result.get("atomic") is True
    mock_bridge.write_file.assert_not_called()


# ---------------------------------------------------------------------------
# Bridge fallthrough: atomic returns bridge_returned_none → legacy used
# ---------------------------------------------------------------------------


def test_atomic_bridge_returned_none_falls_back_to_legacy(patched_bridge_store):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_replace_line_by_task_id.return_value = {
        "error": "bridge_returned_none",
    }

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is True
    # Legacy path was used — the result lacks `atomic=True` from the
    # atomic branch.
    assert result.get("atomic") is False or "atomic" not in result
    mock_bridge.write_file.assert_called_once()


# ---------------------------------------------------------------------------
# Atomic raises typed ObsidianError → fall back to legacy
# ---------------------------------------------------------------------------


def test_atomic_pwu_propagates_for_gateway_recovery(patched_bridge_store):
    """Critical regression test: ObsidianPostWriteUncertain raised by the
    atomic eval must propagate up to the gateway's CP-A7 recovery path,
    NOT trigger the legacy fallback. Without this, the atomic-write
    semantics are silently bypassed when the bridge ack times out.

    Live-test failure 2026-04-28: a `task_update_description` PWU was
    coming from the LEGACY bridge.write_file (PUT path), not the eval
    POST path — meaning the atomic helper had silently fallen through
    to legacy. This test pins the new behavior.
    """
    from work_buddy.obsidian.errors import ObsidianPostWriteUncertain
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_replace_line_by_task_id.side_effect = (
        ObsidianPostWriteUncertain(
            "tasks/master-task-list.md",
            content_hint="- [ ] #todo Fix the auth bug 🆔 t-abc123",
            write_mode="insert",
        )
    )

    # PWU must propagate up — gateway's CP-A7 layer (above this code)
    # is what verifies and decides. The retry decorator explicitly
    # propagates PWU instead of swallowing it.
    with pytest.raises(ObsidianPostWriteUncertain):
        mutations.update_task_description(
            task_id="t-abc123",
            new_description="Fix the auth bug",
        )

    # Crucially: the legacy write_file path must NOT have run.
    mock_bridge.write_file.assert_not_called()


def test_atomic_obsidian_error_falls_back_to_legacy(patched_bridge_store):
    from work_buddy.obsidian.errors import ObsidianUnreachable
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_replace_line_by_task_id.side_effect = ObsidianUnreachable()

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is True
    mock_bridge.write_file.assert_called_once()


# ---------------------------------------------------------------------------
# File not found in vault
# ---------------------------------------------------------------------------


def test_atomic_file_not_found_surfaces_failure(patched_bridge_store):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_replace_line_by_task_id.return_value = {
        "found": False, "conflict": False, "replaced": False,
        "error": "file_not_found",
    }

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is False
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# Task vanished between read and atomic write
# ---------------------------------------------------------------------------


def test_atomic_task_vanished_surfaces_not_found(patched_bridge_store):
    mock_bridge, _ = patched_bridge_store
    # Atomic returns found=False (task was deleted between read and
    # atomic write).
    mock_bridge.atomic_replace_line_by_task_id.return_value = {
        "found": False, "conflict": False, "replaced": False,
    }

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is False
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# JS error inside Obsidian → fall back
# ---------------------------------------------------------------------------


def test_atomic_runtime_error_falls_back_to_legacy(patched_bridge_store):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_replace_line_by_task_id.side_effect = RuntimeError(
        "Eval error: TypeError: Cannot read property 'getAbstractFileByPath' of undefined"
    )

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is True
    mock_bridge.write_file.assert_called_once()


# ---------------------------------------------------------------------------
# Description-match (no task_id) routes to legacy unconditionally
# ---------------------------------------------------------------------------


def test_description_match_uses_legacy_path():
    """When only description_match is given (no task_id), the atomic
    path is skipped — atomic identification needs a task_id."""
    with patch.object(mutations, "bridge") as mock_bridge:
        mock_bridge.read_file.return_value = UNCHECKED
        mock_bridge.write_file.return_value = True

        result = mutations._find_and_replace_task_line(
            file_path="tasks/master-task-list.md",
            task_id=None,
            description_match="Fix the bug",
            transform_fn=lambda line: line.replace(
                "Fix the bug", "Fix the auth bug"
            ),
        )

        assert result["success"] is True
        # Atomic must NOT have been called.
        mock_bridge.atomic_replace_line_by_task_id.assert_not_called()
        # Legacy path used.
        mock_bridge.write_file.assert_called_once()


# ---------------------------------------------------------------------------
# Already-matches-target: no-op success
# ---------------------------------------------------------------------------


def test_atomic_no_op_already_matches_target(patched_bridge_store):
    """If the atomic write finds the line is already in its target
    state (transform produces same text), report success without
    writing."""
    mock_bridge, mock_store = patched_bridge_store
    # Pre-condition: read returns a line where transform is a no-op.
    mock_bridge.read_file.return_value = (
        "- [ ] #todo Fix the auth bug 🆔 t-abc123\n"
    )
    mock_store.get.return_value = {
        "task_id": "t-abc123",
        "description": "Fix the auth bug",  # already matches new
    }

    # We never reach atomic here because old_line == new_line in the
    # transform. The legacy "no changes needed" path runs.
    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is True
    assert "no changes" in result.get("message", "").lower()
    mock_bridge.write_file.assert_not_called()
    mock_bridge.atomic_replace_line_by_task_id.assert_not_called()
