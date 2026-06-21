"""Unit tests for the event-source schema + validator."""

from __future__ import annotations

import pytest

from work_buddy.events.sources.definition import (
    EventSourceDef,
    from_frontmatter,
    parse_debounce,
    parse_interval,
    validate_source_fm,
)

_SEMANTIC = {"question": "Is there material news?", "query": "nvda news", "cooldown": "1h", "debounce": "2/3"}

VALID_FM = {
    "kind": "event_source",
    "source": {"type": "http_poll", "url": "https://x/q.json", "interval": "6h"},
    "cursor": {"from": "now"},
    "extract": {"mode": "json_path", "path": "$.ceo", "id_field": "filingId"},
    "dedup": "unique",
    "condition": "event.data.ceo != prev.data.ceo",
    "action": {"name": "notify", "params": {}},
    "allowed_actions": ["notify"],
    "autonomy": "notify_only",
    "rate_limit": {"max_per_hour": 6},
    "enabled": True,
}


@pytest.mark.parametrize(
    "raw,expected",
    [("30s", 30), ("5m", 300), ("6h", 21600), ("1d", 86400), (" 2h ", 7200)],
)
def test_parse_interval_ok(raw, expected):
    assert parse_interval(raw) == expected


@pytest.mark.parametrize("raw", ["", "nope", "6", "6x", 6, None, "-3h"])
def test_parse_interval_bad(raw):
    assert parse_interval(raw) is None


def test_valid_source_has_no_errors():
    assert validate_source_fm("nvda-watch", VALID_FM) == []


def test_from_frontmatter_fields():
    d = from_frontmatter("nvda-watch", VALID_FM)
    assert isinstance(d, EventSourceDef)
    assert d.name == "nvda-watch"
    assert d.type == "http_poll"
    assert d.interval_s == 21600
    assert d.extract_mode == "json_path"
    assert d.extract_path == "$.ceo"
    assert d.condition == "event.data.ceo != prev.data.ceo"
    assert d.allowed_actions == ("notify",)
    assert d.source_uri == "/wb/source/nvda-watch"
    assert d.event_type == "ai.workbuddy.source.nvda-watch.changed"


def test_missing_url_for_http_poll():
    fm = {**VALID_FM, "source": {"type": "http_poll", "interval": "6h"}}
    errs = validate_source_fm("x", fm)
    assert any("url" in e for e in errs)


def test_bad_interval_flagged():
    fm = {**VALID_FM, "source": {"type": "http_poll", "url": "u", "interval": "soon"}}
    assert any("interval" in e for e in validate_source_fm("x", fm))


def test_bad_cel_flagged():
    fm = {**VALID_FM, "condition": "a +"}
    assert any("CEL" in e for e in validate_source_fm("x", fm))


def test_bad_jsonpath_flagged():
    fm = {**VALID_FM, "extract": {"mode": "json_path", "path": "$.["}}
    assert any("JSONPath" in e for e in validate_source_fm("x", fm))


def test_action_not_in_allowed_actions():
    fm = {**VALID_FM, "allowed_actions": ["task_create"]}
    assert any("allowed_actions" in e for e in validate_source_fm("x", fm))


def test_unknown_source_type():
    fm = {**VALID_FM, "source": {"type": "carrier_pigeon", "interval": "6h"}}
    assert any("source.type" in e for e in validate_source_fm("x", fm))


def test_wrong_kind():
    fm = {**VALID_FM, "kind": "event_sauce"}
    assert any("kind" in e for e in validate_source_fm("x", fm))


# --- Tier-3 semantic block ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("2/3", (2, 3)), ("1/1", (1, 1)), (" 3 / 5 ", (3, 5))])
def test_parse_debounce_ok(raw, expected):
    assert parse_debounce(raw) == expected


@pytest.mark.parametrize("raw", ["", "2", "2/0", "4/3", "0/3", "a/b", 3, None])
def test_parse_debounce_bad(raw):
    assert parse_debounce(raw) is None


def test_valid_semantic_block_has_no_errors():
    assert validate_source_fm("nvda", {**VALID_FM, "semantic": _SEMANTIC}) == []


def test_from_frontmatter_carries_semantic():
    d = from_frontmatter("nvda", {**VALID_FM, "semantic": _SEMANTIC})
    assert d.semantic == _SEMANTIC
    assert from_frontmatter("nvda", VALID_FM).semantic is None  # absent → None


def test_semantic_missing_question():
    fm = {**VALID_FM, "semantic": {"query": "x"}}
    assert any("question" in e for e in validate_source_fm("x", fm))


def test_semantic_bad_cooldown():
    fm = {**VALID_FM, "semantic": {"question": "q", "cooldown": "soon"}}
    assert any("cooldown" in e for e in validate_source_fm("x", fm))


def test_semantic_bad_debounce():
    fm = {**VALID_FM, "semantic": {"question": "q", "debounce": "5/3"}}
    assert any("debounce" in e for e in validate_source_fm("x", fm))


def test_semantic_bad_min_confidence():
    fm = {**VALID_FM, "semantic": {"question": "q", "min_confidence": 1.5}}
    assert any("min_confidence" in e for e in validate_source_fm("x", fm))
