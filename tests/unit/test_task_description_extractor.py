"""Slice 3: extract_description_from_line — clean human-readable text
extraction from a master-task-list line.

Mirrors the sub-extraction that previously lived inside
``_load_task_payload``. Now the canonical extractor for the store's
description column AND the file-vs-store reconciliation in task_sync.
"""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks.mutations import extract_description_from_line


@pytest.mark.parametrize(
    "line,expected",
    [
        # Vanilla task line.
        (
            "- [ ] #todo Fix the bug 🆔 t-abc123",
            "Fix the bug",
        ),
        # Done task with date.
        (
            "- [x] #todo Fix the bug 🆔 t-abc123 ✅ 2026-04-27",
            "Fix the bug",
        ),
        # With project tag and due date.
        (
            "- [ ] #todo Refactor auth #projects/work-buddy 🆔 t-zzz999 📅 2026-04-30",
            "Refactor auth",
        ),
        # With note wikilink.
        (
            "- [ ] #todo Build the dashboard [[abc-def-ghi|📓]] 🆔 t-deadbe",
            "Build the dashboard",
        ),
        # With multiple namespace tags.
        (
            "- [ ] #todo Train the ECG model #paper/ecg-classifier #experiment/augmentation 🆔 t-cafe01",
            "Train the ECG model",
        ),
        # Urgency emoji.
        (
            "- [ ] #todo High-priority work 🔼 🆔 t-fff000",
            "High-priority work",
        ),
        (
            "- [ ] #todo Critical thing ⏫ 🆔 t-fff111",
            "Critical thing",
        ),
        # Whitespace-collapse: multiple spaces between tokens.
        (
            "- [ ] #todo   Lots of   space   🆔 t-spc",
            "Lots of space",
        ),
        # Leading whitespace-checkbox.
        (
            "    - [ ] #todo indented 🆔 t-ind",
            "indented",
        ),
        # Multi-character description.
        (
            "- [ ] #todo This is a longer description with punctuation, parentheses (yes), and dashes — like so. 🆔 t-l1",
            "This is a longer description with punctuation, parentheses (yes), and dashes — like so.",
        ),
    ],
)
def test_extracts_description(line: str, expected: str) -> None:
    assert extract_description_from_line(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
    ],
)
def test_empty_input_returns_empty(line: str) -> None:
    assert extract_description_from_line(line) == ""


def test_only_emoji_and_tags_returns_empty() -> None:
    """If there's no human text after stripping, return empty string."""
    line = "- [ ] #todo #projects/foo 🆔 t-empty"
    assert extract_description_from_line(line) == ""


def test_legacy_tasker_state_tag_stripped() -> None:
    """Legacy `#tasker/state/inbox` style tags should be stripped along with everything else."""
    line = "- [ ] #todo Old-style task #tasker/state/inbox #tasker/urgency/high 🆔 t-old"
    assert extract_description_from_line(line) == "Old-style task"


def test_done_date_emoji_stripped() -> None:
    line = "- [x] #todo Completed item 🆔 t-c1 ✅ 2026-04-27"
    assert extract_description_from_line(line) == "Completed item"
