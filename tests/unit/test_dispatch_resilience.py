"""Tests for the gateway's dispatch-resilience wiring.

The gateway routes every ``wb_run`` capability dispatch through
``guarded_call`` so it emits dispatch-timing telemetry. These tests cover the
``dispatch_resilience`` module's primitives (listener registration, the
in-process metrics recorder, the log listener) and a wiring smoke test that
the gateway source still routes the dispatch through the seam.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from work_buddy.mcp_server import dispatch_resilience as dr
from work_buddy.resilience import guarded_call
from work_buddy.resilience.telemetry import (
    CallCompleted,
    OutcomeKind,
    _reset_listeners_for_tests,
    get_listeners,
)


@pytest.fixture(autouse=True)
def _clean_listeners():
    _reset_listeners_for_tests()
    dr._LISTENERS_READY = False
    dr.get_dispatch_metrics().reset()
    yield
    _reset_listeners_for_tests()
    dr._LISTENERS_READY = False
    dr.get_dispatch_metrics().reset()


class TestListenerRegistration:
    def test_registers_metrics_and_log_listeners(self):
        dr.ensure_listeners_registered()
        names = sorted(type(listener).__name__ for listener in get_listeners())
        assert names == ["InMemoryMetrics", "_DispatchLogListener"]

    def test_is_idempotent(self):
        dr.ensure_listeners_registered()
        dr.ensure_listeners_registered()
        dr.ensure_listeners_registered()
        assert len(get_listeners()) == 2


class TestDispatchTelemetry:
    def test_guarded_dispatch_records_under_wb_run_key(self):
        dr.ensure_listeners_registered()

        async def _run():
            return await guarded_call("wb_run:demo_cap", lambda: "ok")

        outcome = asyncio.run(_run())
        assert outcome.is_success
        assert outcome.value == "ok"

        snap = dr.get_dispatch_metrics().snapshot()
        assert snap["call_count"] == 1
        assert "wb_run:demo_cap/success" in snap["counts_by_operation_outcome"]

    def test_log_listener_emits_grep_able_line(self, caplog):
        listener = dr._DispatchLogListener()
        event = CallCompleted(
            operation_key="wb_run:demo_cap",
            call_id="abc123",
            duration_s=0.042,
            outcome=OutcomeKind.SUCCESS,
        )
        with caplog.at_level(logging.INFO, logger="work_buddy.mcp_server.dispatch"):
            listener.on_event(event)
        assert "guard.call op=wb_run:demo_cap" in caplog.text
        assert "outcome=success" in caplog.text


class TestGatewayWiringSmoke:
    def test_gateway_routes_dispatch_through_guarded_call(self):
        source = (
            Path(__file__).parent.parent.parent
            / "work_buddy" / "mcp_server" / "tools" / "gateway.py"
        ).read_text(encoding="utf-8")
        assert "guarded_call(" in source, (
            "gateway.py no longer routes the dispatch through guarded_call — "
            "dispatch telemetry / timeout wiring may have been removed."
        )
        assert 'f"wb_run:{capability}"' in source, (
            "gateway.py no longer tags the dispatch with the wb_run:<cap> "
            "operation key."
        )
        assert "ensure_listeners_registered" in source, (
            "gateway.py no longer registers the resilience telemetry listeners."
        )
