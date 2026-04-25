"""Tests for the vendored Claude-Code transcript scanner and aggregator.

Covers:
    * pricing helpers
    * end-to-end scan over a synthetic ``~/.claude/projects/`` tree
    * read-model shape produced by the aggregator
    * dashboard glue exposed via /api/costs?source=claude_code
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.llm.claude_code_usage import pricing, scanner, aggregator


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------


def test_pricing_exact_match():
    assert pricing.get_pricing("claude-sonnet-4-6")["input"] == 3.00
    assert pricing.get_pricing("claude-sonnet-4-6")["output"] == 15.00


def test_pricing_prefix_match():
    p = pricing.get_pricing("claude-sonnet-4-6-extended-context")
    assert p is not None
    assert p["input"] == 3.00


def test_pricing_keyword_fallback():
    assert pricing.get_pricing("anthropic-opus-custom") is not None
    assert pricing.get_pricing("haikuish-clone") is not None


def test_pricing_unknown_returns_none():
    assert pricing.get_pricing("qwen/qwen3-4b") is None
    assert pricing.get_pricing(None) is None
    assert pricing.get_pricing("") is None


def test_calc_cost_uses_cache_rates():
    # 1M input + 1M output + 1M cache_read + 1M cache_write of sonnet-4-6
    cost = pricing.calc_cost(
        "claude-sonnet-4-6",
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000,
    )
    assert pytest.approx(cost, abs=1e-3) == 3.0 + 15.0 + 0.30 + 3.75


def test_calc_cost_returns_zero_for_local_models():
    assert pricing.calc_cost("qwen/qwen3-4b", 100, 100, 0, 0) == 0.0


# ---------------------------------------------------------------------------
# Scanner — synthetic ~/.claude/projects/ tree
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _assistant_record(*, sid: str, ts: str, model: str,
                      message_id: str, input_tokens: int, output_tokens: int,
                      cache_read: int = 0, cache_creation: int = 0,
                      cwd: str = "C:/repo/one", git_branch: str = "main",
                      tool_name: str | None = None) -> dict:
    msg = {
        "id": message_id, "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }
    if tool_name:
        msg["content"] = [{"type": "tool_use", "name": tool_name}]
    return {
        "type": "assistant", "sessionId": sid, "timestamp": ts,
        "cwd": cwd, "gitBranch": git_branch, "message": msg,
    }


@pytest.fixture
def projects_tree(tmp_path):
    """A synthetic projects tree with mixed sessions and dedup edge cases."""
    root = tmp_path / "projects"
    proj = root / "C--repo-one"
    other = root / "C--repo-two"
    _write_jsonl(proj / "abc.jsonl", [
        # Streaming dedup: two records share message_id; the LAST wins.
        _assistant_record(sid="s1", ts="2026-04-20T08:00:00Z",
                          model="claude-sonnet-4-6", message_id="m1",
                          input_tokens=100, output_tokens=10),
        _assistant_record(sid="s1", ts="2026-04-20T08:00:01Z",
                          model="claude-sonnet-4-6", message_id="m1",
                          input_tokens=500, output_tokens=200),
        # A second turn in the same session.
        _assistant_record(sid="s1", ts="2026-04-20T08:05:00Z",
                          model="claude-sonnet-4-6", message_id="m2",
                          input_tokens=200, output_tokens=80,
                          cache_read=50000, tool_name="Read"),
        # User record — must be ignored.
        {"type": "user", "sessionId": "s1",
         "timestamp": "2026-04-20T08:00:30Z"},
    ])
    _write_jsonl(other / "def.jsonl", [
        _assistant_record(sid="s2", ts="2026-04-21T09:00:00Z",
                          model="claude-haiku-4-5",
                          message_id="m3",
                          input_tokens=10, output_tokens=5,
                          cwd="C:/repo/two", tool_name="Bash"),
    ])
    return root


def test_scan_initial_run_counts_files_and_turns(tmp_path, projects_tree):
    db = tmp_path / "tx.db"
    result = scanner.scan(projects_dirs=[projects_tree], db_path=db)
    assert result["new"] == 2
    assert result["sessions"] == 2
    # Two unique message_ids in proj/abc.jsonl, one in other/def.jsonl.
    assert result["turns"] == 3


def test_scan_dedups_streaming_message_ids(tmp_path, projects_tree):
    db = tmp_path / "tx.db"
    scanner.scan(projects_dirs=[projects_tree], db_path=db)
    summary = aggregator.get_claude_code_usage_summary(db_path=db)
    s1 = next(s for s in summary["sessions"] if s["session_id"] == "s1")
    # Last record per message_id wins → 500+200 (not 100+10) for m1
    # plus 200+80 for m2.
    assert s1["input_tokens"] == 500 + 200
    assert s1["output_tokens"] == 200 + 80


def test_scan_incremental_skip_unchanged(tmp_path, projects_tree):
    db = tmp_path / "tx.db"
    scanner.scan(projects_dirs=[projects_tree], db_path=db)
    second = scanner.scan(projects_dirs=[projects_tree], db_path=db)
    # mtime unchanged → both files skipped.
    assert second["new"] == 0
    assert second["updated"] == 0
    assert second["skipped"] == 2


def test_full_rebuild_drops_existing_db(tmp_path, projects_tree):
    db = tmp_path / "tx.db"
    scanner.scan(projects_dirs=[projects_tree], db_path=db)
    assert db.exists()
    second = scanner.scan(projects_dirs=[projects_tree], db_path=db,
                           full_rebuild=True)
    # After full_rebuild, all files are NEW again (none in processed_files).
    assert second["new"] == 2


def test_aggregator_unavailable_when_no_db(tmp_path):
    s = aggregator.get_claude_code_usage_summary(db_path=tmp_path / "nope.db")
    assert s["available"] is False
    assert s["source"] == "claude_code"


def test_aggregator_shape_after_scan(tmp_path, projects_tree):
    db = tmp_path / "tx.db"
    scanner.scan(projects_dirs=[projects_tree], db_path=db)
    s = aggregator.get_claude_code_usage_summary(db_path=db)
    assert s["available"] is True
    assert s["session_count"] == 2
    # Top-level read model fields the dashboard relies on.
    for key in ("totals", "by_day", "by_model", "by_tool", "by_project",
                "sessions", "all_models"):
        assert key in s, f"missing top-level key {key!r}"
    # Sonnet entry should carry the cache_read tokens we logged.
    sonnet = next(r for r in s["by_model"] if r["model"] == "claude-sonnet-4-6")
    assert sonnet["cache_read_tokens"] == 50000
    # Tools captured.
    tools = {r["tool"] for r in s["by_tool"]}
    assert "Read" in tools
    assert "Bash" in tools


def test_aggregator_cost_estimates_use_richer_pricing(tmp_path, projects_tree):
    db = tmp_path / "tx.db"
    scanner.scan(projects_dirs=[projects_tree], db_path=db)
    s = aggregator.get_claude_code_usage_summary(db_path=db)
    sonnet = next(r for r in s["by_model"] if r["model"] == "claude-sonnet-4-6")
    # 700 input + 280 output + 50000 cache_read for sonnet:
    expected = (700 * 3.0 + 280 * 15.0 + 50000 * 0.30) / 1_000_000
    assert pytest.approx(sonnet["cost_usd"], abs=1e-6) == round(expected, 6)


# ---------------------------------------------------------------------------
# Dashboard glue
# ---------------------------------------------------------------------------


def test_api_costs_claude_code_unavailable(monkeypatch, tmp_path):
    """No cache yet → /api/costs?source=claude_code returns ``available: false``."""
    monkeypatch.setattr(scanner, "get_db_path",
                         lambda: tmp_path / "missing.db")
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/api/costs?source=claude_code")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available"] is False
    assert body["source"] == "claude_code"


def test_api_costs_all_includes_both_sources(monkeypatch, tmp_path,
                                              projects_tree):
    db = tmp_path / "tx.db"
    scanner.scan(projects_dirs=[projects_tree], db_path=db)
    monkeypatch.setattr(scanner, "get_db_path", lambda: db)
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/api/costs?source=all")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "all"
    assert "internal" in body
    assert "claude_code" in body
    assert body["claude_code"]["session_count"] == 2


def test_api_costs_rescan_route(monkeypatch, tmp_path, projects_tree):
    db = tmp_path / "tx.db"
    monkeypatch.setattr(scanner, "get_db_path", lambda: db)

    def _fake_rescan(*, full_rebuild: bool = False):
        return scanner.scan(
            projects_dirs=[projects_tree], db_path=db,
            full_rebuild=full_rebuild,
        )
    monkeypatch.setattr(
        "work_buddy.dashboard.costs_claude_code_usage.rescan_claude_code_usage",
        _fake_rescan,
    )
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.post("/api/costs/rescan")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["new"] == 2
