"""Tests for the Obsidian bridge resilience adapter (stage A3).

Exercises ``guarded_bridge_call``: typed-error and ``bridge_failure``-dict
mapping onto the outcome taxonomy, ``ObsidianPostWriteUncertain`` passthrough,
and ``guard.*`` telemetry. The bridge layer (``obsidian/retry.py``, the typed
error hierarchy) is not modified by the adapter.
"""

from __future__ import annotations

import asyncio

import pytest

from work_buddy.obsidian.errors import (
    ObsidianEditorConflict,
    ObsidianError,
    ObsidianNotRunning,
    ObsidianPluginDisabled,
    ObsidianPostWriteUncertain,
    ObsidianRefused,
    ObsidianServerError,
    ObsidianStartupRace,
    ObsidianTimeout,
)
from work_buddy.obsidian.resilient_bridge import (
    build_obsidian_pipeline,
    classify_bridge_result,
    classify_obsidian_error,
    guarded_bridge_call,
)
from work_buddy.obsidian.retry import bridge_failure
from work_buddy.resilience import InMemoryMetrics, OutcomeKind, register_listener
from work_buddy.resilience.telemetry import _reset_listeners_for_tests


@pytest.fixture(autouse=True)
def _reset_listeners():
    _reset_listeners_for_tests()
    yield
    _reset_listeners_for_tests()


# ---------------------------------------------------------------------------
# classify_obsidian_error
# ---------------------------------------------------------------------------


def test_classify_obsidian_error_terminal_types():
    for exc in (
        ObsidianNotRunning("down"),
        ObsidianPluginDisabled("disabled"),
        ObsidianRefused(403),
    ):
        assert classify_obsidian_error(exc) is OutcomeKind.TERMINAL_FAILURE


def test_classify_obsidian_error_timeout():
    assert classify_obsidian_error(
        ObsidianTimeout("slow")
    ) is OutcomeKind.TIMEOUT


def test_classify_obsidian_error_transient_types():
    for exc in (
        ObsidianStartupRace("race"),
        ObsidianEditorConflict("notes/x.md"),
        ObsidianServerError(500),
        ObsidianError("unknown"),
    ):
        assert classify_obsidian_error(exc) is OutcomeKind.TRANSIENT_FAILURE


def test_classify_obsidian_error_non_obsidian_falls_through():
    assert classify_obsidian_error(
        ValueError("x")
    ) is OutcomeKind.TRANSIENT_FAILURE
    assert classify_obsidian_error(TimeoutError()) is OutcomeKind.TIMEOUT


# ---------------------------------------------------------------------------
# classify_bridge_result
# ---------------------------------------------------------------------------


def test_classify_bridge_result_terminal():
    failure = bridge_failure(
        "obsidian down", state="obsidian_not_running", state_detail="x",
    )
    assert classify_bridge_result(failure) is OutcomeKind.TERMINAL_FAILURE


def test_classify_bridge_result_transient():
    failure = bridge_failure("slow", state="timeout", state_detail="x")
    assert classify_bridge_result(failure) is OutcomeKind.TRANSIENT_FAILURE


def test_classify_bridge_result_genuine_success_is_none():
    assert classify_bridge_result(
        {"path": "notes/x.md", "created": True}
    ) is None
    assert classify_bridge_result("ok") is None


# ---------------------------------------------------------------------------
# guarded_bridge_call — happy path + exception mapping
# ---------------------------------------------------------------------------


def test_guarded_bridge_call_success():
    outcome = asyncio.run(guarded_bridge_call(
        lambda: {"path": "notes/x.md", "created": True},
        operation_key="obsidian:write",
    ))
    assert outcome.is_success
    assert outcome.unwrap()["created"] is True


def test_guarded_bridge_call_maps_server_error_to_transient():
    def _fn():
        raise ObsidianServerError(500)

    outcome = asyncio.run(guarded_bridge_call(
        _fn, operation_key="obsidian:write",
    ))
    assert outcome.kind is OutcomeKind.TRANSIENT_FAILURE
    assert isinstance(outcome.error, ObsidianServerError)


def test_guarded_bridge_call_maps_not_running_to_terminal():
    def _fn():
        raise ObsidianNotRunning("obsidian closed")

    outcome = asyncio.run(guarded_bridge_call(
        _fn, operation_key="obsidian:read",
    ))
    assert outcome.kind is OutcomeKind.TERMINAL_FAILURE


