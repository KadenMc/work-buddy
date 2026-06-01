"""Tests for the gateway's per-capability dispatch timeout.

The dispatch budget is owned by the operation and the gateway timeout is
opt-in: a ``timeout_seconds`` policy callable derives the budget from the
actual params, a scalar is a fixed ceiling, and an unset field is unbounded
(no gateway cap). A bounded budget composes a ``TimeoutStrategy``; an unbounded
one composes none, so a capability is never capped unless it asks to be.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

from work_buddy.mcp_server import dispatch_resilience as dr
from work_buddy.mcp_server.registry import Capability
from work_buddy.resilience import OutcomeKind, guarded_call


def _cap(**kw) -> Capability:
    return Capability(
        name="c", description="d", category="tasks", parameters={},
        callable=lambda **k: None, **kw,
    )


class TestBudgetResolution:
    def test_unset_is_unbounded_opt_in(self):
        # The gateway timeout is opt-in: an undeclared budget imposes no cap.
        # A flat default is not auto-applied (it would silently break the
        # deliberately-long-running capabilities — see TestOptInProtectsBlockers).
        assert dr.resolve_timeout_budget(_cap(), {}) == math.inf

    def test_unset_obsidian_is_unbounded(self):
        assert dr.resolve_timeout_budget(_cap(requires=["obsidian"]), {}) == math.inf

    def test_scalar_is_a_fixed_ceiling(self):
        assert dr.resolve_timeout_budget(_cap(timeout_seconds=120), {}) == 120.0

    def test_policy_derives_from_params(self):
        cap = _cap(timeout_seconds=lambda p: 10 + p.get("limit", 0) * 0.1)
        assert dr.resolve_timeout_budget(cap, {"limit": 10}) == 11.0
        assert dr.resolve_timeout_budget(cap, {"limit": 1000}) == 110.0

    def test_policy_that_raises_falls_back_to_unbounded(self):
        def _boom(_params):
            raise RuntimeError("bad policy")

        cap = _cap(timeout_seconds=_boom)
        assert dr.resolve_timeout_budget(cap, {}) == math.inf

    def test_policy_returning_none_is_unbounded(self):
        cap = _cap(timeout_seconds=lambda p: None)
        assert dr.resolve_timeout_budget(cap, {}) == math.inf

    def test_non_positive_budget_is_treated_as_unbounded(self):
        # A non-positive timeout is meaningless and TimeoutStrategy rejects it.
        assert dr.resolve_timeout_budget(_cap(timeout_seconds=0), {}) == math.inf
        assert dr.resolve_timeout_budget(_cap(timeout_seconds=-5), {}) == math.inf


class TestOptInProtectsBlockers:
    """Regression guard: capabilities that deliberately block or run long must
    NOT get a gateway wall-time cap unless they explicitly declare one. These
    declare ``requires=[]`` (they don't directly need a tool), so a flat
    non-bridge default would wrongly cap them — human-in-the-loop prompts would
    time out before the user answers, retries before the bridge recovers, LLM
    calls before the model returns."""

    def test_long_running_capabilities_are_unbounded(self):
        from unittest.mock import patch
        from work_buddy.mcp_server import registry as reg_mod

        reg_mod._REGISTRY = None
        try:
            with patch("work_buddy.tools.is_tool_available", return_value=True):
                reg = reg_mod.get_registry()
            disabled = reg_mod.get_disabled_registry()
            for name in (
                "request_send", "request_poll", "conversation_poll",
                "obsidian_retry", "retry", "llm_submit",
            ):
                entry = reg.get(name) or disabled.get(name)
                if entry is None:
                    continue  # capability may not be registered in this build
                budget = dr.resolve_timeout_budget(entry, {})
                assert budget == math.inf, (
                    f"{name} resolved to a {budget}s gateway cap; deliberately-"
                    f"long-running capabilities must be unbounded (opt-in timeout)"
                )
        finally:
            reg_mod._REGISTRY = None


class TestStrategyComposition:
    def test_bounded_budget_adds_one_timeout_strategy(self):
        strategies = dr.build_dispatch_strategies(_cap(), 30.0)
        assert [type(s).__name__ for s in strategies] == ["TimeoutStrategy"]

    def test_unbounded_budget_adds_no_timeout_strategy(self):
        # A self-managing capability is wrapped for telemetry but carries NO
        # TimeoutStrategy, so the gateway cannot falsely kill it. (Use a
        # non-obsidian cap forced unbounded to isolate the timeout behaviour
        # from the obsidian circuit breaker, which is covered separately in
        # test_gateway_circuit_breaker.)
        strategies = dr.build_dispatch_strategies(
            _cap(timeout_seconds=math.inf), math.inf,
        )
        assert all(
            type(s).__name__ != "TimeoutStrategy" for s in strategies
        )

    def test_unbounded_deadline_is_never(self):
        assert dr.build_dispatch_deadline(math.inf).at == math.inf
        assert dr.build_dispatch_deadline(30.0).at != math.inf


class TestTimeoutStrategyFires:
    def test_overrun_yields_timeout_outcome(self):
        strategies = dr.build_dispatch_strategies(_cap(timeout_seconds=0.05), 0.05)

        async def _run():
            return await guarded_call(
                "wb_run:slow_cap",
                lambda: asyncio.sleep(0.3, result="done"),
                deadline=dr.build_dispatch_deadline(0.05),
                strategies=strategies,
            )

        outcome = asyncio.run(_run())
        assert outcome.kind is OutcomeKind.TIMEOUT

    def test_fast_call_succeeds_under_budget(self):
        strategies = dr.build_dispatch_strategies(_cap(timeout_seconds=5.0), 5.0)

        async def _run():
            return await guarded_call(
                "wb_run:fast_cap",
                lambda: asyncio.sleep(0.0, result="done"),
                deadline=dr.build_dispatch_deadline(5.0),
                strategies=strategies,
            )

        outcome = asyncio.run(_run())
        assert outcome.is_success
        assert outcome.value == "done"


class TestGatewayWiringSmoke:
    def test_gateway_surfaces_mcp_gateway_timeout(self):
        source = (
            Path(__file__).parent.parent.parent
            / "work_buddy" / "mcp_server" / "tools" / "gateway.py"
        ).read_text(encoding="utf-8")
        assert "resolve_timeout_budget" in source, (
            "gateway.py no longer resolves a per-capability dispatch budget."
        )
        assert 'OutcomeKind.TIMEOUT' in source, (
            "gateway.py no longer handles the TIMEOUT outcome."
        )
        assert '"mcp_gateway_timeout"' in source, (
            "gateway.py no longer surfaces error_kind=mcp_gateway_timeout on "
            "a dispatch timeout."
        )
