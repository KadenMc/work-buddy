"""Unit tests for :mod:`work_buddy.llm.runner_v2`.

Focus on the parts with genuine logic: error classification, empty-content
detection, escalation loop, and tier resolution. The actual backend
dispatch (``_call_one`` → ``run_task``) is covered by the existing
``run_task`` tests and end-to-end smokes.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from work_buddy.llm.response import ErrorKind, LLMResponse, ToolCall
from work_buddy.llm.runner_v2 import (
    LLMRunner,
    _classify_error,
    _detect_empty_content,
    _normalize_escalate_on,
)
from work_buddy.llm.tiers import ModelTier


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_none_inputs_return_none(self):
        assert _classify_error(None, None) is None
        assert _classify_error("", "") is None

    def test_known_kind_string_wins(self):
        assert _classify_error("anything", "timeout") is ErrorKind.TIMEOUT
        assert _classify_error("anything", "context_exceeded") is ErrorKind.CONTEXT_EXCEEDED
        assert _classify_error("anything", "mcp_gateway_timeout") is ErrorKind.TIMEOUT

    def test_unknown_kind_falls_through_to_heuristic(self):
        # kind_str is not in map → heuristic on message text
        assert _classify_error("Context size exceeded", "weird_kind") is ErrorKind.CONTEXT_EXCEEDED

    def test_message_heuristics(self):
        assert _classify_error("Context size has been exceeded.", None) is ErrorKind.CONTEXT_EXCEEDED
        assert _classify_error("Timed out waiting", None) is ErrorKind.TIMEOUT
        assert _classify_error("Rate limit hit (429)", None) is ErrorKind.RATE_LIMITED
        assert _classify_error("invalid api key", None) is ErrorKind.AUTH
        assert _classify_error(
            "Your credit balance is too low to access the Anthropic API.",
            None,
        ) is ErrorKind.AUTH
        assert _classify_error(
            "Request denied due to insufficient credit", None,
        ) is ErrorKind.AUTH
        # Unknown text → UNKNOWN
        assert _classify_error("something weird", None) is ErrorKind.UNKNOWN

    def test_lm_studio_n_keep_n_ctx_phrasing_is_context_exceeded(self):
        """LM Studio's newer phrasing — no 'exceed', no 'too long', just
        'is greater than the context length' + n_keep/n_ctx tokens. The
        old classifier missed this and fell through to UNKNOWN (or worse,
        SCHEMA_VIOLATION when LM Studio's hint mentioned 'schema'); now
        we match the structured tokens directly."""
        msg = (
            "LM Studio rejected the request at /v1/chat/completions "
            "(HTTP 400): The number of tokens to keep from the initial "
            "prompt is greater than the context length "
            "(n_keep: 4103 >= n_ctx: 4096). Try to load the model with a "
            "larger context length, or provide a shorter input."
        )
        assert _classify_error(msg, None) is ErrorKind.CONTEXT_EXCEEDED

    def test_context_exceeded_beats_schema_when_both_words_present(self):
        """LM Studio's overflow error includes a fallback hint mentioning
        "the native-endpoint schema may have changed" — this used to win
        over the (correct) context-exceeded reading. Rule order now puts
        CONTEXT_EXCEEDED first."""
        msg = (
            "n_keep: 4103 >= n_ctx: 4096. Hint: The request payload was "
            "rejected. This is usually a shape mismatch between our "
            "backend and the LM Studio version in use. If you just "
            "upgraded LM Studio, the native-endpoint schema may have "
            "changed."
        )
        assert _classify_error(msg, None) is ErrorKind.CONTEXT_EXCEEDED

    def test_schema_violation_requires_actual_violation_signal(self):
        """The narrowed SCHEMA_VIOLATION rule needs both 'schema' AND a
        violation/invalid/mismatch/unexpected word — not just 'schema'
        alone (which was the old over-greedy bug)."""
        # Real schema violation — should match.
        assert _classify_error("Output schema violation: missing 'foo'", None) is ErrorKind.SCHEMA_VIOLATION
        assert _classify_error("Schema mismatch on field 'bar'", None) is ErrorKind.SCHEMA_VIOLATION
        assert _classify_error("Invalid JSON in response", None) is ErrorKind.SCHEMA_VIOLATION
        # 'schema' alone in an unrelated hint must NOT match.
        assert _classify_error("Hint: the schema may have been updated", None) is ErrorKind.UNKNOWN

    def test_context_exceeded_alternate_phrasings(self):
        """Cover the various LM Studio / OpenAI-compat phrasings."""
        cases = [
            "Prompt is too long for this context window",
            "Input is too long for this model's context",
            "context length is greater than max",
            "load the model with a larger context length",
            "Context size has been exceeded",
        ]
        for msg in cases:
            assert _classify_error(msg, None) is ErrorKind.CONTEXT_EXCEEDED, msg


# ---------------------------------------------------------------------------
# Empty content detection
# ---------------------------------------------------------------------------


class TestDetectEmptyContent:
    def test_empty_string_no_structured_no_tools(self):
        assert _detect_empty_content("", None, ()) is True

    def test_whitespace_only_treated_as_empty(self):
        assert _detect_empty_content("   \n\t  ", None, ()) is True

    def test_content_not_empty(self):
        assert _detect_empty_content("hi", None, ()) is False

    def test_structured_output_not_empty(self):
        assert _detect_empty_content("", {"k": "v"}, ()) is False
        # Empty-dict structured still counts as non-empty (caller passed schema)
        assert _detect_empty_content("", {}, ()) is False

    def test_tool_calls_not_empty(self):
        tc = ToolCall(name="x")
        assert _detect_empty_content("", None, (tc,)) is False


# ---------------------------------------------------------------------------
# normalize_escalate_on
# ---------------------------------------------------------------------------


class TestNormalizeEscalateOn:
    def test_none(self):
        assert _normalize_escalate_on(None) == set()

    def test_mixed_types(self):
        out = _normalize_escalate_on([ErrorKind.TIMEOUT, "context_exceeded"])
        assert out == {ErrorKind.TIMEOUT, ErrorKind.CONTEXT_EXCEEDED}

    def test_unknown_string_skipped(self):
        # Unknown strings should not crash — they're skipped with a warning.
        out = _normalize_escalate_on(["not_a_real_kind", "timeout"])
        assert out == {ErrorKind.TIMEOUT}


# ---------------------------------------------------------------------------
# Escalation integration
# ---------------------------------------------------------------------------


@dataclass
class _FakeTaskResult:
    content: str = ""
    parsed: dict | None = None
    model: str = "fake-model"
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False
    cache_key: str | None = None
    error: str | None = None


def _fake_run_task_factory(scripted):
    """Return a run_task stub that yields scripted results in order.

    Each element of ``scripted`` is either a :class:`_FakeTaskResult`
    (single attempt's return) or an ``Exception`` to raise.
    """
    it = iter(scripted)

    def _fake(**kwargs):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return _fake


class TestEscalation:
    def test_success_on_first_tier_no_escalation(self):
        scripted = [_FakeTaskResult(content="yes")]
        with patch("work_buddy.llm.runner.run_task",
                   side_effect=_fake_run_task_factory(scripted)):
            resp = LLMRunner().call(
                tier=ModelTier.FRONTIER_FAST,
                system="s", user="u",
                escalate_on=[ErrorKind.TIMEOUT],
                escalate_to=[ModelTier.FRONTIER_BALANCED],
            )
        assert resp.content == "yes"
        assert resp.is_error() is False
        assert resp.tier_used == ModelTier.FRONTIER_FAST.value
        assert len(resp.tier_attempts) == 1
        assert resp.tier_attempts[0].error_kind is None

    def test_timeout_escalates_to_next_tier(self):
        scripted = [
            _FakeTaskResult(error="Timed out waiting for backend"),
            _FakeTaskResult(content="recovered"),
        ]
        with patch("work_buddy.llm.runner.run_task",
                   side_effect=_fake_run_task_factory(scripted)):
            resp = LLMRunner().call(
                tier=ModelTier.FRONTIER_FAST,
                system="s", user="u",
                escalate_on=[ErrorKind.TIMEOUT],
                escalate_to=[ModelTier.FRONTIER_BALANCED],
            )
        assert resp.content == "recovered"
        assert resp.is_error() is False
        assert resp.tier_used == ModelTier.FRONTIER_BALANCED.value
        assert len(resp.tier_attempts) == 2
        assert resp.tier_attempts[0].error_kind is ErrorKind.TIMEOUT
        assert resp.tier_attempts[1].error_kind is None

    def test_empty_content_escalates_when_in_policy(self):
        scripted = [
            _FakeTaskResult(content=""),          # empty → should escalate
            _FakeTaskResult(content="not empty"),
        ]
        with patch("work_buddy.llm.runner.run_task",
                   side_effect=_fake_run_task_factory(scripted)):
            resp = LLMRunner().call(
                tier=ModelTier.LOCAL_TOOL_CALLING,
                system="s", user="u",
                escalate_on=[ErrorKind.EMPTY_CONTENT],
                escalate_to=[ModelTier.FRONTIER_BALANCED],
            )
        assert resp.content == "not empty"
        assert resp.tier_used == ModelTier.FRONTIER_BALANCED.value
        assert resp.tier_attempts[0].error_kind is ErrorKind.EMPTY_CONTENT

    def test_empty_content_does_not_escalate_when_not_in_policy(self):
        scripted = [_FakeTaskResult(content="")]
        with patch("work_buddy.llm.runner.run_task",
                   side_effect=_fake_run_task_factory(scripted)):
            resp = LLMRunner().call(
                tier=ModelTier.LOCAL_TOOL_CALLING,
                system="s", user="u",
                escalate_on=[ErrorKind.TIMEOUT],   # not EMPTY_CONTENT
                escalate_to=[ModelTier.FRONTIER_BALANCED],
            )
        assert resp.is_error() is True
        assert resp.error_kind is ErrorKind.EMPTY_CONTENT
        assert resp.tier_used == ModelTier.LOCAL_TOOL_CALLING.value
        assert len(resp.tier_attempts) == 1

    def test_all_tiers_fail_returns_last_error(self):
        scripted = [
            _FakeTaskResult(error="Timed out"),
            _FakeTaskResult(error="Context size exceeded"),
        ]
        with patch("work_buddy.llm.runner.run_task",
                   side_effect=_fake_run_task_factory(scripted)):
            resp = LLMRunner().call(
                tier=ModelTier.FRONTIER_FAST,
                system="s", user="u",
                escalate_on=[ErrorKind.TIMEOUT, ErrorKind.CONTEXT_EXCEEDED],
                escalate_to=[ModelTier.FRONTIER_BALANCED],
            )
        assert resp.is_error() is True
        assert resp.error_kind is ErrorKind.CONTEXT_EXCEEDED
        assert len(resp.tier_attempts) == 2
        # tier_used reflects the last tier tried (where we gave up).
        assert resp.tier_used == ModelTier.FRONTIER_BALANCED.value

    def test_non_escalating_error_halts_chain_early(self):
        # AUTH errors shouldn't escalate through multiple tiers — second
        # tier would just hit the same auth problem.
        scripted = [_FakeTaskResult(error="invalid api key")]
        with patch("work_buddy.llm.runner.run_task",
                   side_effect=_fake_run_task_factory(scripted)):
            resp = LLMRunner().call(
                tier=ModelTier.FRONTIER_FAST,
                system="s", user="u",
                escalate_on=[ErrorKind.TIMEOUT],    # AUTH not in policy
                escalate_to=[ModelTier.FRONTIER_BALANCED, ModelTier.FRONTIER_BEST],
            )
        assert resp.is_error() is True
        assert resp.error_kind is ErrorKind.AUTH
        assert len(resp.tier_attempts) == 1  # only one attempt — chain halted

    def test_backend_exception_becomes_unknown_error(self):
        scripted = [RuntimeError("boom")]
        with patch("work_buddy.llm.runner.run_task",
                   side_effect=_fake_run_task_factory(scripted)):
            resp = LLMRunner().call(
                tier=ModelTier.FRONTIER_FAST,
                system="s", user="u",
            )
        assert resp.is_error() is True
        assert resp.error_kind is ErrorKind.UNKNOWN
        assert "boom" in (resp.error or "")


# ---------------------------------------------------------------------------
# Tools parameter — phase 1 scope guard
# ---------------------------------------------------------------------------


def test_tools_param_raises_in_phase_1():
    with pytest.raises(NotImplementedError, match="phase 3"):
        LLMRunner().call(
            tier=ModelTier.FRONTIER_FAST,
            system="s", user="u",
            tools=["triage_submit"],
        )


def test_empty_tools_list_does_not_raise():
    # None and [] are both fine — the phase-1 guard only trips on
    # actually-requested tools.
    scripted = [_FakeTaskResult(content="ok")]
    with patch("work_buddy.llm.runner.run_task",
               side_effect=_fake_run_task_factory(scripted)):
        resp = LLMRunner().call(
            tier=ModelTier.FRONTIER_FAST,
            system="s", user="u",
            tools=[],
        )
    assert resp.content == "ok"


# ---------------------------------------------------------------------------
# to_legacy_dict round-trip
# ---------------------------------------------------------------------------


def test_to_legacy_dict_shape():
    r = LLMResponse(
        content="hi",
        structured_output={"k": "v"},
        model="m", input_tokens=10, output_tokens=20,
        cached=False, cache_key="key",
        error=None, error_kind=None,
    )
    d = r.to_legacy_dict()
    # Legacy dict mirrors TaskResult-ish shape.
    assert d["content"] == "hi"
    assert d["parsed"] == {"k": "v"}
    assert d["model"] == "m"
    assert d["input_tokens"] == 10
    assert d["error"] is None
    assert d["error_kind"] is None
    assert d["tool_calls"] == []
