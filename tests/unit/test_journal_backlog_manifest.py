"""Tests for work_buddy.journal_backlog.manifest — per-thread tag/summary build.

The manifest builder walks line-range thread dicts and uses LLMRunner
to produce, per thread, a ``{id, tags, summary}`` entry. The cluster
step downstream consumes these entries.

Failure handling: a per-thread error doesn't abort the whole run. The
failed entry has ``tags=[]``, ``summary=""``, and an ``error`` field;
other threads in the batch still get processed.
"""

from __future__ import annotations

from typing import Any

from work_buddy.llm import ErrorKind, LLMResponse, LLMRunner, ModelTier


def _thread(tid: str, raw_text: str) -> dict[str, Any]:
    """Minimal thread dict shape (matches build_threads_from_line_ranges)."""
    return {"id": tid, "raw_text": raw_text, "line_count": raw_text.count("\n") + 1,
            "lines": [], "source_dates": [], "has_multi_flag": False}


def test_build_thread_manifest_single_thread(monkeypatch) -> None:
    from work_buddy.journal_backlog.manifest import build_thread_manifest

    def fake_call(self, **kw):
        return LLMResponse(
            content='{"tags": ["tax-prep"], "summary": "Prepare taxes."}',
            structured_output={"tags": ["tax-prep"], "summary": "Prepare taxes."},
        )

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    result = build_thread_manifest([_thread("t_a", "- file taxes")])
    assert len(result) == 1
    entry = result[0]
    assert entry["id"] == "t_a"
    assert entry["tags"] == ["tax-prep"]
    assert entry["summary"] == "Prepare taxes."
    assert entry.get("error") is None


def test_build_thread_manifest_multiple_threads_iterates(monkeypatch) -> None:
    from work_buddy.journal_backlog.manifest import build_thread_manifest

    calls: list[dict] = []

    def fake_call(self, **kw):
        calls.append(kw)
        idx = len(calls) - 1
        return LLMResponse(
            content="",
            structured_output={
                "tags": [f"topic-{idx}"],
                "summary": f"Summary {idx}.",
            },
        )

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    threads = [_thread(f"t_{i}", f"line {i}") for i in range(3)]
    result = build_thread_manifest(threads)

    assert len(calls) == 3
    assert len(result) == 3
    assert result[0]["tags"] == ["topic-0"]
    assert result[2]["summary"] == "Summary 2."


def test_build_thread_manifest_handles_per_thread_error(monkeypatch) -> None:
    from work_buddy.journal_backlog.manifest import build_thread_manifest

    responses = iter([
        LLMResponse(
            content="",
            structured_output={"tags": ["alpha"], "summary": "A"},
        ),
        LLMResponse(
            content="",
            error="upstream timed out",
            error_kind=ErrorKind.TIMEOUT,
        ),
        LLMResponse(
            content="",
            structured_output={"tags": ["gamma"], "summary": "C"},
        ),
    ])

    def fake_call(self, **kw):
        return next(responses)

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    threads = [_thread(f"t_{i}", f"line {i}") for i in range(3)]
    result = build_thread_manifest(threads)

    assert len(result) == 3
    assert result[0]["tags"] == ["alpha"]
    # Errored entry has the contract-defined fallback shape.
    assert result[1]["tags"] == []
    assert result[1]["summary"] == ""
    assert "error" in result[1] and result[1]["error"]
    # Other threads unaffected.
    assert result[2]["tags"] == ["gamma"]


def test_build_thread_manifest_empty_input() -> None:
    from work_buddy.journal_backlog.manifest import build_thread_manifest

    assert build_thread_manifest([]) == []


def test_build_thread_manifest_uses_frontier_fast_by_default(monkeypatch) -> None:
    from work_buddy.journal_backlog.manifest import build_thread_manifest

    captured: dict = {}

    def fake_call(self, **kw):
        captured.update(kw)
        return LLMResponse(
            content="",
            structured_output={"tags": ["x"], "summary": "y"},
        )

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    build_thread_manifest([_thread("t_a", "x")])
    assert captured.get("tier") == ModelTier.FRONTIER_FAST


def test_build_thread_manifest_validation_failure_propagates(monkeypatch) -> None:
    """LLM returns valid response but missing the required `tags` field
    → entry's error explains the missing-field condition."""
    from work_buddy.journal_backlog.manifest import build_thread_manifest

    def fake_call(self, **kw):
        return LLMResponse(
            content="",
            structured_output={"summary": "no tags here"},  # missing tags
        )

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    result = build_thread_manifest([_thread("t_a", "x")])
    assert result[0]["tags"] == []
    assert "tags" in (result[0].get("error") or "").lower()
