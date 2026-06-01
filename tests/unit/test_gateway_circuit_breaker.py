"""Tests for the Obsidian-bridge circuit breaker on the gateway dispatch (Gap 3).

A missing Obsidian bridge no longer build-time disables its dependent
capabilities (covered in ``test_gateway_auto_recovery`` and
``test_registry_invariants``). Instead the gateway composes a shared circuit
breaker into the dispatch of every bridge-dependent capability: sustained
transient/timeout failures open it and shed (REJECTED) without hammering the
bridge, while terminal failures fail fast per call and never trip it.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from work_buddy.mcp_server import dispatch_resilience as dr
from work_buddy.mcp_server.registry import Capability
from work_buddy.obsidian.errors import ObsidianNotRunning, ObsidianTimeout
from work_buddy.resilience import OutcomeKind, guarded_call
from work_buddy.resilience.strategies import CircuitState


def _cap(**kw) -> Capability:
    return Capability(
        name="c", description="d", category="tasks", parameters={},
        callable=lambda **k: None, **kw,
    )


@pytest.fixture
def reset_breaker():
    """Reset the shared Obsidian breaker to CLOSED around a test."""
    breaker = dr._OBSIDIAN_BREAKER
    breaker._state = CircuitState.CLOSED
    breaker._consecutive_failures = 0
    breaker._opened_at = 0.0
    breaker._probe_in_flight = False
    yield breaker
    breaker._state = CircuitState.CLOSED
    breaker._consecutive_failures = 0
    breaker._opened_at = 0.0
    breaker._probe_in_flight = False


class TestStrategyAndClassifierSelection:
    def test_obsidian_cap_gets_breaker_no_timeout(self):
        # Unbounded budget (obsidian self-manages) → breaker only.
        strategies = dr.build_dispatch_strategies(_cap(requires=["obsidian"]), math.inf)
        assert [type(s).__name__ for s in strategies] == ["CircuitBreakerStrategy"]

    def test_non_obsidian_cap_gets_no_breaker(self):
        strategies = dr.build_dispatch_strategies(_cap(), 30.0)
        assert all(
            type(s).__name__ != "CircuitBreakerStrategy" for s in strategies
        )

    def test_obsidian_classifiers_and_passthrough(self):
        classify, result_classify = dr.dispatch_classifiers(_cap(requires=["obsidian"]))
        assert classify.__name__ == "classify_obsidian_error"
        assert result_classify.__name__ == "classify_bridge_result"
        passthrough = dr.dispatch_passthrough(_cap(requires=["obsidian"]))
        names = {t.__name__ for t in passthrough}
        # Control-flow + the bridge's own post-write-uncertain passthrough.
        assert {"ConsentRequired", "ToolUnavailable", "TypeError"} <= names
        assert "ObsidianPostWriteUncertain" in names

    def test_non_obsidian_passthrough_is_control_flow_only(self):
        passthrough = dr.dispatch_passthrough(_cap())
        names = {t.__name__ for t in passthrough}
        assert names == {"ConsentRequired", "ToolUnavailable", "TypeError"}


class TestBreakerTripAndShed:
    def _dispatch(self, breaker_strategies, classify, result_classify, fn):
        async def _run():
            return await guarded_call(
                "wb_run:obsidian_cap", fn,
                strategies=breaker_strategies,
                classify=classify,
                result_classifier=result_classify,
            )

        return asyncio.run(_run())

    def test_transient_failures_trip_then_shed(self, reset_breaker):
        cap = _cap(requires=["obsidian"])
        strategies = dr.build_dispatch_strategies(cap, math.inf)
        classify, result_classify = dr.dispatch_classifiers(cap)

        def _boom():
            raise ObsidianTimeout("bridge slow")

        # Five consecutive transient (TIMEOUT) failures trip the breaker.
        for _ in range(5):
            outcome = self._dispatch(strategies, classify, result_classify, _boom)
            assert outcome.kind is OutcomeKind.TIMEOUT
        assert reset_breaker.state is CircuitState.OPEN

        # The next call is shed without invoking the capability at all.
        invoked = {"n": 0}

        def _should_not_run():
            invoked["n"] += 1
            raise ObsidianTimeout("should not be called")

        shed = self._dispatch(strategies, classify, result_classify, _should_not_run)
        assert shed.kind is OutcomeKind.REJECTED
        assert invoked["n"] == 0, "open circuit must shed without invoking the call"

    def test_terminal_failure_does_not_trip(self, reset_breaker):
        cap = _cap(requires=["obsidian"])
        strategies = dr.build_dispatch_strategies(cap, math.inf)
        classify, result_classify = dr.dispatch_classifiers(cap)

        def _down():
            raise ObsidianNotRunning("obsidian closed")

        # Terminal failures fail fast per call but never trip the breaker —
        # they recover the instant the bridge returns, no shedding needed.
        for _ in range(8):
            outcome = self._dispatch(strategies, classify, result_classify, _down)
            assert outcome.kind is OutcomeKind.TERMINAL_FAILURE
        assert reset_breaker.state is CircuitState.CLOSED


class TestBridgeFamily:
    """The bridge failure domain is the Obsidian bridge AND its in-Obsidian
    plugins (datacore, smart_connections, google_calendar) — they go down
    together when the bridge is down. All are breaker-governed, and carved out
    of the build-time disable only when the bridge ITSELF is the cause."""

    def test_obsidian_backed_tools_includes_plugins(self):
        from work_buddy.tools import obsidian_backed_tools

        backed = obsidian_backed_tools()
        assert {"obsidian", "datacore", "smart_connections"} <= backed

    def test_plugin_cap_is_breaker_governed_and_unbounded(self):
        # A datacore-requiring cap (NOT directly requiring obsidian) still gets
        # the bridge breaker + obsidian classifiers + unbounded budget.
        cap = _cap(requires=["datacore"])
        assert dr.resolve_timeout_budget(cap, {}) == math.inf
        assert [type(s).__name__ for s in dr.build_dispatch_strategies(cap, math.inf)] == [
            "CircuitBreakerStrategy"
        ]
        classify, result_classify = dr.dispatch_classifiers(cap)
        assert classify.__name__ == "classify_obsidian_error"
        assert result_classify.__name__ == "classify_bridge_result"

    def test_plugin_cap_admitted_when_bridge_down_disabled_when_genuinely_missing(self):
        """Transitive-only: a datacore cap stays admitted when the bridge is
        down (breaker governs), but is hard-disabled when the bridge is up and
        the plugin itself is genuinely missing."""
        from unittest.mock import patch
        from work_buddy.mcp_server import registry as reg_mod
        from work_buddy.tools import DISABLED_CAPABILITIES

        def build(unavailable: set[str]):
            reg_mod._REGISTRY = None
            reg_mod._DISABLED_REGISTRY.clear()
            DISABLED_CAPABILITIES.clear()
            with patch(
                "work_buddy.tools.is_tool_available",
                side_effect=lambda t: t not in unavailable,
            ):
                reg_mod.get_registry()
            return dict(DISABLED_CAPABILITIES)

        try:
            # Bridge down → obsidian + all its plugins unavailable transitively.
            bridge_down = build(
                {"obsidian", "datacore", "smart_connections", "google_calendar"}
            )
            assert "datacore_query" not in bridge_down, (
                "datacore_query should stay admitted when the bridge is down"
            )
            # Bridge up, datacore plugin genuinely missing → hard-disable.
            plugin_missing = build({"datacore"})
            assert "datacore_query" in plugin_missing, (
                "datacore_query should hard-disable when the plugin is genuinely "
                "missing (bridge up)"
            )
        finally:
            reg_mod._REGISTRY = None
            reg_mod._DISABLED_REGISTRY.clear()
            DISABLED_CAPABILITIES.clear()


class TestGatewayWiringSmoke:
    def test_gateway_handles_rejected_shed(self):
        source = (
            Path(__file__).parent.parent.parent
            / "work_buddy" / "mcp_server" / "tools" / "gateway.py"
        ).read_text(encoding="utf-8")
        assert "OutcomeKind.REJECTED" in source, (
            "gateway.py no longer handles the circuit-breaker shed (REJECTED)."
        )
        assert "obsidian_bridge_circuit_open" in source, (
            "gateway.py no longer surfaces the bridge-circuit-open error_kind."
        )
        assert "dispatch_passthrough" in source, (
            "gateway.py no longer passes control-flow exceptions through the "
            "seam — the consent-retry loop / breaker accounting may be broken."
        )

    def test_registry_excludes_obsidian_from_build_time_disable(self):
        source = (
            Path(__file__).parent.parent.parent
            / "work_buddy" / "mcp_server" / "registry.py"
        ).read_text(encoding="utf-8")
        assert "obsidian_backed_tools" in source and "bridge_down" in source, (
            "registry.py no longer excludes the bridge-backed tool family from "
            "the build-time disable filter — a transient bridge probe failure "
            "would again disable every bridge-dependent capability for the session."
        )
