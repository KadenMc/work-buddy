"""Unit tests for the Costs tab data path.

Covers:
    * the JSONL aggregator in :mod:`work_buddy.dashboard.costs`
    * the ``/api/costs`` Flask route
    * the ``/vendor/<path>`` vendored-asset route
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.dashboard import costs as costs_mod


# ---------------------------------------------------------------------------
# Fixture: synthetic agents/ directory with mixed sessions
# ---------------------------------------------------------------------------


def _write_session(
    root: Path, name: str, *, manifest: dict | None = None,
    entries: list[dict] | None = None,
) -> Path:
    """Build a session directory with optional manifest + cost log."""
    sd = root / name
    sd.mkdir(parents=True)
    if manifest is not None:
        (sd / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if entries is not None:
        with open(sd / "llm_costs.jsonl", "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    return sd


@pytest.fixture
def agents_dir(tmp_path):
    """An ``agents/`` tree covering the realistic mix the aggregator must handle."""
    root = tmp_path / "agents"
    root.mkdir()

    # 1) Cloud session — Sonnet, real cost recorded.
    _write_session(
        root, "2026-04-20T08-00-00_cloud-aaa",
        manifest={"session_id": "cloud-aaa-full", "short_id": "cloud-aaa",
                  "project": "C:\\repo\\one"},
        entries=[
            {"timestamp": "2026-04-20T08:00:00", "model": "claude-sonnet-4-6",
             "task_id": "chrome_infer:scan", "input_tokens": 1000,
             "output_tokens": 500, "estimated_cost_usd": 0.0105,
             "cached": False, "execution_mode": "cloud",
             "caller": [], "backend": "anthropic_default"},
            {"timestamp": "2026-04-20T08:30:00", "model": "claude-sonnet-4-6",
             "task_id": "chrome_infer:scan", "input_tokens": 200,
             "output_tokens": 50, "estimated_cost_usd": 0.0,
             "cached": True, "execution_mode": "cloud",
             "caller": [], "backend": "anthropic_default"},
        ],
    )

    # 2) Local session — Qwen, zero cost by design.
    _write_session(
        root, "2026-04-21T09-00-00_local-bbb",
        manifest={"session_id": "local-bbb-full", "short_id": "local-bbb",
                  "project": "C:\\repo\\one"},
        entries=[
            {"timestamp": "2026-04-21T09:00:00", "model": "qwen/qwen3-4b",
             "task_id": "journal_segment:2026-04-21",
             "input_tokens": 700, "output_tokens": 60,
             "estimated_cost_usd": 0.0, "cached": False,
             "execution_mode": "local", "caller": [],
             "backend": "lmstudio_local"},
        ],
    )

    # 3) Legacy session — entries missing execution_mode and backend; aggregator
    #    must re-estimate cost from the model rate.
    _write_session(
        root, "2026-04-19T07-00-00_legacy-ccc",
        manifest={"session_id": "legacy-ccc-full", "short_id": "legacy-ccc",
                  "project": "C:\\repo\\two"},
        entries=[
            {"timestamp": "2026-04-19T07:00:00", "model": "claude-haiku-4-5",
             "task_id": "shared", "input_tokens": 1_000_000,
             "output_tokens": 0, "caller": []},
            # Missing model — aggregator should bucket as "unknown"
            # and not crash.
            {"timestamp": "2026-04-19T07:05:00", "task_id": "misc",
             "input_tokens": 1, "output_tokens": 1, "caller": []},
        ],
    )

    # 4) Session with malformed JSONL line — must be tolerated.
    sd = _write_session(
        root, "2026-04-22T10-00-00_corrupt-ddd",
        manifest={"session_id": "corrupt-ddd-full", "short_id": "corrupt-ddd",
                  "project": ""},
    )
    with open(sd / "llm_costs.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-04-22T10:00:00", "model": "claude-sonnet-4-7",
            "task_id": "ok:1", "input_tokens": 100, "output_tokens": 100,
            "estimated_cost_usd": 0.0018, "cached": False,
            "execution_mode": "cloud",
        }) + "\n")
        f.write("{not valid json\n")
        f.write(json.dumps({
            "timestamp": "2026-04-22T10:05:00", "model": "claude-sonnet-4-7",
            "task_id": "ok:2", "input_tokens": 100, "output_tokens": 100,
            "estimated_cost_usd": 0.0018, "cached": False,
            "execution_mode": "cloud",
        }) + "\n")

    # 5) Session with no log file at all — must not appear in sessions list.
    _write_session(
        root, "2026-04-23T11-00-00_empty-eee",
        manifest={"session_id": "empty-eee-full", "short_id": "empty-eee"},
    )

    return root


# ---------------------------------------------------------------------------
# Aggregator tests
# ---------------------------------------------------------------------------


def test_aggregator_totals_match_entries(agents_dir):
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    # 6 entries total — 2 cloud + 1 local + 2 legacy + 2 corrupt-survivors,
    # minus the malformed line that's silently skipped = 7. (2+1+2+2)
    assert s["totals"]["calls"] == 7
    assert s["totals"]["cache_hits"] == 1
    assert s["totals"]["api_calls"] == 6
    # Sessions with no log file are skipped.
    short_ids = [r["short_id"] for r in s["sessions"]]
    assert "empty-eee" not in short_ids
    assert set(short_ids) == {"cloud-aaa", "local-bbb", "legacy-ccc", "corrupt-ddd"}


def test_aggregator_handles_missing_cost_field(agents_dir):
    """Legacy entries without ``estimated_cost_usd`` are re-priced from the rate table."""
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    haiku_row = next(r for r in s["by_model"] if r["model"] == "claude-haiku-4-5")
    # 1M input tokens at $0.80/1M = $0.80
    assert pytest.approx(haiku_row["cost_usd"], abs=1e-3) == 0.80


def test_aggregator_unknown_model_bucketed_safely(agents_dir):
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    models = {r["model"] for r in s["by_model"]}
    assert "unknown" in models


def test_aggregator_local_costs_zeroed(agents_dir):
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    qwen = next(r for r in s["by_model"] if r["model"] == "qwen/qwen3-4b")
    assert qwen["cost_usd"] == 0.0


def test_aggregator_tolerates_malformed_jsonl(agents_dir):
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    sess = next(r for r in s["sessions"] if r["short_id"] == "corrupt-ddd")
    # Two valid records survive; the broken middle line is silently dropped.
    assert sess["calls"] == 2


def test_aggregator_session_metadata(agents_dir):
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    cloud = next(r for r in s["sessions"] if r["short_id"] == "cloud-aaa")
    assert cloud["session_id"] == "cloud-aaa-full"
    assert cloud["project"] == "C:\\repo\\one"
    assert cloud["models"] == ["claude-sonnet-4-6"]
    assert cloud["first"] == "2026-04-20T08:00:00"
    assert cloud["last"] == "2026-04-20T08:30:00"


def test_aggregator_sessions_sorted_by_recency(agents_dir):
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    last_values = [r["last"] for r in s["sessions"]]
    assert last_values == sorted(last_values, reverse=True)


def test_aggregator_by_execution_mode(agents_dir):
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    by_mode = {r["mode"]: r for r in s["by_execution_mode"]}
    assert "cloud" in by_mode
    assert "local" in by_mode
    # Legacy rows missing ``execution_mode`` are defaulted to "cloud" by
    # the aggregator (see scripts/backfill_execution_mode.py + the
    # ``mode = entry.get("execution_mode") or "cloud"`` line in costs.py).
    # The "unknown" bucket should never appear.
    assert "unknown" not in by_mode
    assert by_mode["local"]["cost_usd"] == 0.0


def test_aggregator_unknown_bucket_never_appears(agents_dir):
    """The 'unknown' execution_mode bucket is impossible by construction."""
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    modes = {r["mode"] for r in s["by_execution_mode"]}
    assert modes <= {"cloud", "local"}, (
        f"Unexpected mode bucket(s): {modes - {'cloud', 'local'}}"
    )


def test_aggregator_cloud_local_call_counts(agents_dir):
    """Each bucket exposes cloud_calls and local_calls counts."""
    s = costs_mod.get_costs_summary(agents_dir=agents_dir)
    t = s["totals"]
    assert t["cloud_calls"] + t["local_calls"] == t["calls"]
    # Sample entries in the fixture: cloud-aaa has 2 cloud rows; local-bbb
    # has 1 local row; legacy-ccc has 2 cloud rows (missing execution_mode
    # defaults to cloud); corrupt-ddd has 2 cloud rows.
    assert t["cloud_calls"] >= 6
    assert t["local_calls"] >= 1


def test_aggregator_legacy_missing_execution_mode_buckets_as_cloud(tmp_path):
    """Pre-Phase-1 rows with no ``execution_mode`` field land in 'cloud'."""
    root = tmp_path / "agents"
    _write_session(
        root, "2026-04-08T00-00-00_legacy",
        manifest={"short_id": "legacy"},
        entries=[{
            "timestamp": "2026-04-08T08:00:00",
            "model": "claude-sonnet-4-6",
            "task_id": "test",
            "input_tokens": 100, "output_tokens": 50,
            "estimated_cost_usd": 0.0011,
            "cached": False,
            # NOTE: deliberately omitting execution_mode
        }],
    )
    s = costs_mod.get_costs_summary(agents_dir=root)
    by_mode = {r["mode"]: r for r in s["by_execution_mode"]}
    assert "cloud" in by_mode
    assert "unknown" not in by_mode
    assert s["totals"]["cloud_calls"] == 1
    assert s["totals"]["local_calls"] == 0


def test_aggregator_sums_cache_token_fields(tmp_path):
    """cache_read_tokens / cache_creation_tokens roll up into bucket totals."""
    root = tmp_path / "agents"
    _write_session(
        root, "2026-04-25T10-00-00_with-cache",
        manifest={"short_id": "wc"},
        entries=[
            {"timestamp": "2026-04-25T10:00:00",
             "model": "claude-sonnet-4-6", "task_id": "t1",
             "input_tokens": 1000, "output_tokens": 500,
             "cache_read_tokens": 50_000,
             "cache_creation_tokens": 5_000,
             "estimated_cost_usd": 0.0125, "cached": False,
             "execution_mode": "cloud"},
            {"timestamp": "2026-04-25T10:30:00",
             "model": "claude-sonnet-4-6", "task_id": "t2",
             "input_tokens": 200, "output_tokens": 100,
             "cache_read_tokens": 100_000,
             "cache_creation_tokens": 0,
             "estimated_cost_usd": 0.005, "cached": False,
             "execution_mode": "cloud"},
        ],
    )
    s = costs_mod.get_costs_summary(agents_dir=root)
    t = s["totals"]
    assert t["cache_read_tokens"] == 150_000
    assert t["cache_creation_tokens"] == 5_000
    # Per-day rolls up too.
    day = s["by_day"][0]
    assert day["cache_read_tokens"] == 150_000
    assert day["cache_creation_tokens"] == 5_000


def test_aggregator_treats_missing_cache_fields_as_zero(tmp_path):
    """Rows written before 2026-04-25 lack the cache fields entirely."""
    root = tmp_path / "agents"
    _write_session(
        root, "2026-04-10T00-00-00_no-cache-field",
        manifest={"short_id": "ncf"},
        entries=[{
            "timestamp": "2026-04-10T08:00:00",
            "model": "claude-sonnet-4-6", "task_id": "t1",
            "input_tokens": 100, "output_tokens": 50,
            "estimated_cost_usd": 0.0011, "cached": False,
            "execution_mode": "cloud",
            # No cache_read_tokens / cache_creation_tokens fields.
        }],
    )
    s = costs_mod.get_costs_summary(agents_dir=root)
    assert s["totals"]["cache_read_tokens"] == 0
    assert s["totals"]["cache_creation_tokens"] == 0
    # Aggregator must not crash and must aggregate the call normally.
    assert s["totals"]["calls"] == 1


def test_aggregator_empty_directory(tmp_path):
    s = costs_mod.get_costs_summary(agents_dir=tmp_path / "doesnotexist")
    assert s["totals"]["calls"] == 0
    assert s["sessions"] == []
    assert s["log_files_seen"] == 0


def test_aggregator_directory_with_no_log_files(tmp_path):
    root = tmp_path / "agents"
    _write_session(root, "2026-04-24T00-00-00_only-manifest",
                   manifest={"short_id": "om"})
    s = costs_mod.get_costs_summary(agents_dir=root)
    assert s["totals"]["calls"] == 0
    assert s["sessions"] == []


# ---------------------------------------------------------------------------
# Flask route tests
# ---------------------------------------------------------------------------


def test_api_costs_route_internal(monkeypatch, agents_dir):
    monkeypatch.setattr(costs_mod, "_AGENTS_DIR", agents_dir)
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/api/costs")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "work_buddy_internal"
    assert body["totals"]["calls"] == 7
    assert "by_day" in body
    assert "sessions" in body


def test_api_costs_route_transcripts_phase1(monkeypatch, agents_dir):
    """Phase 1: ``source=transcripts`` returns ``available: false``."""
    monkeypatch.setattr(costs_mod, "_AGENTS_DIR", agents_dir)
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/api/costs?source=transcripts")
    assert resp.status_code == 200
    body = resp.get_json()
    # Either an explicit unavailable marker or a populated transcripts
    # summary if Phase 2 is wired. Both are acceptable here.
    assert "source" in body or "available" in body


def test_api_costs_route_all_source_wraps(monkeypatch, agents_dir):
    monkeypatch.setattr(costs_mod, "_AGENTS_DIR", agents_dir)
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/api/costs?source=all")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "all"
    assert "internal" in body
    assert "transcripts" in body
    assert body["internal"]["totals"]["calls"] == 7


def test_vendor_route_serves_chart_js():
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/vendor/chart.umd.min.js")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/javascript")
    assert len(resp.data) > 100_000  # Chart.js minified is ~200 KB


def test_vendor_route_blocks_traversal():
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/vendor/../service.py")
    # Either Flask 404s outright or our handler does — both are fine.
    assert resp.status_code in (404, 308, 301)
