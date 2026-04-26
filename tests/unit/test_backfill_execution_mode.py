"""Tests for the one-shot ``execution_mode`` backfill migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.backfill_execution_mode import (
    backfill_one_file,
    find_cost_logs,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_backfill_stamps_missing_execution_mode(tmp_path):
    log = tmp_path / "llm_costs.jsonl"
    _write_jsonl(log, [
        {"model": "claude-sonnet-4-6", "input_tokens": 100,
         "output_tokens": 50, "estimated_cost_usd": 0.001},   # missing
        {"model": "claude-haiku-4-5", "execution_mode": "cloud",
         "input_tokens": 10, "output_tokens": 5,
         "estimated_cost_usd": 0.0001},                       # already set
    ])
    scanned, modified = backfill_one_file(log, apply=True)
    assert scanned == 2
    assert modified == 1

    rows = _read_jsonl(log)
    assert all(r["execution_mode"] == "cloud" for r in rows)


def test_backfill_dry_run_does_not_write(tmp_path):
    log = tmp_path / "llm_costs.jsonl"
    _write_jsonl(log, [
        {"model": "claude-sonnet-4-6", "input_tokens": 1, "output_tokens": 1},
    ])
    original = log.read_bytes()
    scanned, modified = backfill_one_file(log, apply=False)
    assert scanned == 1
    assert modified == 1
    assert log.read_bytes() == original


def test_backfill_idempotent(tmp_path):
    log = tmp_path / "llm_costs.jsonl"
    _write_jsonl(log, [
        {"model": "claude-sonnet-4-6", "input_tokens": 1, "output_tokens": 1},
    ])
    backfill_one_file(log, apply=True)
    scanned, modified = backfill_one_file(log, apply=True)
    assert modified == 0


def test_backfill_preserves_malformed_lines(tmp_path):
    """A garbage line in the middle must not be dropped or rewritten."""
    log = tmp_path / "llm_costs.jsonl"
    log.write_text(
        '{"model": "claude-sonnet-4-6", "input_tokens": 1, "output_tokens": 1}\n'
        '{not json\n'
        '{"model": "claude-haiku-4-5", "input_tokens": 2, "output_tokens": 2}\n',
        encoding="utf-8",
    )
    scanned, modified = backfill_one_file(log, apply=True)
    assert modified == 2
    text = log.read_text(encoding="utf-8")
    assert "{not json" in text  # garbage line preserved
    rows_with_mode = [json.loads(line) for line in text.splitlines()
                       if line.strip() and line.strip().startswith("{") and "model" in line]
    assert all(r.get("execution_mode") == "cloud" for r in rows_with_mode)


def test_backfill_handles_missing_file(tmp_path):
    scanned, modified = backfill_one_file(tmp_path / "nope.jsonl", apply=True)
    assert (scanned, modified) == (0, 0)


def test_find_cost_logs(tmp_path):
    root = tmp_path / "agents"
    (root / "session-a").mkdir(parents=True)
    (root / "session-a" / "llm_costs.jsonl").write_text("", encoding="utf-8")
    (root / "session-b").mkdir(parents=True)
    # session-b has no log
    (root / "session-c").mkdir(parents=True)
    (root / "session-c" / "llm_costs.jsonl").write_text("", encoding="utf-8")
    found = find_cost_logs(root)
    names = [p.parent.name for p in found]
    assert sorted(names) == ["session-a", "session-c"]
