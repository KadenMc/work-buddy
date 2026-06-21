"""Unit tests for the event-source loader."""

from __future__ import annotations

import yaml

from work_buddy.events.sources.loader import load_event_sources

VALID_FM = {
    "kind": "event_source",
    "source": {"type": "http_poll", "url": "https://x/q.json", "interval": "6h"},
    "extract": {"mode": "json_path", "path": "$.ceo"},
    "condition": "event.data.ceo != prev.data.ceo",
    "action": {"name": "notify", "params": {}},
    "allowed_actions": ["notify"],
    "autonomy": "notify_only",
    "enabled": True,
}


def _write_source(directory, name, fm):
    (directory / f"{name}.md").write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\nnotes\n",
        encoding="utf-8",
    )


def test_loads_valid_source(tmp_path):
    _write_source(tmp_path, "nvda-watch", VALID_FM)
    defs, errors = load_event_sources(tmp_path)
    assert errors == []
    assert len(defs) == 1
    assert defs[0].name == "nvda-watch"
    assert defs[0].interval_s == 21600


def test_rejects_malformed_source(tmp_path):
    bad = {**VALID_FM, "source": {"type": "http_poll", "interval": "nope"}}  # no url + bad interval
    _write_source(tmp_path, "bad-one", bad)
    defs, errors = load_event_sources(tmp_path)
    assert defs == []
    assert len(errors) == 1
    assert errors[0]["file"] == "bad-one.md"
    assert any("url" in e for e in errors[0]["errors"])


def test_mixed_valid_and_invalid(tmp_path):
    _write_source(tmp_path, "good", VALID_FM)
    _write_source(tmp_path, "bad", {**VALID_FM, "condition": "a +"})
    defs, errors = load_event_sources(tmp_path)
    assert [d.name for d in defs] == ["good"]
    assert [e["file"] for e in errors] == ["bad.md"]


def test_empty_dir_is_clean(tmp_path):
    assert load_event_sources(tmp_path) == ([], [])
