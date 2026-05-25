"""Tests for the resilience pipeline, builder, and registry (stage B1b)."""

from __future__ import annotations

import asyncio

import pytest

from work_buddy.resilience import (
    CircuitBreakerStrategy,
    InMemoryMetrics,
    OutcomeKind,
    ResiliencePipeline,
    ResiliencePipelineBuilder,
    ResiliencePipelineRegistry,
    RetryStrategy,
    TimeoutStrategy,
    get_pipeline_registry,
    register_listener,
)
from work_buddy.resilience.pipeline import _reset_pipeline_registry_for_tests
from work_buddy.resilience.telemetry import _reset_listeners_for_tests


@pytest.fixture(autouse=True)
def _reset():
    _reset_listeners_for_tests()
    _reset_pipeline_registry_for_tests()
    yield
    _reset_listeners_for_tests()
    _reset_pipeline_registry_for_tests()


# ===========================================================================
# ResiliencePipeline — basic execution
# ===========================================================================


def test_empty_pipeline_runs_the_call():
    pipeline = ResiliencePipelineBuilder("bare").build()
    outcome = asyncio.run(pipeline.execute(lambda: 42))
    assert outcome.unwrap() == 42


def test_pipeline_operation_key_defaults_to_name():
    m = InMemoryMetrics()
    register_listener(m)
    pipeline = ResiliencePipelineBuilder("named-policy").build()
    asyncio.run(pipeline.execute(lambda: 1))
    assert "named-policy/success" in m.snapshot()["counts_by_operation_outcome"]


def test_pipeline_operation_key_override():
    m = InMemoryMetrics()
    register_listener(m)
    pipeline = ResiliencePipelineBuilder("policy").build()
    asyncio.run(pipeline.execute(lambda: 1, operation_key="specific-call"))
    counts = m.snapshot()["counts_by_operation_outcome"]
    assert "specific-call/success" in counts


# ===========================================================================
# Builder
# ===========================================================================


def test_builder_accumulates_strategies_in_declaration_order():
    pipeline = (
        ResiliencePipelineBuilder("p")
        .timeout(5.0)
        .retry(max_attempts=2)
        .circuit_breaker(failure_threshold=3)
        .build()
    )
    kinds = [type(s) for s in pipeline.strategies]
    assert kinds == [TimeoutStrategy, RetryStrategy, CircuitBreakerStrategy]


def test_builder_passthrough_is_honored():
    class _Signal(Exception):
        pass

    def _fn():
        raise _Signal("control flow")

    pipeline = ResiliencePipelineBuilder("p").passthrough(_Signal).build()
    with pytest.raises(_Signal):
        asyncio.run(pipeline.execute(_fn))


def test_builder_custom_classifier_is_honored():
    def _fn():
        raise ValueError("x")

    pipeline = (
        ResiliencePipelineBuilder("p")
        .classify(lambda exc: OutcomeKind.TERMINAL_FAILURE)
        .build()
    )
    outcome = asyncio.run(pipeline.execute(_fn))
    assert outcome.kind is OutcomeKind.TERMINAL_FAILURE


# ===========================================================================
# Composition behaviour
# ===========================================================================


def test_pipeline_retry_recovers_a_flaky_call():
    calls: list[int] = []

    def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("flaky")
        return "ok"

    pipeline = (
        ResiliencePipelineBuilder("flaky")
        .retry(max_attempts=3, base_delay_s=0.01)
        .build()
    )
    outcome = asyncio.run(pipeline.execute(_flaky))
    assert outcome.is_success
    assert len(calls) == 3


def test_pipeline_timeout_fires_on_a_slow_call():
    async def _slow():
        await asyncio.sleep(0.5)
        return "ok"

    pipeline = ResiliencePipelineBuilder("p").timeout(0.05).build()
    outcome = asyncio.run(pipeline.execute(_slow))
    assert outcome.kind is OutcomeKind.TIMEOUT


def test_retry_wrapping_timeout_gives_per_attempt_timeouts():
    """Canonical order: Retry (outer) wraps Timeout (inner). The first
    attempt is too slow and times out; the retry attempt is fast and
    succeeds — proving each attempt gets its own timeout clock."""
    calls: list[int] = []

    async def _fn():
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0.4)  # attempt 1 exceeds the per-attempt cap
        return "ok"

    pipeline = (
        ResiliencePipelineBuilder("p")
        .retry(max_attempts=2, base_delay_s=0.01)
        .timeout(0.1)
        .build()
    )
    outcome = asyncio.run(pipeline.execute(_fn))
    assert outcome.is_success
    assert len(calls) == 2


def test_pipeline_circuit_breaker_state_persists_across_executes():
    def _fail():
        raise ValueError("dependency down")

    probe_calls: list[int] = []

    def _probe():
        probe_calls.append(1)
        return "ok"

    pipeline = (
        ResiliencePipelineBuilder("p")
        .circuit_breaker(failure_threshold=2, reset_timeout_s=10.0)
        .build()
    )
    # two failures across two separate executes -> the breaker opens
    asyncio.run(pipeline.execute(_fail))
    asyncio.run(pipeline.execute(_fail))
    # third execute: circuit open -> shed without invoking the call
    outcome = asyncio.run(pipeline.execute(_probe))
    assert outcome.kind is OutcomeKind.REJECTED
    assert probe_calls == []


# ===========================================================================
# Registry
# ===========================================================================


def test_registry_get_builds_and_caches():
    reg = ResiliencePipelineRegistry()
    builds: list[int] = []

    def _factory() -> ResiliencePipeline:
        builds.append(1)
        return ResiliencePipelineBuilder("cached").timeout(5.0).build()

    reg.register("cached", _factory)
    first = reg.get("cached")
    second = reg.get("cached")
    assert first is second           # cached — same instance
    assert len(builds) == 1          # factory ran exactly once


def test_registry_unknown_name_raises_keyerror():
    reg = ResiliencePipelineRegistry()
    with pytest.raises(KeyError):
        reg.get("never-registered")


def test_registry_names_lists_registered():
    reg = ResiliencePipelineRegistry()
    reg.register("a", lambda: ResiliencePipelineBuilder("a").build())
    reg.register("b", lambda: ResiliencePipelineBuilder("b").build())
    assert reg.names() == ["a", "b"]


def test_registry_reregister_invalidates_cache():
    reg = ResiliencePipelineRegistry()
    reg.register("p", lambda: ResiliencePipelineBuilder("v1").build())
    v1 = reg.get("p")
    reg.register("p", lambda: ResiliencePipelineBuilder("v2").build())
    v2 = reg.get("p")
    assert v1 is not v2
    assert v2.name == "v2"


def test_process_global_registry_is_a_singleton():
    assert get_pipeline_registry() is get_pipeline_registry()


def test_registry_pipeline_is_executable():
    reg = get_pipeline_registry()
    reg.register(
        "obsidian-ish",
        lambda: ResiliencePipelineBuilder("obsidian-ish")
        .retry(max_attempts=2, base_delay_s=0.01)
        .build(),
    )
    pipeline = reg.get("obsidian-ish")
    outcome = asyncio.run(pipeline.execute(lambda: "done"))
    assert outcome.unwrap() == "done"
