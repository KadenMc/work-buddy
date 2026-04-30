"""Slice 3 tests: deadline + dependency Haiku pre-pass.

The function makes real LLM calls when given non-empty text, so most
tests stub the runner; a couple of pure-logic tests (empty text,
sentinel structure, merge logic) need no mock.
"""

from __future__ import annotations

from datetime import date as _date
from unittest.mock import MagicMock, patch

import pytest

from work_buddy.clarify import deadline_extract
from work_buddy.clarify.deadline_extract import (
    _failure_sentinel,
    extract_deadline_hints,
    merge_hints_into_records,
)


# ---------------------------------------------------------------------------
# Empty-text + sentinel paths (no LLM call expected)
# ---------------------------------------------------------------------------


def test_empty_text_returns_no_hints() -> None:
    out = extract_deadline_hints("")
    assert out["has_deadline"] is False
    assert out["deadline_date"] is None
    assert out["has_dependency"] is False
    assert out["dependency_hint"] is None


def test_whitespace_only_returns_no_hints() -> None:
    out = extract_deadline_hints("   \n\n  ")
    assert out["has_deadline"] is False
    assert out["has_dependency"] is False


def test_failure_sentinel_shape() -> None:
    s = _failure_sentinel()
    assert s["has_deadline"] is False
    assert s["deadline_date"] is None
    assert s["has_dependency"] is False
    assert s["dependency_hint"] is None
    assert s["hint_extraction_failed"] is True


# ---------------------------------------------------------------------------
# merge_hints_into_records — pure-logic tests
# ---------------------------------------------------------------------------


def test_merge_hints_into_empty_records() -> None:
    assert merge_hints_into_records(None, {"has_deadline": True}) == []
    assert merge_hints_into_records([], {"has_deadline": True}) == []


def test_merge_hints_skips_non_task_records() -> None:
    records = [
        {"destination": "task", "task_proposal": {"suggested_task_text": "t"}},
        {"destination": "delete", "delete_reason": "duplicate"},
        {"destination": "reference", "reference_proposal": {"summary": "s"}},
        {"destination": "calendar_only", "calendar_proposal": {"title": "c"}},
    ]
    hints = {
        "has_deadline": True, "deadline_date": "2026-05-15",
        "has_dependency": False, "dependency_hint": None,
    }
    merge_hints_into_records(records, hints)
    # Only the task record should have hint fields stamped.
    assert records[0]["task_proposal"]["has_deadline"] is True
    assert records[0]["task_proposal"]["deadline_date"] == "2026-05-15"
    # Other destinations untouched.
    assert "has_deadline" not in records[1]
    assert "has_deadline" not in records[2]
    assert "has_deadline" not in records[3]


def test_merge_hints_preserves_sonnet_supplied_values() -> None:
    """If the Sonnet verdict already set has_deadline / deadline_date,
    the Haiku hints must NOT overwrite. Sonnet's calibration on the
    actual rationale wins."""
    records = [{
        "destination": "task",
        "task_proposal": {
            "suggested_task_text": "t",
            "has_deadline": True,
            "deadline_date": "2026-04-30",
        },
    }]
    hints = {
        "has_deadline": True, "deadline_date": "2026-05-15",
        "has_dependency": False, "dependency_hint": None,
    }
    merge_hints_into_records(records, hints)
    # Sonnet's deadline preserved, NOT overwritten by Haiku.
    assert records[0]["task_proposal"]["deadline_date"] == "2026-04-30"


def test_merge_hints_stamps_missing_fields() -> None:
    records = [{
        "destination": "task",
        "task_proposal": {"suggested_task_text": "t"},
    }]
    hints = {
        "has_deadline": True, "deadline_date": "2026-05-15",
        "has_dependency": True, "dependency_hint": "Bob's review",
    }
    merge_hints_into_records(records, hints)
    p = records[0]["task_proposal"]
    assert p["has_deadline"] is True
    assert p["deadline_date"] == "2026-05-15"
    assert p["has_dependency"] is True
    assert p["dependency_hint"] == "Bob's review"


def test_merge_hints_handles_missing_task_proposal() -> None:
    """Records missing a task_proposal should be left alone (defensive
    against malformed verdicts)."""
    records = [{"destination": "task"}]  # No task_proposal!
    merge_hints_into_records(records, {"has_deadline": True})
    # No task_proposal added — the function only stamps existing ones.
    assert "task_proposal" not in records[0]


