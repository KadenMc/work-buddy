"""Tests for the deadline + dependency pre-pass.

The pre-pass runs through the decomposed-judgment framework
(:func:`work_buddy.llm.run_subcall` against
:data:`work_buddy.clarify.deadline_extract.DEADLINE_HINTS_SUBCALL`),
so the test pattern is:

- Construct a real :class:`LLMResponse` for the desired outcome.
- Build a ``MagicMock`` runner whose ``.call`` returns that response.
- Pass the mock as ``runner=`` to :func:`extract_deadline_hints` (or
  monkeypatch :func:`_get_runner` for callers that don't accept the
  kwarg).

Pure-logic tests (empty text, sentinel structure, merge logic) need
no mock at all.
"""

from __future__ import annotations

from datetime import date as _date
from unittest.mock import MagicMock

import pytest

from work_buddy.clarify import deadline_extract
from work_buddy.clarify.deadline_extract import (
    DEADLINE_HINTS_SUBCALL,
    _failure_sentinel,
    extract_deadline_hints,
    merge_hints_into_records,
)
from work_buddy.llm.response import ErrorKind, LLMResponse, TierAttempt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(structured: dict, tier: str = "local_fast") -> LLMResponse:
    return LLMResponse(
        content="",
        structured_output=structured,
        tier_used=tier,
        tier_attempts=(
            TierAttempt(
                tier=tier, model="qwen3-4b", error_kind=None, error=None,
                elapsed_ms=42, outcome="success",
            ),
        ),
        model="qwen3-4b",
    )


