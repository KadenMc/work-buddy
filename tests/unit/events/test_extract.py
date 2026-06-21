"""Unit tests for source value extraction."""

from __future__ import annotations

from work_buddy.events.sources.extract import content_hash, extract_value


def test_json_path_scalar():
    assert extract_value("json_path", {"quote": {"price": 123}}, path="$.quote.price") == 123


def test_json_path_multi_returns_list():
    payload = {"items": [{"id": 1}, {"id": 2}]}
    assert extract_value("json_path", payload, path="$.items[*].id") == [1, 2]


def test_hash_differs_on_content():
    a = extract_value("hash", {"x": 1})
    b = extract_value("hash", {"x": 2})
    assert a != b
    assert len(a) == 64  # sha256 hex


def test_content_hash_is_order_independent():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
