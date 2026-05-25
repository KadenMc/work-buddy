"""Tests for the thin `llm.summarize` shims over the framework.

The shims delegate prompt / schema / parse to `FlatExtractionStrategy` and
adapt `SummaryNode` back into `PageSummary` for legacy callers. These tests
patch `LLMRunner.call` to verify the orchestration without touching real
LLM endpoints — they complement the strategy-level tests in
`test_summarization_framework.py` and end-to-end tests in
`test_chrome_summarization.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from work_buddy.llm.summarize import (
    PageSummary,
    TypedEntity,
    summarize,
    summarize_batch,
)


@dataclass
class _FakeLLMResponse:
    structured_output: dict | None = None
    content: str = ""
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    model: str = ""
    backend: str = ""

    def is_error(self) -> bool:
        return self.error is not None


def _make_single_response(
    summary_text: str = "A page about X",
    entities: list[dict] | None = None,
    cached: bool = False,
) -> _FakeLLMResponse:
    return _FakeLLMResponse(
        structured_output={
            "content_summary": summary_text,
            "entities": entities or [{"name": "X", "type": "concept", "context": "appears here"}],
            "key_claims": ["X is interesting"],
            "user_intent_speculation": "they want to know about X",
            "user_posture": "researching",
        },
        content="",
        cached=cached,
        input_tokens=120,
        output_tokens=80,
    )


def _make_batch_response(n: int) -> _FakeLLMResponse:
    return _FakeLLMResponse(
        structured_output={
            "summaries": [
                {
                    "item_index": i,
                    "content_summary": f"summary {i}",
                    "entities": [
                        {"name": f"E{i}", "type": "concept", "context": "here"},
                    ],
                    "key_claims": [f"claim {i}"],
                    "user_intent_speculation": "spec",
                    "user_posture": "researching",
                }
                for i in range(n)
            ],
        },
        content="",
        cached=False,
        input_tokens=200,
        output_tokens=150,
    )


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_returns_pagesummary_with_strategy_parsed_fields(monkeypatch):
    captured: list[dict] = []

    def fake_call(self, **kwargs):
        captured.append(kwargs)
        return _make_single_response("X is a concept")

    from work_buddy.llm import runner_v2
    monkeypatch.setattr(runner_v2.LLMRunner, "call", fake_call)

    result = summarize("the content here", label="My Label")

    assert isinstance(result, PageSummary)
    assert result.content_summary == "X is a concept"
    assert result.source_label == "My Label"
    assert result.user_posture == "researching"
    assert isinstance(result.entities[0], TypedEntity)
    assert result.entities[0].name == "X"
    assert result.tokens == {"input": 120, "output": 80}
    # The shim passed the strategy's system prompt through.
    assert "extract structured facts" in captured[0]["system"].lower()
    # And the strategy's single-item schema (not the batch schema).
    assert captured[0]["output_schema"].get("required") == [
        "content_summary",
        "entities",
        "key_claims",
        "user_intent_speculation",
        "user_posture",
    ]


def test_summarize_returns_failure_pagesummary_on_llm_error(monkeypatch):
    def fake_call(self, **kwargs):
        return _FakeLLMResponse(error="upstream timeout")

    from work_buddy.llm import runner_v2
    monkeypatch.setattr(runner_v2.LLMRunner, "call", fake_call)

    result = summarize("content", label="L")

    assert "Summarization failed" in result.content_summary
    assert "upstream timeout" in result.content_summary
    assert result.source_label == "L"
    assert result.entities == []


def test_summarize_returns_failure_pagesummary_on_parse_error(monkeypatch):
    def fake_call(self, **kwargs):
        # Structured output missing required `content_summary` -> strategy.parse raises.
        return _FakeLLMResponse(
            structured_output={"entities": []},
        )

    from work_buddy.llm import runner_v2
    monkeypatch.setattr(runner_v2.LLMRunner, "call", fake_call)

    result = summarize("content", label="L")

    assert "Summarization failed" in result.content_summary
    assert "parse error" in result.content_summary
    assert result.source_label == "L"


def test_summarize_passes_cache_ttl_through(monkeypatch):
    captured: dict = {}

    def fake_call(self, **kwargs):
        captured.update(kwargs)
        return _make_single_response()

    from work_buddy.llm import runner_v2
    monkeypatch.setattr(runner_v2.LLMRunner, "call", fake_call)

    summarize("text", label="L", cache_ttl_minutes=42)

    assert captured["cache_ttl_minutes"] == 42


# ---------------------------------------------------------------------------
# summarize_batch
# ---------------------------------------------------------------------------


def test_summarize_batch_empty_short_circuits():
    assert summarize_batch([]) == []


def test_summarize_batch_returns_aligned_pagesummaries(monkeypatch):
    captured: dict = {}

    def fake_call(self, **kwargs):
        captured.update(kwargs)
        return _make_batch_response(3)

    from work_buddy.llm import runner_v2
    monkeypatch.setattr(runner_v2.LLMRunner, "call", fake_call)

    items = [
        {"text": "alpha content", "label": "Alpha"},
        {"text": "beta content", "label": "Beta"},
        {"text": "gamma content", "label": "Gamma"},
    ]
    results = summarize_batch(items)

    assert len(results) == 3
    assert all(isinstance(r, PageSummary) for r in results)
    assert results[0].content_summary == "summary 0"
    assert results[2].content_summary == "summary 2"
    assert results[1].source_label == "Beta"
    # The batch schema was used, not the single-item schema.
    assert captured["output_schema"].get("required") == ["summaries"]


def test_summarize_batch_returns_empty_for_missing_response_items(monkeypatch):
    """If the LLM returns fewer summaries than items, the missing ones get
    empty `PageSummary` objects so the consumer can index by position."""

    def fake_call(self, **kwargs):
        # Only one summary even though we'll send three items.
        return _FakeLLMResponse(
            structured_output={
                "summaries": [{
                    "item_index": 0,
                    "content_summary": "alpha summary",
                    "entities": [],
                    "key_claims": [],
                    "user_intent_speculation": "",
                    "user_posture": "researching",
                }],
            },
            input_tokens=30, output_tokens=20,
        )

    from work_buddy.llm import runner_v2
    monkeypatch.setattr(runner_v2.LLMRunner, "call", fake_call)

    items = [
        {"text": "a", "label": "Alpha"},
        {"text": "b", "label": "Beta"},
        {"text": "c", "label": "Gamma"},
    ]
    results = summarize_batch(items)

    assert len(results) == 3
    assert results[0].content_summary == "alpha summary"
    assert results[1].content_summary == ""
    assert results[1].source_label == "Beta"
    assert results[2].content_summary == ""


def test_summarize_batch_returns_failure_pagesummaries_on_error(monkeypatch):
    def fake_call(self, **kwargs):
        return _FakeLLMResponse(error="batch failed")

    from work_buddy.llm import runner_v2
    monkeypatch.setattr(runner_v2.LLMRunner, "call", fake_call)

    items = [
        {"text": "x", "label": "X"},
        {"text": "y", "label": "Y"},
    ]
    results = summarize_batch(items)

    assert len(results) == 2
    assert all("Summarization failed" in r.content_summary for r in results)
    assert results[0].source_label == "X"
    assert results[1].source_label == "Y"
