"""Tests for the generic text-segmenter SubCall.

The segmenter splits captured text into distinct *matters*. Used by
the inline pipeline to detect multi-matter captures and route each
matter to its own thread instead of conflating them into one umbrella.

The LLMRunner is mocked throughout — we're testing the segmenter's
post-parse validation + bypass + caller-shape contract, not the LLM
output itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from work_buddy.clarify.text_segmenter import (
    TEXT_SEGMENTER_SUBCALL,
    _validate_and_normalize_segments,
    segment_into_matters,
)
from work_buddy.llm.response import ErrorKind, LLMResponse, TierAttempt


def _ok(structured: dict) -> LLMResponse:
    return LLMResponse(
        content="",
        structured_output=structured,
        tier_used="local_fast",
        tier_attempts=(
            TierAttempt(
                tier="local_fast", model="qwen3-4b",
                error_kind=None, error=None,
                elapsed_ms=42, outcome="success",
            ),
        ),
        model="qwen3-4b",
    )


def _err() -> LLMResponse:
    return LLMResponse(
        content="",
        structured_output=None,
        tier_used="frontier_fast",
        tier_attempts=(
            TierAttempt(
                tier="frontier_fast", model="claude-haiku",
                error_kind=ErrorKind.TIMEOUT, error="boom",
                elapsed_ms=99, outcome="backend_error",
            ),
        ),
        error="boom",
        error_kind=ErrorKind.TIMEOUT,
    )


# ---------------------------------------------------------------------------
# SubCall declaration sanity
# ---------------------------------------------------------------------------


def test_subcall_declaration() -> None:
    assert TEXT_SEGMENTER_SUBCALL.config_key == "triage.text_segmenter"
    assert TEXT_SEGMENTER_SUBCALL.fail_policy == "soft"
    default = TEXT_SEGMENTER_SUBCALL.soft_fail_default
    assert default is not None
    assert default["segments"] == []


# ---------------------------------------------------------------------------
# Bypass paths (no LLM call)
# ---------------------------------------------------------------------------


def test_empty_text_returns_empty_list() -> None:
    runner = MagicMock()
    out = segment_into_matters("", runner=runner)
    runner.call.assert_not_called()
    assert out == []


def test_whitespace_only_text_returns_empty_list() -> None:
    runner = MagicMock()
    out = segment_into_matters("   \n\n  ", runner=runner)
    runner.call.assert_not_called()
    assert out == []


def test_short_text_bypassed_to_single_matter() -> None:
    """Texts under 120 chars (default) skip the LLM entirely."""
    runner = MagicMock()
    short = "Email Bob about the report"
    out = segment_into_matters(short, runner=runner)
    runner.call.assert_not_called()
    assert len(out) == 1
    assert out[0]["start_char"] == 0
    assert out[0]["end_char"] == len(short)
    assert out[0]["text"] == short
    assert out[0]["label"] == "(short capture)"


# ---------------------------------------------------------------------------
# LLM-driven happy path
# ---------------------------------------------------------------------------


_LONG_TEXT = (
    "I need to draft the TKA paper introduction by Monday — Bo wants "
    "the v3 outline locked in before then.\n\n"
    "Also remember: dentist appointment is Tuesday at 9am.\n"
    "Should I follow up with the JAMA reviewer about the missing figure?"
)


def test_one_segment_passthrough(monkeypatch) -> None:
    """LLM returns 1 segment → that segment is returned with text slice."""
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"triage": {"text_segmenter": {
            "tier_chain": ["local_fast"],
        }}},
    )
    runner = MagicMock()
    runner.call.return_value = _ok({
        "segments": [
            {"start_char": 0, "end_char": len(_LONG_TEXT),
             "label": "Whole capture"},
        ],
    })
    out = segment_into_matters(_LONG_TEXT, runner=runner)
    assert len(out) == 1
    assert out[0]["start_char"] == 0
    assert out[0]["end_char"] == len(_LONG_TEXT)
    assert out[0]["text"] == _LONG_TEXT
    assert out[0]["label"] == "Whole capture"


def test_multiple_segments_returned_with_text_slices(monkeypatch) -> None:
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"triage": {"text_segmenter": {
            "tier_chain": ["local_fast"],
        }}},
    )
    runner = MagicMock()
    # Three reasonable-looking segments covering most of the text.
    runner.call.return_value = _ok({
        "segments": [
            {"start_char": 0, "end_char": 100, "label": "TKA paper intro"},
            {"start_char": 102, "end_char": 155, "label": "Dentist appointment"},
            {"start_char": 156, "end_char": len(_LONG_TEXT),
             "label": "JAMA reviewer follow-up"},
        ],
    })
    out = segment_into_matters(_LONG_TEXT, runner=runner)
    assert len(out) == 3
    assert out[0]["label"] == "TKA paper intro"
    assert out[1]["label"] == "Dentist appointment"
    assert out[2]["label"] == "JAMA reviewer follow-up"
    # Each entry's `text` is the slice of the input.
    for s in out:
        assert s["text"] == _LONG_TEXT[s["start_char"]:s["end_char"]]


def test_soft_fail_returns_passthrough(monkeypatch) -> None:
    """LLM error → caller gets a single-matter passthrough, not [] —
    so the inline pipeline still spawns ONE thread for the input."""
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"triage": {"text_segmenter": {
            "tier_chain": ["local_fast"],
        }}},
    )
    runner = MagicMock()
    runner.call.return_value = _err()
    out = segment_into_matters(_LONG_TEXT, runner=runner)
    assert len(out) == 1
    assert out[0]["start_char"] == 0
    assert out[0]["end_char"] == len(_LONG_TEXT)
    assert out[0]["text"] == _LONG_TEXT
    assert out[0]["label"] == "(unsegmented)"


def test_runner_throws_returns_passthrough(monkeypatch) -> None:
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"triage": {"text_segmenter": {
            "tier_chain": ["local_fast"],
        }}},
    )
    runner = MagicMock()
    runner.call.side_effect = RuntimeError("backend down")
    out = segment_into_matters(_LONG_TEXT, runner=runner)
    assert len(out) == 1  # Passthrough, not []
    assert out[0]["text"] == _LONG_TEXT


# ---------------------------------------------------------------------------
# _validate_and_normalize_segments — pure logic
# ---------------------------------------------------------------------------


class TestValidateSegments:

    def test_drops_negative_offsets(self) -> None:
        out = _validate_and_normalize_segments(
            {"segments": [
                {"start_char": -5, "end_char": 10, "label": "bad"},
                {"start_char": 0, "end_char": 10, "label": "ok"},
            ]},
            text="0123456789",  # exactly the kept segment's range
        )
        assert len(out) == 1
        assert out[0]["label"] == "ok"

    def test_drops_inverted_ranges(self) -> None:
        out = _validate_and_normalize_segments(
            {"segments": [
                {"start_char": 10, "end_char": 5, "label": "inverted"},
            ]},
            text="0123456789abcdef",
        )
        assert out == []

    def test_drops_out_of_range(self) -> None:
        out = _validate_and_normalize_segments(
            {"segments": [
                {"start_char": 0, "end_char": 100, "label": "too long"},
            ]},
            text="hello",  # length 5
        )
        assert out == []

    def test_drops_overlapping_segments(self) -> None:
        out = _validate_and_normalize_segments(
            {"segments": [
                {"start_char": 0, "end_char": 10, "label": "first"},
                {"start_char": 5, "end_char": 15, "label": "overlap"},
                {"start_char": 16, "end_char": 25, "label": "ok"},
            ]},
            text="x" * 25,
            # Disable coverage check; we're testing overlap dropping in
            # isolation. The remaining "first" + "ok" cover 19 of 25
            # chars (76%) which would otherwise trip the floor.
            coverage_floor=0.0,
        )
        labels = [s["label"] for s in out]
        assert "first" in labels
        assert "overlap" not in labels
        assert "ok" in labels

    def test_caps_at_max_segments(self) -> None:
        out = _validate_and_normalize_segments(
            {"segments": [
                {"start_char": i*10, "end_char": (i+1)*10, "label": f"s{i}"}
                for i in range(10)
            ]},
            text="x" * 100,
            max_segments=3,
            coverage_floor=0.0,  # Cap test in isolation; coverage tested below.
        )
        assert len(out) == 3
        labels = [s["label"] for s in out]
        assert labels == ["s0", "s1", "s2"]

    def test_coverage_floor_drops_low_coverage(self) -> None:
        """Segments covering <85% of non-whitespace input → discard all."""
        long_text = "x" * 100
        out = _validate_and_normalize_segments(
            {"segments": [
                {"start_char": 0, "end_char": 10, "label": "tiny slice"},
            ]},
            text=long_text,
            coverage_floor=0.85,
        )
        assert out == []  # 10% coverage < 85% floor

    def test_coverage_ignores_whitespace(self) -> None:
        """Whitespace gaps between segments don't count against coverage."""
        # 12 non-whitespace chars total ("hello" + "world")
        text = "hello\n\n\nworld"
        out = _validate_and_normalize_segments(
            {"segments": [
                {"start_char": 0, "end_char": 5, "label": "hello"},
                {"start_char": 8, "end_char": 13, "label": "world"},
            ]},
            text=text,
        )
        # Both segments cover all non-whitespace chars; coverage ~100%.
        assert len(out) == 2

    def test_attaches_text_slice(self) -> None:
        out = _validate_and_normalize_segments(
            {"segments": [
                {"start_char": 0, "end_char": 5, "label": "first"},
                {"start_char": 6, "end_char": 11, "label": "second"},
            ]},
            text="hello,world",
        )
        assert out[0]["text"] == "hello"
        assert out[1]["text"] == "world"

    def test_empty_input_returns_empty(self) -> None:
        out = _validate_and_normalize_segments(
            {"segments": []},
            text="hello world",
        )
        assert out == []