def test_guarded_bridge_call_maps_timeout():
    def _fn():
        raise ObsidianTimeout("no response")

    outcome = asyncio.run(guarded_bridge_call(
        _fn, operation_key="obsidian:read",
    ))
    assert outcome.kind is OutcomeKind.TIMEOUT


# ---------------------------------------------------------------------------
# ObsidianPostWriteUncertain — passthrough
# ---------------------------------------------------------------------------


def test_post_write_uncertain_propagates_untouched():
    """PWU is a control-flow signal for the gateway verify-then-decide path
    — the adapter must re-raise it, not box it into an Outcome."""
    def _fn():
        raise ObsidianPostWriteUncertain("notes/x.md", write_mode="append")

    with pytest.raises(ObsidianPostWriteUncertain):
        asyncio.run(guarded_bridge_call(_fn, operation_key="obsidian:write"))


# ---------------------------------------------------------------------------
# bridge_failure return-dict mapping
# ---------------------------------------------------------------------------


def test_guarded_bridge_call_transient_bridge_failure_dict():
    def _fn():
        return bridge_failure("bridge slow", state="timeout", state_detail="x")

    outcome = asyncio.run(guarded_bridge_call(
        _fn, operation_key="obsidian:write",
    ))
    assert not outcome.is_success
    assert outcome.kind is OutcomeKind.TRANSIENT_FAILURE
    assert outcome.metadata["result"]["message"] == "bridge slow"


def test_guarded_bridge_call_terminal_bridge_failure_dict():
    def _fn():
        return bridge_failure(
            "obsidian down", state="obsidian_not_running", state_detail="x",
        )

    outcome = asyncio.run(guarded_bridge_call(
        _fn, operation_key="obsidian:write",
    ))
    assert outcome.kind is OutcomeKind.TERMINAL_FAILURE


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_guarded_bridge_call_emits_telemetry():
    m = InMemoryMetrics()
    register_listener(m)
    asyncio.run(guarded_bridge_call(
        lambda: "ok", operation_key="obsidian:read",
    ))
    snap = m.snapshot()
    assert snap["counts_by_operation_outcome"]["obsidian:read/success"] == 1


# ---------------------------------------------------------------------------
# build_obsidian_pipeline — the framework-native @bridge_retry equivalent (B3)
# ---------------------------------------------------------------------------


def test_obsidian_pipeline_retries_transient_then_succeeds():
    pipeline = build_obsidian_pipeline(max_attempts=3, retry_base_delay_s=0.01)
    calls: list[int] = []

    def _fn():
        calls.append(1)
        if len(calls) < 3:
            raise ObsidianServerError(500)  # 5xx -> TRANSIENT_FAILURE
        return {"ok": True}

    outcome = asyncio.run(pipeline.execute(_fn))
    assert outcome.is_success
    assert len(calls) == 3


def test_obsidian_pipeline_does_not_retry_terminal():
    pipeline = build_obsidian_pipeline(max_attempts=5, retry_base_delay_s=0.01)
    calls: list[int] = []

    def _fn():
        calls.append(1)
        raise ObsidianNotRunning("obsidian closed")  # terminal

    outcome = asyncio.run(pipeline.execute(_fn))
    assert outcome.kind is OutcomeKind.TERMINAL_FAILURE
    assert len(calls) == 1  # terminal failures short-circuit retry


def test_obsidian_pipeline_passes_post_write_uncertain_through():
    pipeline = build_obsidian_pipeline()

    def _fn():
        raise ObsidianPostWriteUncertain("notes/x.md", write_mode="append")

    with pytest.raises(ObsidianPostWriteUncertain):
        asyncio.run(pipeline.execute(_fn))


def test_obsidian_pipeline_maps_terminal_bridge_failure_dict():
    pipeline = build_obsidian_pipeline(max_attempts=1)

    def _fn():
        return bridge_failure(
            "obsidian down", state="obsidian_not_running", state_detail="x",
        )

    outcome = asyncio.run(pipeline.execute(_fn))
    assert outcome.kind is OutcomeKind.TERMINAL_FAILURE


def test_obsidian_pipeline_success_passthrough():
    pipeline = build_obsidian_pipeline()
    outcome = asyncio.run(pipeline.execute(
        lambda: {"path": "notes/x.md", "created": True},
    ))
    assert outcome.is_success
    assert outcome.unwrap()["created"] is True
