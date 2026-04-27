"""Slice 2 density-promotion heuristic tests."""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks.density_heuristic import (
    DensityFlag,
    count_max_bullets_per_section,
    detect_parenthetical_sublist,
    flag_task,
)


# ---------------------------------------------------------------------------
# detect_parenthetical_sublist
# ---------------------------------------------------------------------------


def test_paren_with_three_comma_items_fires() -> None:
    text = "Refactor auth (sessions, tokens, csrf)"
    assert detect_parenthetical_sublist(text) == "(sessions, tokens, csrf)"


def test_paren_with_three_slash_items_fires() -> None:
    text = "Build MCP measurement tool module (PR/QRS/QT/axis/amplitudes)"
    assert (
        detect_parenthetical_sublist(text)
        == "(PR/QRS/QT/axis/amplitudes)"
    )


def test_paren_with_and_terminal_fires() -> None:
    text = "Plan dinner (appetizer, main, and dessert)"
    assert detect_parenthetical_sublist(text) is not None


def test_single_item_paren_does_not_fire() -> None:
    """(WIP), (draft), (Q3) — single items should NOT trigger."""
    assert detect_parenthetical_sublist("Refactor auth (WIP)") is None
    assert detect_parenthetical_sublist("Q3 plan (draft)") is None


def test_two_items_does_not_fire() -> None:
    """Two items is too few — could be a noun phrase (e.g. "(Smith, 2024)")."""
    assert detect_parenthetical_sublist("Read paper (Smith, 2024)") is None


def test_no_paren_does_not_fire() -> None:
    assert detect_parenthetical_sublist("Plain task with no parens") is None
    assert detect_parenthetical_sublist("") is None


# ---------------------------------------------------------------------------
# count_max_bullets_per_section
# ---------------------------------------------------------------------------


def test_zero_bullets() -> None:
    assert count_max_bullets_per_section("# Heading\n\nJust prose.") == 0


def test_two_bullets_in_one_section() -> None:
    body = """# Plan
- Step 1
- Step 2
"""
    assert count_max_bullets_per_section(body) == 2


def test_max_across_sections() -> None:
    """Section with 5 bullets dominates section with 2."""
    body = """# A
- one
- two

# B
- 1
- 2
- 3
- 4
- 5

# C
- only one
"""
    assert count_max_bullets_per_section(body) == 5


def test_handles_numbered_lists() -> None:
    body = """# Plan
1. First
2. Second
3. Third
"""
    assert count_max_bullets_per_section(body) == 3


def test_indented_bullets_count() -> None:
    body = """# Plan
  - indented one
  - indented two
  - indented three
"""
    assert count_max_bullets_per_section(body) == 3


def test_empty_body_returns_zero() -> None:
    assert count_max_bullets_per_section("") == 0
    assert count_max_bullets_per_section(None) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# flag_task: combined signals
# ---------------------------------------------------------------------------


def test_paren_only_flag() -> None:
    flag = flag_task(
        task_id="t-paren",
        task_text="Refactor auth (sessions, tokens, csrf)",
        note_body=None,
    )
    assert flag is not None
    assert flag.signals == ["parenthetical_sublist"]
    assert "(sessions" in flag.sample_evidence


def test_bullets_only_flag() -> None:
    body = "# Plan\n- a\n- b\n- c\n- d\n"
    flag = flag_task(
        task_id="t-bullets",
        task_text="Plain task with no signal",
        note_body=body,
    )
    assert flag is not None
    assert flag.signals == ["note_has_4_bullets_in_one_section"]


def test_both_signals_fire() -> None:
    flag = flag_task(
        task_id="t-both",
        task_text="Refactor auth (sessions, tokens, csrf)",
        note_body="# Plan\n- a\n- b\n- c\n",
    )
    assert flag is not None
    assert "parenthetical_sublist" in flag.signals
    assert any("bullets" in s for s in flag.signals)


def test_no_signals_returns_none() -> None:
    flag = flag_task(
        task_id="t-none",
        task_text="Plain task",
        note_body="# Heading\n\nJust prose.",
    )
    assert flag is None


def test_threshold_respected() -> None:
    """A section with 2 bullets shouldn't fire under default threshold=3."""
    body = "# Plan\n- a\n- b\n"  # only 2
    flag = flag_task(
        task_id="t-thresh",
        task_text="Plain text",
        note_body=body,
    )
    assert flag is None
