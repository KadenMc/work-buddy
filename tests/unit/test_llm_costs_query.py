"""Tests for the unified ``llm_costs_query`` capability."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.llm import cost_query


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------


def test_window_named_7d_resolves_to_seven_days():
    info = cost_query._resolve_window("7d")
    assert info["label"] == "7d"
    assert info["days"] == 7


def test_window_today_is_one_day():
    info = cost_query._resolve_window("today")
    assert info["days"] == 1


def test_window_iso_range_inclusive():
    info = cost_query._resolve_window("2026-04-01..2026-04-10")
    assert info["start"] == "2026-04-01"
    assert info["end"] == "2026-04-10"
    assert info["days"] == 10


def test_window_single_day_form():
    info = cost_query._resolve_window("2026-04-15")
    assert info["start"] == "2026-04-15"
    assert info["end"] == "2026-04-15"
    assert info["days"] == 1


def test_window_all_has_no_lower_bound():
    info = cost_query._resolve_window("all")
    assert info["_dt_start"] is None
    assert info["days"] is None


def test_window_invalid_raises():
    with pytest.raises(ValueError):
        cost_query._resolve_window("garbage")


def test_previous_window_inverts_correctly():
    info = cost_query._resolve_window("2026-04-08..2026-04-14")  # 7 days
    prev = cost_query._previous_window(info)
    assert prev is not None
    assert prev["start"] == "2026-04-01"
    assert prev["end"] == "2026-04-07"
    assert prev["days"] == 7


def test_previous_window_returns_none_for_unbounded_all():
    info = cost_query._resolve_window("all")
    assert cost_query._previous_window(info) is None


# ---------------------------------------------------------------------------
# Source-isolation tests via monkeypatch
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_internal(monkeypatch, tmp_path):
    """Plant a synthetic internal log under ``data/agents/`` for the run."""
    root = tmp_path / "agents"
    sd = root / "2026-04-25T10-00-00_test-sess"
    sd.mkdir(parents=True)
    (sd / "manifest.json").write_text(json.dumps({
        "session_id": "test-sess-id", "short_id": "testsess",
        "project": "C:/repo/work-buddy",
    }), encoding="utf-8")
    now_iso = datetime.now(timezone.utc).isoformat()
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    far_iso = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    with open(sd / "llm_costs.jsonl", "w", encoding="utf-8") as f:
        # 2 cloud + 1 local within last 7 days
        for ts in [now_iso, yesterday_iso]:
            f.write(json.dumps({
                "timestamp": ts, "model": "claude-sonnet-4-6",
                "task_id": "tt", "input_tokens": 100, "output_tokens": 50,
                "estimated_cost_usd": 0.005, "cached": False,
                "execution_mode": "cloud",
            }) + "\n")
        f.write(json.dumps({
            "timestamp": now_iso, "model": "qwen/qwen3-4b",
            "task_id": "tt", "input_tokens": 200, "output_tokens": 100,
            "estimated_cost_usd": 0.0, "cached": False,
            "execution_mode": "local",
        }) + "\n")
        # 1 ancient cloud row (outside 7d window)
        f.write(json.dumps({
            "timestamp": far_iso, "model": "claude-sonnet-4-6",
            "task_id": "tt", "input_tokens": 100, "output_tokens": 50,
            "estimated_cost_usd": 0.005, "cached": False,
            "execution_mode": "cloud",
        }) + "\n")

    # Monkey-patch the iterator to read from our tmp tree.
    from work_buddy.dashboard import costs as costs_mod
    monkeypatch.setattr(costs_mod, "_AGENTS_DIR", root)
    return root


@pytest.fixture
def empty_claude_code(monkeypatch, tmp_path):
    """Point the claude_code source at an empty cache."""
    from work_buddy.llm.claude_code_usage import scanner as _scanner
    monkeypatch.setattr(_scanner, "get_db_path", lambda: tmp_path / "missing.db")


def test_query_internal_only_default_window(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    compare_to_previous=False)
    # 3 rows in the 7d window (2 cloud + 1 local), 1 ancient excluded.
    assert r["totals"]["calls"] == 3
    assert r["totals"]["calls_by_source"]["work_buddy_internal_cloud"] == 2
    assert r["totals"]["calls_by_source"]["work_buddy_internal_local"] == 1
    assert r["totals"]["calls_by_source"]["claude_code_transcripts"] == 0


def test_query_excludes_local_when_include_local_false(fake_internal,
                                                         empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    include_local=False,
                                    compare_to_previous=False)
    assert r["totals"]["calls"] == 2
    assert r["totals"]["calls_by_source"]["work_buddy_internal_local"] == 0


def test_query_window_filters_old_rows(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="all", source="internal",
                                    compare_to_previous=False)
    assert r["totals"]["calls"] == 4   # ancient row included


def test_query_groups_by_model(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    group_by="model",
                                    compare_to_previous=False)
    keys = {g["key"] for g in r["groups"]}
    assert "claude-sonnet-4-6" in keys
    assert "qwen/qwen3-4b" in keys


def test_query_top_n_caps_groups(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    group_by="model", top_n=1,
                                    compare_to_previous=False)
    assert len(r["groups"]) == 1


def test_query_groups_sorted_by_cost_desc(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    group_by="model",
                                    compare_to_previous=False)
    costs = [g["cost_usd"] for g in r["groups"]]
    assert costs == sorted(costs, reverse=True)


def test_query_min_cost_filter(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    min_cost=0.001,
                                    compare_to_previous=False)
    # Local row has cost 0; should be excluded.
    assert r["totals"]["calls_by_source"]["work_buddy_internal_local"] == 0
    assert r["totals"]["calls"] == 2  # only the two cloud rows


def test_query_project_filter_substring(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    project="work-buddy",
                                    compare_to_previous=False)
    assert r["totals"]["calls"] == 3   # all 3 rows in this session match


def test_query_project_filter_no_match(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    project="some-other-repo",
                                    compare_to_previous=False)
    assert r["totals"]["calls"] == 0


def test_query_model_filter(fake_internal, empty_claude_code):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    model="claude-sonnet-4-6",
                                    compare_to_previous=False)
    assert r["totals"]["calls"] == 2


def test_query_emits_warning_when_claude_code_cache_missing(
    fake_internal, empty_claude_code,
):
    r = cost_query.llm_costs_query(window="7d", source="claude_code",
                                    compare_to_previous=False)
    assert any("claude_code" in w.lower() for w in r["warnings"])
    assert r["totals"]["calls"] == 0


def test_query_comparison_field_present_when_requested(
    fake_internal, empty_claude_code,
):
    r = cost_query.llm_costs_query(window="7d", source="internal",
                                    compare_to_previous=True)
    assert "comparison" in r
    assert r["comparison"] is not None
    assert "previous_window" in r["comparison"]
    assert "delta_pct_cost" in r["comparison"]


def test_query_comparison_omitted_when_window_unbounded(
    fake_internal, empty_claude_code,
):
    r = cost_query.llm_costs_query(window="all", source="internal",
                                    compare_to_previous=True)
    assert r["comparison"] is None


def test_query_legacy_transcripts_source_alias_maps_to_claude_code(
    fake_internal, empty_claude_code,
):
    r = cost_query.llm_costs_query(window="7d", source="transcripts",
                                    compare_to_previous=False)
    assert r["source"] == "claude_code"


def test_query_invalid_source_raises(fake_internal, empty_claude_code):
    with pytest.raises(ValueError):
        cost_query.llm_costs_query(source="garbage")


def test_query_invalid_group_by_raises(fake_internal, empty_claude_code):
    with pytest.raises(ValueError):
        cost_query.llm_costs_query(group_by="nope")


def test_query_filters_applied_echoed_in_response(
    fake_internal, empty_claude_code,
):
    r = cost_query.llm_costs_query(
        window="7d", group_by="model", source="internal",
        min_cost=0.001, project="work-buddy",
        compare_to_previous=False,
    )
    fa = r["filters_applied"]
    assert fa["window"] == "7d"
    assert fa["group_by"] == "model"
    assert fa["source"] == "internal"
    assert fa["min_cost"] == 0.001
    assert fa["project"] == "work-buddy"


# ---------------------------------------------------------------------------
# Capability dispatch
# ---------------------------------------------------------------------------


def test_capability_dispatches_to_query(fake_internal, empty_claude_code):
    from work_buddy.mcp_server.registry import _llm_costs_query
    r = _llm_costs_query(window="7d", source="internal",
                          compare_to_previous=False)
    assert "totals" in r
    assert r["totals"]["calls"] == 3


def test_capability_registered_in_registry():
    """Sanity: llm_costs_query resolves as a declared llm capability."""
    from work_buddy.knowledge.capability_loader import load_declared_capabilities
    from work_buddy.mcp_server import op_registry
    op_registry.clear_ops()
    op_registry.load_builtin_ops()
    caps, _issues = load_declared_capabilities()
    names = {c.name for c in caps if c.category == "llm"}
    assert "llm_costs_query" in names
    assert "claude_code_usage_summary" not in names  # removed
    assert "claude_code_usage_scan" in names         # kept (mutates state)
