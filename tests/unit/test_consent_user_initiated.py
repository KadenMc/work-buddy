"""Tests for the ``user_initiated`` consent context manager.

The premise: UI clicks are themselves consent. ``@requires_consent``
gates exist for autonomous agent actions, not for code paths the
user explicitly invoked. ``user_initiated`` is the seam that lets
endpoint handlers say "this came from a click — let nested gates
pass through."
"""

from __future__ import annotations

import pytest

from work_buddy import consent
from work_buddy.consent import (
    ConsentRequired,
    Risk,
    _cache,
    in_consent_context,
    requires_consent,
    user_initiated,
)


@pytest.fixture(autouse=True)
def _clear_consent_cache():
    """Each test starts with no granted consents."""
    _cache._grants.clear() if hasattr(_cache, "_grants") else None
    # Defensive — try a few possible internal-state shapes.
    for attr in ("_grants", "_cache", "_data"):
        store = getattr(_cache, attr, None)
        if isinstance(store, dict):
            store.clear()
    yield


# ---------------------------------------------------------------------------
# Without user_initiated: gates fire as expected
# ---------------------------------------------------------------------------


def test_without_user_initiated_gate_blocks() -> None:
    """Sanity: a moderate-risk @requires_consent function raises
    ConsentRequired when called from a fresh context."""

    @requires_consent(
        operation="test.moderate_op",
        risk="moderate",
        reason="test",
    )
    def do_thing() -> str:
        return "did it"

    with pytest.raises(ConsentRequired):
        do_thing()


# ---------------------------------------------------------------------------
# With user_initiated: gates pass through
# ---------------------------------------------------------------------------


def test_user_initiated_lets_moderate_op_pass() -> None:
    """A moderate-risk op called inside ``user_initiated`` runs without
    prompting."""

    @requires_consent(
        operation="test.moderate_op",
        risk="moderate",
        reason="test",
    )
    def do_thing() -> str:
        return "did it"

    with user_initiated("test.click"):
        result = do_thing()
    assert result == "did it"


def test_user_initiated_lets_high_risk_op_pass() -> None:
    """The pass-through is unconditional on the inner risk level —
    the click is the consent for everything in the block."""

    @requires_consent(
        operation="test.high_op",
        risk="high",
        reason="test",
    )
    def do_thing() -> str:
        return "high-risk action"

    with user_initiated("test.click"):
        result = do_thing()
    assert result == "high-risk action"


def test_in_consent_context_true_inside_user_initiated() -> None:
    """``in_consent_context`` reflects the depth correctly."""
    assert in_consent_context() is False
    with user_initiated("test.click"):
        assert in_consent_context() is True
    assert in_consent_context() is False


def test_user_initiated_restores_context_on_exception() -> None:
    """Even if the block raises, the context must restore so the
    next request starts clean."""

    @requires_consent(
        operation="test.moderate_op",
        risk="moderate",
        reason="test",
    )
    def do_thing() -> None:
        raise RuntimeError("oops")

    with pytest.raises(RuntimeError, match="oops"):
        with user_initiated("test.click"):
            do_thing()
    assert in_consent_context() is False


# ---------------------------------------------------------------------------
# Reentrancy
# ---------------------------------------------------------------------------


def test_nested_user_initiated_blocks_compose() -> None:
    """Nested user_initiated blocks each add a depth layer; the gate
    passes through at any depth > 0."""

    @requires_consent(
        operation="test.moderate_op",
        risk="moderate",
        reason="test",
    )
    def do_thing() -> str:
        return "ran"

    with user_initiated("outer.click"):
        with user_initiated("inner.click"):
            result = do_thing()
    assert result == "ran"
    assert in_consent_context() is False


# ---------------------------------------------------------------------------
# Multiple operations cascade correctly
# ---------------------------------------------------------------------------


def test_two_inner_ops_both_covered_by_one_user_initiated() -> None:
    """A single click can authorize a handler that performs multiple
    consent-gated operations in sequence."""

    @requires_consent(
        operation="test.op_a",
        risk="moderate",
        reason="A",
    )
    def op_a() -> str:
        return "A"

    @requires_consent(
        operation="test.op_b",
        risk="high",
        reason="B",
    )
    def op_b() -> str:
        return "B"

    with user_initiated("test.click"):
        a = op_a()
        b = op_b()
    assert a == "A" and b == "B"


# ---------------------------------------------------------------------------
# Outside the context, unaffected operations are unchanged
# ---------------------------------------------------------------------------


def test_after_block_gate_blocks_again() -> None:
    """Leaving the block restores the gating behavior — the context
    is scoped to the block, not 'always allowed' afterward."""

    @requires_consent(
        operation="test.moderate_op",
        risk="moderate",
        reason="test",
    )
    def do_thing() -> str:
        return "ran"

    with user_initiated("test.click"):
        do_thing()  # passes

    with pytest.raises(ConsentRequired):
        do_thing()  # blocks again
