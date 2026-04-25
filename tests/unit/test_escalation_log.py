"""Tests for the LLM-escalation observability log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.llm import escalation_log as elog
from work_buddy.llm.response import ErrorKind, TierAttempt


@pytest.fixture
def isolated_log(monkeypatch, tmp_path):
    """Redirect the log to a temp path so tests don't pollute real data/."""
    log_file = tmp_path / "escalations.log"
    monkeypatch.setattr(elog, "_log_path", lambda: log_file)
    return log_file


def _make_attempt(tier: str, outcome: str = "success",
                  error_kind: ErrorKind | None = None,
                  elapsed_ms: int = 100,
                  input_tokens: int = 500,
                  output_tokens: int = 200) -> TierAttempt:
    return TierAttempt(
        tier=tier,
        model=f"model-{tier}",
        error_kind=error_kind,
        error=str(error_kind.value) if error_kind else None,
        elapsed_ms=elapsed_ms,
        outcome=outcome,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def test_log_one_success_record(isolated_log):
    elog.log_escalation(
        source="llm_runner",
        attempts=[_make_attempt("frontier_fast")],
        final_outcome="success",
        trace_id="t1",
    )
    records = elog.read_escalations()
    assert len(records) == 1
    rec = records[0]
    assert rec["source"] == "llm_runner"
    assert rec["trace_id"] == "t1"
    assert rec["final_outcome"] == "success"
    assert rec["final_tier"] == "frontier_fast"
    assert rec["attempts"][0]["model"] == "model-frontier_fast"


def test_log_escalation_chain_preserves_order(isolated_log):
    elog.log_escalation(
        source="llm_runner",
        attempts=[
            _make_attempt("local_fast", outcome="empty_content",
                          error_kind=ErrorKind.EMPTY_CONTENT),
            _make_attempt("frontier_fast"),
        ],
        final_outcome="success",
    )
    rec = elog.read_escalations()[0]
    assert [a["tier"] for a in rec["attempts"]] == ["local_fast", "frontier_fast"]
    assert rec["attempts"][0]["error_kind"] == "empty_content"
    assert rec["final_tier"] == "frontier_fast"


def test_read_escalations_filters_by_outcome(isolated_log):
    elog.log_escalation(source="llm_runner",
                         attempts=[_make_attempt("a")], final_outcome="success")
    elog.log_escalation(source="llm_runner",
                         attempts=[_make_attempt("a", outcome="backend_error",
                                                 error_kind=ErrorKind.TIMEOUT)],
                         final_outcome="backend_error")
    succ = elog.read_escalations(final_outcome="success")
    fail = elog.read_escalations(final_outcome="backend_error")
    assert len(succ) == 1 and succ[0]["final_outcome"] == "success"
    assert len(fail) == 1 and fail[0]["final_outcome"] == "backend_error"


def test_read_escalations_filters_by_trace_and_source(isolated_log):
    elog.log_escalation(source="llm_runner", attempts=[_make_attempt("a")],
                         final_outcome="success", trace_id="trace-A")
    elog.log_escalation(source="journal_segmenter", attempts=[_make_attempt("b")],
                         final_outcome="success", trace_id="trace-B")
    by_trace = elog.read_escalations(trace_id="trace-B")
    by_source = elog.read_escalations(source="journal_segmenter")
    assert len(by_trace) == 1 and by_trace[0]["trace_id"] == "trace-B"
    assert len(by_source) == 1 and by_source[0]["source"] == "journal_segmenter"


def test_read_escalations_newest_first(isolated_log):
    for i in range(5):
        elog.log_escalation(source="llm_runner",
                             attempts=[_make_attempt(f"t{i}")],
                             final_outcome="success", trace_id=f"id-{i}")
    recs = elog.read_escalations(limit=3)
    assert [r["trace_id"] for r in recs] == ["id-4", "id-3", "id-2"]


def test_read_escalations_tolerates_corrupt_lines(isolated_log):
    # First a valid record, then garbage, then another valid record.
    elog.log_escalation(source="llm_runner",
                         attempts=[_make_attempt("a")], final_outcome="success",
                         trace_id="t1")
    isolated_log.parent.mkdir(parents=True, exist_ok=True)
    with open(isolated_log, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    elog.log_escalation(source="llm_runner",
                         attempts=[_make_attempt("b")], final_outcome="success",
                         trace_id="t2")
    recs = elog.read_escalations()
    assert len(recs) == 2
    assert {r["trace_id"] for r in recs} == {"t1", "t2"}


def test_log_skips_when_attempts_empty(isolated_log):
    elog.log_escalation(source="llm_runner", attempts=[],
                         final_outcome="success")
    assert elog.read_escalations() == []
    # No file created either.
    assert not isolated_log.exists()


def test_log_accepts_dict_attempts(isolated_log):
    """Adapter-level callers pass dicts (with extra fields), not TierAttempts."""
    elog.log_escalation(
        source="journal_segmenter",
        attempts=[
            {"tier": "local_fast", "outcome": "validation_failed",
             "categories": ["missing_coverage"], "elapsed_ms": 2400},
            {"tier": "frontier_fast", "outcome": "success",
             "elapsed_ms": 1900},
        ],
        final_outcome="success",
        trace_id="seg-1",
        metadata={"journal_date": "2026-04-25"},
    )
    rec = elog.read_escalations()[0]
    assert rec["metadata"] == {"journal_date": "2026-04-25"}
    assert rec["attempts"][0]["categories"] == ["missing_coverage"]


def test_summarize_counts(isolated_log):
    elog.log_escalation(source="llm_runner",
                         attempts=[_make_attempt("a")], final_outcome="success")
    elog.log_escalation(source="llm_runner",
                         attempts=[_make_attempt("a"),
                                   _make_attempt("b")],
                         final_outcome="success")
    elog.log_escalation(source="journal_segmenter",
                         attempts=[_make_attempt("a", outcome="validation_failed")],
                         final_outcome="exhausted")
    s = elog.summarize_escalations()
    assert s["total"] == 3
    assert s["escalated_past_first"] == 1  # only the 2-attempt record
    assert s["by_source"]["llm_runner"] == 2
    assert s["by_source"]["journal_segmenter"] == 1
    assert s["by_outcome"]["success"] == 2
    assert s["by_outcome"]["exhausted"] == 1


def test_error_kind_enum_serialized_to_string(isolated_log):
    elog.log_escalation(
        source="llm_runner",
        attempts=[_make_attempt("a", outcome="backend_error",
                                error_kind=ErrorKind.RATE_LIMITED)],
        final_outcome="backend_error",
    )
    raw = isolated_log.read_text(encoding="utf-8").strip()
    rec = json.loads(raw)
    assert rec["attempts"][0]["error_kind"] == "rate_limited"


# ---------------------------------------------------------------------------
# Capability wiring
# ---------------------------------------------------------------------------


def test_capability_returns_summary(isolated_log):
    """``escalation_recent(summary=True)`` returns aggregate counts."""
    from work_buddy.mcp_server.registry import _escalation_recent
    elog.log_escalation(source="llm_runner",
                         attempts=[_make_attempt("a")], final_outcome="success")
    out = _escalation_recent(summary=True)
    assert "summary" in out
    assert out["summary"]["total"] == 1


def test_capability_returns_records(isolated_log):
    from work_buddy.mcp_server.registry import _escalation_recent
    elog.log_escalation(source="llm_runner",
                         attempts=[_make_attempt("a")], final_outcome="success",
                         trace_id="t1")
    out = _escalation_recent(limit=10)
    assert out["count"] == 1
    assert out["records"][0]["trace_id"] == "t1"
    assert out["applied_filters"]["limit"] == 10
