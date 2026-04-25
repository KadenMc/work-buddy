"""Tests for the priced_with stamp migration."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.migrate_priced_with_v2 import find_cost_logs, stamp_one_file


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and line.strip().startswith("{")]


def test_stamp_marks_unstamped_rows(tmp_path):
    log = tmp_path / "llm_costs.jsonl"
    _write_jsonl(log, [
        {"model": "claude-sonnet-4-6", "input_tokens": 1, "output_tokens": 1},
        {"model": "claude-haiku-4-5", "input_tokens": 1, "output_tokens": 1,
         "priced_with": "v2"},  # already stamped
    ])
    scanned, modified = stamp_one_file(log, apply=True)
    assert scanned == 2
    assert modified == 1
    rows = _read_jsonl(log)
    assert all(r["priced_with"] == "v2" for r in rows)


def test_stamp_idempotent(tmp_path):
    log = tmp_path / "llm_costs.jsonl"
    _write_jsonl(log, [{"model": "claude-sonnet-4-6"}])
    stamp_one_file(log, apply=True)
    scanned, modified = stamp_one_file(log, apply=True)
    assert modified == 0


def test_stamp_dry_run_does_not_write(tmp_path):
    log = tmp_path / "llm_costs.jsonl"
    _write_jsonl(log, [{"model": "claude-sonnet-4-6"}])
    original = log.read_bytes()
    scanned, modified = stamp_one_file(log, apply=False)
    assert scanned == 1
    assert modified == 1
    assert log.read_bytes() == original


def test_stamp_preserves_malformed_lines(tmp_path):
    log = tmp_path / "llm_costs.jsonl"
    log.write_text(
        '{"model": "claude-sonnet-4-6"}\n'
        '{not json}\n'
        '{"model": "claude-haiku-4-5"}\n',
        encoding="utf-8",
    )
    scanned, modified = stamp_one_file(log, apply=True)
    assert modified == 2
    text = log.read_text(encoding="utf-8")
    assert "{not json}" in text


def test_stamp_does_not_change_cost_field(tmp_path):
    """The migration must not touch any field except priced_with."""
    log = tmp_path / "llm_costs.jsonl"
    _write_jsonl(log, [{
        "model": "claude-sonnet-4-6",
        "input_tokens": 100, "output_tokens": 50,
        "estimated_cost_usd": 0.0011,
        "cached": False, "execution_mode": "cloud",
    }])
    stamp_one_file(log, apply=True)
    row = _read_jsonl(log)[0]
    assert row["estimated_cost_usd"] == 0.0011
    assert row["priced_with"] == "v2"


def test_find_cost_logs(tmp_path):
    root = tmp_path / "agents"
    (root / "session-a").mkdir(parents=True)
    (root / "session-a" / "llm_costs.jsonl").write_text("", encoding="utf-8")
    (root / "session-b").mkdir(parents=True)
    found = find_cost_logs(root)
    assert len(found) == 1