def test_merge_hints_no_hints_supplied_noop() -> None:
    records = [{
        "destination": "task",
        "task_proposal": {"suggested_task_text": "t"},
    }]
    merge_hints_into_records(records, {})  # Empty hints dict
    # Original record untouched.
    assert records[0]["task_proposal"] == {"suggested_task_text": "t"}


# ---------------------------------------------------------------------------
# extract_deadline_hints — LLM mocked
# ---------------------------------------------------------------------------


class _MockResponse:
    def __init__(
        self, structured_output: dict | None = None,
        is_error_val: bool = False,
        error: str | None = None,
        error_kind: object = None,
        tier_used: str = "frontier_fast",
    ) -> None:
        self.structured_output = structured_output
        self._is_error = is_error_val
        self.error = error
        self.error_kind = error_kind
        self.tier_used = tier_used

    def is_error(self) -> bool:
        return self._is_error


def test_extract_deadline_hints_happy_path(monkeypatch) -> None:
    """When the LLM returns a clean structured output, the function
    normalizes it (booleans + nullable strings)."""
    fake_runner = MagicMock()
    fake_runner.call.return_value = _MockResponse(structured_output={
        "has_deadline": True,
        "deadline_date": "2026-05-15",
        "has_dependency": False,
        "dependency_hint": None,
    })
    monkeypatch.setattr(deadline_extract, "_get_runner", lambda: fake_runner)

    out = extract_deadline_hints(
        "Send the recommendation letter by Friday.",
        message_date=_date(2026, 5, 1),
    )
    assert out["has_deadline"] is True
    assert out["deadline_date"] == "2026-05-15"
    assert out["has_dependency"] is False
    assert out["dependency_hint"] is None


def test_extract_deadline_hints_llm_error_returns_sentinel(monkeypatch) -> None:
    """When the LLM errors, the function returns the all-false
    sentinel + ``hint_extraction_failed=True`` so the caller can
    log graceful degradation."""
    fake_runner = MagicMock()
    fake_runner.call.return_value = _MockResponse(
        is_error_val=True, error="timeout", error_kind="TIMEOUT",
    )
    monkeypatch.setattr(deadline_extract, "_get_runner", lambda: fake_runner)

    out = extract_deadline_hints("some text")
    assert out["has_deadline"] is False
    assert out["hint_extraction_failed"] is True


def test_extract_deadline_hints_runner_throws_returns_sentinel(monkeypatch) -> None:
    """Even if the runner itself raises (e.g. network error before the
    response builder), the function should NOT propagate — it returns
    the sentinel instead."""
    fake_runner = MagicMock()
    fake_runner.call.side_effect = RuntimeError("boom")
    monkeypatch.setattr(deadline_extract, "_get_runner", lambda: fake_runner)

    out = extract_deadline_hints("some text")
    assert out["has_deadline"] is False
    assert out["hint_extraction_failed"] is True


def test_extract_deadline_hints_normalizes_booleans(monkeypatch) -> None:
    """The function coerces truthy non-bool values to bool. (LLMs
    occasionally emit 'true'/'false' strings.)"""
    fake_runner = MagicMock()
    fake_runner.call.return_value = _MockResponse(structured_output={
        "has_deadline": "true",  # String, not bool — defensive cast
        "deadline_date": "2026-05-15",
        "has_dependency": 0,
        "dependency_hint": "",
    })
    monkeypatch.setattr(deadline_extract, "_get_runner", lambda: fake_runner)

    out = extract_deadline_hints("text")
    assert out["has_deadline"] is True  # bool('true') == True
    assert out["has_dependency"] is False  # bool(0) == False
    # Empty string → None for dependency_hint.
    assert out["dependency_hint"] is None


def test_extract_deadline_hints_message_date_string(monkeypatch) -> None:
    """ISO string for message_date is accepted alongside date objects."""
    fake_runner = MagicMock()
    fake_runner.call.return_value = _MockResponse(structured_output={
        "has_deadline": False,
        "has_dependency": False,
    })
    monkeypatch.setattr(deadline_extract, "_get_runner", lambda: fake_runner)

    out = extract_deadline_hints("text", message_date="2026-05-01")
    # Just confirm no exception + sentinel structure.
    assert out["has_deadline"] is False
