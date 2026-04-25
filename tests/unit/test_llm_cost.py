"""Tests for the per-call LLM cost log writer (``work_buddy.llm.cost``).

Covers:
    * the JSONL row shape (incl. cache_read / cache_creation token fields)
    * cost estimation including Anthropic prompt-cache rates
    * session_total aggregation surfaces cache token totals
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.llm import cost as cost_mod


@pytest.fixture
def isolated_log(monkeypatch, tmp_path):
    """Redirect the cost log to a temp path."""
    log = tmp_path / "llm_costs.jsonl"
    monkeypatch.setattr(cost_mod, "_cost_log_path", lambda: log)
    return log


def _read_rows(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------------------------------------------------------------------
# log_call schema
# ---------------------------------------------------------------------------


def test_log_call_writes_cache_token_fields(isolated_log):
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        task_id="test",
        cache_read_tokens=50_000,
        cache_creation_tokens=5_000,
    )
    rows = _read_rows(isolated_log)
    assert len(rows) == 1
    row = rows[0]
    assert row["cache_read_tokens"] == 50_000
    assert row["cache_creation_tokens"] == 5_000


def test_log_call_defaults_cache_fields_to_zero(isolated_log):
    """Callers that don't pass cache fields still produce well-formed rows."""
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=10, output_tokens=5,
        task_id="test",
    )
    row = _read_rows(isolated_log)[0]
    assert row["cache_read_tokens"] == 0
    assert row["cache_creation_tokens"] == 0


def test_log_call_local_mode_zeros_cost_even_with_cache_tokens(isolated_log):
    """Local execution_mode forces cost=0 regardless of token counts."""
    cost_mod.log_call(
        model="qwen/qwen3-4b",
        input_tokens=1000, output_tokens=500,
        task_id="test",
        execution_mode="local",
        cache_read_tokens=50_000,  # nonsensical for local but should not affect cost
    )
    row = _read_rows(isolated_log)[0]
    assert row["estimated_cost_usd"] == 0.0


def test_log_call_cached_zeros_cost(isolated_log):
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=1000, output_tokens=500,
        task_id="test",
        cached=True,
        cache_read_tokens=10_000,
    )
    row = _read_rows(isolated_log)[0]
    assert row["estimated_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Cost computation via log_call (now backed by the canonical
# work_buddy.llm.transcripts.pricing.calc_cost)
# ---------------------------------------------------------------------------


def test_log_call_cost_no_cache_matches_canonical_table(isolated_log):
    """Sonnet at $3 input / $15 output per 1M tokens."""
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=1_000_000, output_tokens=1_000_000,
        task_id="t",
    )
    row = _read_rows(isolated_log)[0]
    assert pytest.approx(row["estimated_cost_usd"], abs=1e-6) == 3.0 + 15.0


def test_log_call_cost_applies_cache_read_discount(isolated_log):
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=0, output_tokens=0,
        task_id="t",
        cache_read_tokens=1_000_000,
    )
    row = _read_rows(isolated_log)[0]
    # Sonnet cache_read = $0.30 per 1M (10% of $3 input).
    assert pytest.approx(row["estimated_cost_usd"], abs=1e-6) == 0.30


def test_log_call_cost_applies_cache_creation_premium(isolated_log):
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=0, output_tokens=0,
        task_id="t",
        cache_creation_tokens=1_000_000,
    )
    row = _read_rows(isolated_log)[0]
    # Sonnet cache_creation = $3.75 per 1M (125% of $3 input).
    assert pytest.approx(row["estimated_cost_usd"], abs=1e-6) == 3.75


def test_log_call_unknown_model_returns_zero_cost(isolated_log):
    """Post-consolidation, non-Anthropic models return $0 (no $1/$5 fallback)."""
    cost_mod.log_call(
        model="totally-made-up-model",
        input_tokens=1_000_000, output_tokens=1_000_000,
        task_id="t",
    )
    row = _read_rows(isolated_log)[0]
    assert row["estimated_cost_usd"] == 0.0


def test_log_call_stamps_priced_with_v2(isolated_log):
    """Every new row carries the current pricing-version stamp."""
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=10, output_tokens=5, task_id="t",
    )
    row = _read_rows(isolated_log)[0]
    assert row["priced_with"] == "v2"


# ---------------------------------------------------------------------------
# session_total surfaces the new fields
# ---------------------------------------------------------------------------


def test_session_total_surfaces_cache_token_totals(isolated_log):
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=100, output_tokens=50, task_id="t1",
        cache_read_tokens=10_000, cache_creation_tokens=1_000,
    )
    cost_mod.log_call(
        model="claude-sonnet-4-6",
        input_tokens=200, output_tokens=80, task_id="t2",
        cache_read_tokens=5_000, cache_creation_tokens=0,
    )
    summary = cost_mod.session_total()
    assert summary["total_cache_read_tokens"] == 15_000
    assert summary["total_cache_creation_tokens"] == 1_000
    assert summary["total_calls"] == 2


def test_session_total_empty_log_includes_zero_cache_fields(isolated_log):
    summary = cost_mod.session_total()
    assert summary["total_cache_read_tokens"] == 0
    assert summary["total_cache_creation_tokens"] == 0
