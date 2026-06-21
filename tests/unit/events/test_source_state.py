"""Unit tests for per-source cursor state."""

from __future__ import annotations

from work_buddy.events.sources.state import load_state, save_state


def test_missing_state_is_empty(tmp_path):
    assert load_state("nope", tmp_path) == {}


def test_state_roundtrip(tmp_path):
    save_state("s", {"last_hash": "abc", "last_value": 1, "last_polled": "t"}, tmp_path)
    got = load_state("s", tmp_path)
    assert got["last_hash"] == "abc"
    assert got["last_value"] == 1


def test_corrupt_state_degrades_to_empty(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert load_state("bad", tmp_path) == {}
