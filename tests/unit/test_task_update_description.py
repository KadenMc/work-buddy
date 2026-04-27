"""Slice 3 / Slice B: task_update_description mutation.

Two surfaces:
1. ``replace_description_in_line(line, new_text)`` — pure string transform.
2. ``update_task_description(task_id, new_description)`` — full mutation
   with bridge + store updates.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.obsidian.tasks import mutations
from work_buddy.obsidian.tasks.mutations import (
    extract_description_from_line,
    replace_description_in_line,
)


# ---------------------------------------------------------------------------
# Pure transform: replace_description_in_line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old,new,expected",
    [
        # Vanilla rewrite.
        (
            "- [ ] #todo Fix the bug 🆔 t-abc123",
            "Fix the auth bug",
            "- [ ] #todo Fix the auth bug 🆔 t-abc123",
        ),
        # Done task — checkbox preserved.
        (
            "- [x] #todo Fix the bug 🆔 t-abc123 ✅ 2026-04-27",
            "Fix the bug fully",
            "- [x] #todo Fix the bug fully 🆔 t-abc123 ✅ 2026-04-27",
        ),
        # Project tag preserved.
        (
            "- [ ] #todo Refactor auth #projects/work-buddy 🆔 t-abc123",
            "Refactor the auth flow",
            "- [ ] #todo Refactor the auth flow #projects/work-buddy 🆔 t-abc123",
        ),
        # Wikilink preserved.
        (
            "- [ ] #todo Build dashboard [[abc-def-ghi|📓]] 🆔 t-abc123",
            "Build the new dashboard",
            "- [ ] #todo Build the new dashboard [[abc-def-ghi|📓]] 🆔 t-abc123",
        ),
        # Multiple namespace tags preserved.
        (
            "- [ ] #todo Train model #paper/ecg #experiment/aug 🆔 t-aaa111",
            "Train the new ECG model",
            "- [ ] #todo Train the new ECG model #paper/ecg #experiment/aug 🆔 t-aaa111",
        ),
        # Due date preserved.
        (
            "- [ ] #todo Investigate Kubernetes 🆔 t-bbb222 📅 2026-04-30",
            "Investigate the K8s deployment",
            "- [ ] #todo Investigate the K8s deployment 🆔 t-bbb222 📅 2026-04-30",
        ),
        # Whitespace in input collapsed.
        (
            "- [ ] #todo Old text 🆔 t-abc123",
            "  New    text   with   spaces  ",
            "- [ ] #todo New text with spaces 🆔 t-abc123",
        ),
    ],
)
def test_replace_preserves_structure(old: str, new: str, expected: str) -> None:
    assert replace_description_in_line(old, new) == expected


def test_replace_keeps_round_trip_with_extractor() -> None:
    """After a description-rewrite, the extractor must round-trip
    cleanly — it should pull out exactly the new description and not
    leak any structural tokens."""
    old = "- [ ] #todo old text [[uuid|📓]] #projects/foo 🆔 t-abc123 📅 2026-04-30"
    new_line = replace_description_in_line(old, "completely new text")
    assert extract_description_from_line(new_line) == "completely new text"


def test_replace_no_match_returns_unchanged() -> None:
    """A non-task line (no checkbox + #todo prefix) is returned as-is."""
    line = "Just some prose, not a task."
    assert replace_description_in_line(line, "new") == line


def test_replace_strips_newlines_in_new_description() -> None:
    """Newlines must not survive into a single-line task line."""
    old = "- [ ] #todo old 🆔 t-abc123"
    out = replace_description_in_line(old, "new\nwith\rlinebreaks")
    assert out == "- [ ] #todo new with linebreaks 🆔 t-abc123"


# ---------------------------------------------------------------------------
# Mutation: update_task_description
# ---------------------------------------------------------------------------

UNCHECKED = "- [ ] #todo Fix the bug 🆔 t-abc123\n"
WITH_TAGS = "- [ ] #todo Refactor auth #projects/wb #paper/ecg 🆔 t-bbb222\n"


@pytest.fixture(autouse=True)
def _bypass_consent_and_retry():
    with patch("work_buddy.consent._cache") as mock_cache:
        mock_cache.is_granted.return_value = True
        mock_cache.get_mode.return_value = "always"
        yield


@pytest.fixture(autouse=True)
def _patch_bridge_and_store():
    with patch.object(mutations, "bridge") as mock_bridge, \
         patch.object(mutations, "store") as mock_store:
        mock_bridge.read_file.return_value = UNCHECKED
        mock_bridge.write_file.return_value = True
        # Slice C: force atomic path to fall through to legacy by
        # default so these legacy-path tests keep testing the same
        # write_file code path. Atomic-path coverage lives in the
        # dedicated atomic-write test file.
        mock_bridge.atomic_replace_line_by_task_id.return_value = {
            "error": "bridge_returned_none",
        }
        mock_store.update.return_value = {"changed": True}
        mock_store.get.return_value = {
            "task_id": "t-abc123",
            "state": "inbox",
            "urgency": "medium",
            "description": "Fix the bug",
        }
        yield mock_bridge, mock_store


def test_update_description_writes_file_and_store(_patch_bridge_and_store):
    mock_bridge, mock_store = _patch_bridge_and_store

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="Fix the auth bug",
    )

    assert result["success"] is True
    assert result["task_id"] == "t-abc123"
    assert result["old_description"] == "Fix the bug"
    assert result["new_description"] == "Fix the auth bug"
    assert result["store_updated"] is True

    mock_bridge.write_file.assert_called_once()
    written_content = mock_bridge.write_file.call_args[0][1]
    assert "Fix the auth bug" in written_content
    assert "🆔 t-abc123" in written_content

    mock_store.update.assert_called_once()
    kwargs = mock_store.update.call_args.kwargs
    assert kwargs.get("description") == "Fix the auth bug"


