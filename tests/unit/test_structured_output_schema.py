"""Tests for :func:`work_buddy.llm.runner._normalize_structured_output_schema`.

Anthropic's constrained-decoding structured-output API (and OpenAI's
strict ``json_schema`` mode) accept only a subset of JSON Schema:
objects must set ``additionalProperties: false``, and validation
keywords like ``maxItems`` / ``minimum`` / ``pattern`` are rejected
outright. The runner normalizes every structured-output schema so
schema authors never have to track the dialect.
"""

from __future__ import annotations

from work_buddy.llm.runner import _normalize_structured_output_schema as _norm


def _objects_missing_strict(node, path="root"):
    """Paths of object nodes lacking ``additionalProperties: false``."""
    bad = []
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                bad.append(path)
        for key, value in node.items():
            bad += _objects_missing_strict(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            bad += _objects_missing_strict(value, f"{path}[{i}]")
    return bad


def _keys_present(node):
    """Flat set of every dict key anywhere in the schema."""
    keys = set()
    if isinstance(node, dict):
        keys |= set(node.keys())
        for v in node.values():
            keys |= _keys_present(v)
    elif isinstance(node, list):
        for v in node:
            keys |= _keys_present(v)
    return keys


# --- additionalProperties: false on objects ---------------------------

def test_top_level_object_gets_strict():
    out = _norm({"type": "object", "properties": {"a": {"type": "string"}}})
    assert out["additionalProperties"] is False


def test_nested_object_in_items_gets_strict():
    out = _norm({
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {"type": "object", "properties": {"x": {"type": "integer"}}},
            },
        },
    })
    assert out["additionalProperties"] is False
    assert out["properties"]["rows"]["items"]["additionalProperties"] is False


def test_object_without_explicit_type_still_gets_strict():
    out = _norm({"properties": {"a": {"type": "string"}}})
    assert out["additionalProperties"] is False


def test_anyof_and_defs_branches_covered():
    out = _norm({
        "$defs": {"Inner": {"type": "object", "properties": {"a": {"type": "string"}}}},
        "anyOf": [
            {"type": "object", "properties": {"b": {"type": "string"}}},
            {"type": "null"},
        ],
    })
    assert out["$defs"]["Inner"]["additionalProperties"] is False
    assert out["anyOf"][0]["additionalProperties"] is False
    assert "additionalProperties" not in out["anyOf"][1]


def test_existing_additional_properties_forced_false():
    out = _norm({"type": "object", "properties": {}, "additionalProperties": True})
    assert out["additionalProperties"] is False


# --- unsupported validation keywords are stripped ---------------------

def test_unsupported_array_and_numeric_keywords_stripped():
    out = _norm({
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"},
                     "maxItems": 5, "uniqueItems": True},
            "score": {"type": "number", "minimum": 0, "maximum": 100,
                      "multipleOf": 5},
            "name": {"type": "string", "minLength": 1, "maxLength": 80,
                     "pattern": "^[a-z]+$"},
        },
    })
    present = _keys_present(out)
    for banned in ("maxItems", "uniqueItems", "minimum", "maximum",
                   "multipleOf", "minLength", "maxLength", "pattern"):
        assert banned not in present, f"{banned} should have been stripped"
    # Shape keywords survive.
    assert out["properties"]["tags"]["type"] == "array"
    assert out["properties"]["tags"]["items"] == {"type": "string"}


def test_minitems_kept_only_for_zero_or_one():
    kept0 = _norm({"type": "array", "items": {"type": "string"}, "minItems": 0})
    kept1 = _norm({"type": "array", "items": {"type": "string"}, "minItems": 1})
    dropped = _norm({"type": "array", "items": {"type": "string"}, "minItems": 2})
    assert kept0["minItems"] == 0
    assert kept1["minItems"] == 1
    assert "minItems" not in dropped


# --- general invariants -----------------------------------------------

def test_non_object_nodes_untouched():
    assert _norm({"type": "string"}) == {"type": "string"}
    assert _norm({"type": "array", "items": {"type": "integer"}}) == {
        "type": "array", "items": {"type": "integer"},
    }


def test_input_schema_not_mutated():
    original = {"type": "object", "properties": {"a": {"type": "string"}},
                "maxProperties": 3}
    out = _norm(original)
    assert out["additionalProperties"] is False
    assert "maxProperties" not in out
    # The caller's dict is left exactly as it was.
    assert "additionalProperties" not in original
    assert original["maxProperties"] == 3


def test_real_summary_output_schema_is_api_compliant():
    """A real in-tree schema (conversation_observability's summary
    schema) normalizes to a fully API-compliant form — no object
    missing ``additionalProperties``, no ``maxItems``/``minItems``."""
    from work_buddy.conversation_observability.summaries import SUMMARY_OUTPUT_SCHEMA

    out = _norm(SUMMARY_OUTPUT_SCHEMA)
    assert _objects_missing_strict(out) == []
    present = _keys_present(out)
    assert "maxItems" not in present
    assert "minItems" not in present  # the schema's only minItems was 2
    # Original left untouched (deep copy).
    assert "additionalProperties" not in SUMMARY_OUTPUT_SCHEMA