def _err(kind: ErrorKind = ErrorKind.TIMEOUT, tier: str = "frontier_fast") -> LLMResponse:
    return LLMResponse(
        content="",
        structured_output=None,
        tier_used=tier,
        tier_attempts=(
            TierAttempt(
                tier=tier, model="claude-haiku", error_kind=kind, error="boom",
                elapsed_ms=99, outcome="backend_error",
            ),
        ),
        model="claude-haiku",
        error="boom",
        error_kind=kind,
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
    assert records[0]["task_proposal"]["has_deadline"] is True
    assert records[0]["task_proposal"]["deadline_date"] == "2026-05-15"
    assert "has_deadline" not in records[1]
    assert "has_deadline" not in records[2]
    assert "has_deadline" not in records[3]


def test_merge_hints_preserves_verdict_supplied_values() -> None:
    """If the verdict already set has_deadline / deadline_date,
    the pre-pass hints must NOT overwrite. The verdict's calibration on
    the actual rationale wins."""
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
    records = [{"destination": "task"}]
    merge_hints_into_records(records, {"has_deadline": True})
    assert "task_proposal" not in records[0]


def test_merge_hints_no_hints_supplied_noop() -> None:
    records = [{
        "destination": "task",
        "task_proposal": {"suggested_task_text": "t"},
    }]
    merge_hints_into_records(records, {})
    assert records[0]["task_proposal"] == {"suggested_task_text": "t"}


# ---------------------------------------------------------------------------
# extract_deadline_hints — LLM mocked
# ---------------------------------------------------------------------------


def test_extract_deadline_hints_happy_path() -> None:
    """When the LLM returns a clean structured output, the function
    normalizes it (booleans + nullable strings)."""
    fake_runner = MagicMock()
    fake_runner.call.return_value = _ok({
        "has_deadline": True,
        "deadline_date": "2026-05-15",
        "has_dependency": False,
        "dependency_hint": None,
    })

    out = extract_deadline_hints(
        "Send the recommendation letter by Friday.",
        message_date=_date(2026, 5, 1),
        runner=fake_runner,
    )
    assert out["has_deadline"] is True
    assert out["deadline_date"] == "2026-05-15"
    assert out["has_dependency"] is False
    assert out["dependency_hint"] is None


def test_extract_deadline_hints_llm_error_returns_sentinel() -> None:
    """When the LLM errors, the function returns the all-false
    sentinel + ``hint_extraction_failed=True`` so the caller can
    log graceful degradation."""
    fake_runner = MagicMock()
    fake_runner.call.return_value = _err()

    out = extract_deadline_hints("some text", runner=fake_runner)
    assert out["has_deadline"] is False
    assert out["hint_extraction_failed"] is True


def test_extract_deadline_hints_runner_throws_returns_sentinel() -> None:
    """Even if the runner itself raises, the function should NOT
    propagate — it returns the sentinel instead."""
    fake_runner = MagicMock()
    fake_runner.call.side_effect = RuntimeError("boom")

    out = extract_deadline_hints("some text", runner=fake_runner)
    assert out["has_deadline"] is False
    assert out["hint_extraction_failed"] is True


def test_extract_deadline_hints_normalizes_booleans() -> None:
    """The function coerces truthy non-bool values to bool. (LLMs
    occasionally emit 'true'/'false' strings.)"""
    fake_runner = MagicMock()
    fake_runner.call.return_value = _ok({
        "has_deadline": "true",  # String, not bool — defensive cast
        "deadline_date": "2026-05-15",
        "has_dependency": 0,
        "dependency_hint": "",
    })

    out = extract_deadline_hints("text", runner=fake_runner)
    assert out["has_deadline"] is True   # bool('true') is True
    assert out["has_dependency"] is False  # bool(0) is False
    assert out["dependency_hint"] is None  # empty string → None


def test_extract_deadline_hints_message_date_string() -> None:
    """ISO string for message_date is accepted alongside date objects."""
    fake_runner = MagicMock()
    fake_runner.call.return_value = _ok({
        "has_deadline": False,
        "has_dependency": False,
    })
    out = extract_deadline_hints(
        "text", message_date="2026-05-01", runner=fake_runner,
    )
    assert out["has_deadline"] is False


# ---------------------------------------------------------------------------
# Tier-chain assertion: framework reads from triage.deadline_extract
# ---------------------------------------------------------------------------


def test_extract_deadline_hints_walks_local_first_tier_chain(monkeypatch) -> None:
    """The SubCall framework reads triage.deadline_extract.tier_chain
    from config; the first entry becomes ``tier=`` and the rest become
    ``escalate_to=``.

    Triage config goes through ``load_triage_config`` which deep-merges
    TRIAGE_DEFAULTS with the user's YAML overrides. We monkey-patch the
    raw YAML loader to be empty so TRIAGE_DEFAULTS supplies the chain
    entirely from in-code defaults.
    """
    from work_buddy.llm.tiers import ModelTier

    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})

    fake_runner = MagicMock()
    fake_runner.call.return_value = _ok({
        "has_deadline": False, "has_dependency": False,
    })

    extract_deadline_hints("hello", runner=fake_runner)

    kwargs = fake_runner.call.call_args.kwargs
    # TRIAGE_DEFAULTS["deadline_extract"]["tier_chain"] is
    # ["local_tool_calling", "local_fast", "frontier_fast"]
    assert kwargs["tier"] == ModelTier.LOCAL_TOOL_CALLING
    assert kwargs["escalate_to"] == [
        ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST,
    ]


def test_extract_deadline_hints_subcall_declaration() -> None:
    """The SubCall declaration carries the canonical config_key + soft-fail."""
    assert DEADLINE_HINTS_SUBCALL.config_key == "triage.deadline_extract"
    assert DEADLINE_HINTS_SUBCALL.fail_policy == "soft"
    assert DEADLINE_HINTS_SUBCALL.soft_fail_default is not None
    assert DEADLINE_HINTS_SUBCALL.soft_fail_default["hint_extraction_failed"] is True


# ---------------------------------------------------------------------------
# Back-compat: the legacy `_get_runner` is still importable so old test
# patches and any out-of-tree callers don't break.
# ---------------------------------------------------------------------------


def test_get_runner_singleton_preserved(monkeypatch) -> None:
    """_get_runner is the canonical singleton accessor, kept for back-compat.
    Monkey-patching it must redirect the underlying call."""
    fake = MagicMock()
    fake.call.return_value = _ok({"has_deadline": False, "has_dependency": False})
    monkeypatch.setattr(deadline_extract, "_get_runner", lambda: fake)

    out = extract_deadline_hints("text")
    assert out["has_deadline"] is False
    fake.call.assert_called_once()
