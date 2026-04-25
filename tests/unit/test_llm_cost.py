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
# _estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_no_cache_matches_legacy_arithmetic():
    """With cache=0, cost equals input*input_rate + output*output_rate."""
    # Sonnet rates: $3/$15 per million in the legacy compact table.
    cost = cost_mod._estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert pytest.approx(cost, abs=1e-6) == 3.0 + 15.0


def test_estimate_cost_applies_cache_read_discount():
    # Cache reads at 10% of input rate (90% off).
    cost = cost_mod._estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    # 1M tokens × $3.00 × 0.10 = $0.30
    assert pytest.approx(cost, abs=1e-6) == 0.30


def test_estimate_cost_applies_cache_creation_premium():
    # Cache writes at 125% of input rate (+25% premium).
    cost = cost_mod._estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=1_000_000,
    )
    # 1M tokens × $3.00 × 1.25 = $3.75
    assert pytest.approx(cost, abs=1e-6) == 3.75


def test_estimate_cost_unknown_model_uses_fallback():
    # Fallback rates: $1 input / $5 output per million.
    cost = cost_mod._estimate_cost(
        "totally-made-up-model",
        input_tokens=1_000_000, output_tokens=1_000_000,
    )
    assert pytest.approx(cost, abs=1e-6) == 1.0 + 5.0


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
