"""Unit tests for the component-gate AST (work_buddy.control.gates)."""

from __future__ import annotations

import pytest

from work_buddy.control.gates import (
    And,
    Component,
    Not,
    Or,
    evaluate,
    from_json,
    parse_gate,
    referenced_components,
    to_json,
    validate,
)

pytestmark = pytest.mark.unit


def test_component_gate_membership():
    assert evaluate(Component("obsidian"), {"obsidian"}) is True
    assert evaluate(Component("obsidian"), {"telegram"}) is False
    assert evaluate(Component("obsidian"), set()) is False


def test_and_or_not_truth_tables():
    a, b = Component("a"), Component("b")
    assert evaluate(And((a, b)), {"a", "b"}) is True
    assert evaluate(And((a, b)), {"a"}) is False
    assert evaluate(Or((a, b)), {"a"}) is True
    assert evaluate(Or((a, b)), set()) is False
    assert evaluate(Not(a), set()) is True
    assert evaluate(Not(a), {"a"}) is False


def test_empty_and_is_true__empty_or_is_false():
    assert evaluate(And(()), set()) is True       # vacuous truth
    assert evaluate(Or(()), set()) is False       # vacuous falsity


def test_none_gate_is_always_active():
    assert evaluate(None, set()) is True


def test_nested_expression():
    # obsidian & (thunderbird | outlook)
    gate = And((
        Component("obsidian"),
        Or((Component("thunderbird"), Component("outlook"))),
    ))
    assert evaluate(gate, {"obsidian", "outlook"}) is True
    assert evaluate(gate, {"obsidian"}) is False
    assert evaluate(gate, {"thunderbird"}) is False


def test_referenced_components_walks_nested_tree():
    gate = And((
        Component("obsidian"),
        Or((Component("thunderbird"), Not(Component("outlook")))),
    ))
    assert referenced_components(gate) == {"obsidian", "thunderbird", "outlook"}
    assert referenced_components(None) == set()


def test_validate_accepts_known_components():
    validate(Component("obsidian"), {"obsidian", "telegram"})  # no raise
    validate(None, set())                                      # no raise


def test_validate_rejects_unknown_component():
    with pytest.raises(ValueError, match="unknown components"):
        validate(
            And((Component("obsidian"), Component("nonexistent"))),
            {"obsidian"},
        )


def test_to_json_from_json_roundtrip():
    gate = And((
        Component("obsidian"),
        Or((Component("thunderbird"), Not(Component("outlook")))),
    ))
    assert from_json(to_json(gate)) == gate
    assert to_json(None) is None
    assert from_json(None) is None


def test_parse_simple_identifier():
    assert parse_gate("obsidian") == Component("obsidian")


def test_parse_and_or_precedence():
    # & binds tighter than |  ->  a | (b & c)
    assert parse_gate("a | b & c") == Or((
        Component("a"),
        And((Component("b"), Component("c"))),
    ))


def test_parse_not_binds_tightest():
    # !a & b  ->  (!a) & b
    assert parse_gate("!a & b") == And((Not(Component("a")), Component("b")))


def test_parse_parenthesised_user_example():
    assert parse_gate("obsidian & (thunderbird | outlook)") == And((
        Component("obsidian"),
        Or((Component("thunderbird"), Component("outlook"))),
    ))


def test_parse_roundtrips_through_json():
    gate = parse_gate("obsidian & (thunderbird | outlook)")
    assert from_json(to_json(gate)) == gate


@pytest.mark.parametrize("expr", [
    "",
    "   ",
    "a &",
    "& a",
    "(a",
    "a)",
    "a b",
    "a & | b",
    "!",
])
def test_parse_rejects_malformed(expr):
    with pytest.raises(ValueError):
        parse_gate(expr)