def test_update_description_preserves_tags(_patch_bridge_and_store):
    mock_bridge, _ = _patch_bridge_and_store
    mock_bridge.read_file.return_value = WITH_TAGS

    result = mutations.update_task_description(
        task_id="t-bbb222",
        new_description="Refactor the auth flow",
    )

    assert result["success"] is True
    written_content = mock_bridge.write_file.call_args[0][1]
    assert "#projects/wb" in written_content
    assert "#paper/ecg" in written_content
    assert "Refactor the auth flow" in written_content


def test_update_description_empty_rejected(_patch_bridge_and_store):
    mock_bridge, _ = _patch_bridge_and_store
    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="   ",
    )
    assert result["success"] is False
    assert "empty" in result["message"].lower()
    mock_bridge.write_file.assert_not_called()


def test_update_description_multiline_rejected(_patch_bridge_and_store):
    mock_bridge, _ = _patch_bridge_and_store
    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="line one\nline two",
    )
    assert result["success"] is False
    assert "single line" in result["message"].lower()
    mock_bridge.write_file.assert_not_called()


def test_update_description_no_task_id_raises(_patch_bridge_and_store):
    with pytest.raises(ValueError, match="task_id"):
        mutations.update_task_description(
            task_id="",
            new_description="anything",
        )


def test_update_description_task_not_found(_patch_bridge_and_store):
    mock_bridge, _ = _patch_bridge_and_store
    mock_bridge.read_file.return_value = "# Empty list\n"

    result = mutations.update_task_description(
        task_id="t-doesnotexist",
        new_description="anything",
    )

    assert result["success"] is False
    assert "not found" in result["message"].lower()


def test_update_description_skips_store_when_no_record(
    _patch_bridge_and_store,
):
    """If the store has no record for the task, file write proceeds
    but store.update is not called (mirrors create_task's defensive
    pattern)."""
    mock_bridge, mock_store = _patch_bridge_and_store
    mock_store.get.return_value = None

    result = mutations.update_task_description(
        task_id="t-abc123",
        new_description="New text",
    )

    assert result["success"] is True
    assert result["store_updated"] is False
    mock_store.update.assert_not_called()
