"""Unit tests for CelCondition (safe predicate, fail-closed)."""

from __future__ import annotations

import pytest

from work_buddy.events.conditions.cel import CelCondition
from work_buddy.events.envelope import new_event
from work_buddy.events.protocol import ConditionContext


def _evt(current, prev):
    return new_event(
        "/wb/source/nvda",
        "ai.workbuddy.source.nvda.changed",
        data={"current": current, "prev": prev, "source_name": "nvda"},
        modality="pull",
    )


def _eval(expr, current, prev):
    return CelCondition(expr).evaluate(_evt(current, prev), None, ConditionContext())


def test_object_field_inequality():
    expr = "event.data.ceo != prev.data.ceo"
    assert _eval(expr, {"ceo": "B"}, {"ceo": "A"}) is True
    assert _eval(expr, {"ceo": "A"}, {"ceo": "A"}) is False


def test_scalar_shorthand():
    assert _eval("current != prev.data", "B", "A") is True
    assert _eval("event.data != prev.data", "A", "A") is False


def test_numeric_threshold_with_abs():
    expr = "abs(event.data.price - prev.data.price) / prev.data.price > 0.05"
    assert _eval(expr, {"price": 110.0}, {"price": 100.0}) is True   # 10% move
    assert _eval(expr, {"price": 102.0}, {"price": 100.0}) is False  # 2% move


def test_fail_closed_on_missing_field():
    # `event.data` is a scalar here, so `.ceo` access errors → False, not a raise.
    assert _eval("event.data.ceo != prev.data.ceo", "B", "A") is False


def test_bad_syntax_raises_at_construction():
    with pytest.raises(Exception):
        CelCondition("a +")
