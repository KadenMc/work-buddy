"""Tests for the decomposed-LLM-judgment framework.

The LLMRunner is mocked throughout — we're testing the composition logic
(prompt building, working-dict propagation, soft/hard fail, tier-chain
resolution from config), not the underlying call. Live LLM tests live
elsewhere.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from work_buddy.llm.decomposed import (
    DecomposedJudgment,
    MainCall,
    SubCall,
    _compose_trace_id,
    _resolve_dials,
    _walk_dotted,
    run_subcall,
)
from work_buddy.llm.response import ErrorKind, LLMResponse, TierAttempt
from work_buddy.llm.tiers import ModelTier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(structured: dict[str, Any], tier: str = "local_fast") -> LLMResponse:
    return LLMResponse(
        content="",
        structured_output=structured,
        tier_used=tier,
        tier_attempts=(
            TierAttempt(
                tier=tier, model="qwen3-4b", error_kind=None, error=None,
                elapsed_ms=42, outcome="success", input_tokens=10, output_tokens=20,
            ),
        ),
        model="qwen3-4b",
    )


def _err_response(
    kind: ErrorKind = ErrorKind.TIMEOUT, tier: str = "frontier_fast",
) -> LLMResponse:
    return LLMResponse(
        content="",
        structured_output=None,
        tier_used=tier,
        tier_attempts=(
            TierAttempt(
                tier=tier, model="claude-haiku", error_kind=kind, error="boom",
                elapsed_ms=99, outcome="backend_error", input_tokens=5, output_tokens=0,
            ),
        ),
        model="claude-haiku",
        error="boom",
        error_kind=kind,
    )


_TRIVIAL_SCHEMA = {"type": "object"}


def _trivial_user_prompt(inputs: dict[str, Any]) -> str:
    return f"input keys: {sorted(inputs.keys())}"


def _make_subcall(
    *,
    name: str = "sc",
    fail_policy: str = "soft",
    soft_fail_default: dict | None = None,
    config_key: str | None = None,
) -> SubCall:
    return SubCall(
        name=name,
        system_prompt="sys",
        user_prompt=_trivial_user_prompt,
        output_schema=_TRIVIAL_SCHEMA,
        config_key=config_key,
        fail_policy=fail_policy,
        soft_fail_default=(soft_fail_default if soft_fail_default is not None
                           else ({} if fail_policy == "soft" else None)),
    )


def _make_maincall(name: str = "main", config_key: str | None = None) -> MainCall:
    return MainCall(
        name=name,
        system_prompt="sys-main",
        user_prompt=_trivial_user_prompt,
        output_schema=_TRIVIAL_SCHEMA,
        config_key=config_key,
    )


# ---------------------------------------------------------------------------
# SubCall constructor validation
# ---------------------------------------------------------------------------


def test_subcall_soft_fail_requires_default() -> None:
    """fail_policy='soft' demands a soft_fail_default at construction."""
    with pytest.raises(ValueError, match="soft_fail_default"):
        SubCall(
            name="x",
            system_prompt="sys",
            user_prompt=_trivial_user_prompt,
            output_schema=_TRIVIAL_SCHEMA,
            fail_policy="soft",
            soft_fail_default=None,
        )


def test_subcall_hard_fail_default_optional() -> None:
    """fail_policy='hard' does NOT require soft_fail_default."""
    sc = SubCall(
        name="x",
        system_prompt="sys",
        user_prompt=_trivial_user_prompt,
        output_schema=_TRIVIAL_SCHEMA,
        fail_policy="hard",
        soft_fail_default=None,
    )
    assert sc.fail_policy == "hard"


def test_subcall_invalid_fail_policy() -> None:
    with pytest.raises(ValueError, match="fail_policy"):
        SubCall(
            name="x", system_prompt="s", user_prompt=_trivial_user_prompt,
            output_schema=_TRIVIAL_SCHEMA, fail_policy="lenient",  # type: ignore[arg-type]
            soft_fail_default={},
        )


def test_subcall_empty_name_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        SubCall(
            name="   ", system_prompt="s", user_prompt=_trivial_user_prompt,
            output_schema=_TRIVIAL_SCHEMA, soft_fail_default={},
        )


def test_subcall_user_prompt_must_be_callable() -> None:
    with pytest.raises(ValueError, match="callable"):
        SubCall(
            name="x", system_prompt="s",
            user_prompt="static",  # type: ignore[arg-type]
            output_schema=_TRIVIAL_SCHEMA, soft_fail_default={},
        )


# ---------------------------------------------------------------------------
# DecomposedJudgment constructor validation
# ---------------------------------------------------------------------------


def test_decomposed_rejects_duplicate_subcall_names() -> None:
    sub_a = _make_subcall(name="dup")
    sub_b = _make_subcall(name="dup")
    with pytest.raises(ValueError, match="duplicate"):
        DecomposedJudgment(
            name="chain", sub_calls=(sub_a, sub_b), main=_make_maincall(),
        )


def test_decomposed_rejects_main_name_collision_with_subcall() -> None:
    sub = _make_subcall(name="verdict")
    with pytest.raises(ValueError, match="collides"):
        DecomposedJudgment(
            name="chain", sub_calls=(sub,), main=_make_maincall(name="verdict"),
        )


# ---------------------------------------------------------------------------
# run_subcall — single-step entry point
# ---------------------------------------------------------------------------


def test_run_subcall_happy_path() -> None:
    sub = _make_subcall(soft_fail_default={"zero": 0})
    runner = MagicMock()
    runner.call.return_value = _ok_response({"echo": "hi"})

    result = run_subcall(sub, {"text": "hi"}, runner=runner, trace_id="t-1")

    assert result.ok is True
    assert result.failed_softly is False
    assert result.output == {"echo": "hi"}
    assert result.tier_used == "local_fast"
    runner.call.assert_called_once()
    call_kwargs = runner.call.call_args.kwargs
    assert call_kwargs["system"] == "sys"
    assert "text" in call_kwargs["user"]
    assert call_kwargs["trace_id"] == "t-1"
    assert call_kwargs["output_schema"] is _TRIVIAL_SCHEMA


def test_run_subcall_soft_fail_substitutes_default() -> None:
    default = {"has_x": False, "x": None}
    sub = _make_subcall(fail_policy="soft", soft_fail_default=default)
    runner = MagicMock()
    runner.call.return_value = _err_response()

    result = run_subcall(sub, {}, runner=runner)

    assert result.ok is False
    assert result.failed_softly is True
    assert result.output == default
    # Defensive copy — mutating the result must not bleed into the SubCall.
    result.output["mutated"] = True
    assert "mutated" not in default


def test_run_subcall_hard_fail_returns_failure() -> None:
    sub = _make_subcall(fail_policy="hard")
    runner = MagicMock()
    runner.call.return_value = _err_response(kind=ErrorKind.SCHEMA_VIOLATION)

    result = run_subcall(sub, {}, runner=runner)

    assert result.ok is False
    assert result.failed_softly is False
    assert result.output == {}
    assert result.error_kind == ErrorKind.SCHEMA_VIOLATION


def test_run_subcall_passes_user_prompt_from_callable() -> None:
    """The user_prompt callable receives the full inputs dict."""
    captured: dict[str, Any] = {}

    def builder(inputs: dict[str, Any]) -> str:
        captured.update(inputs)
        return f"got {len(inputs)} items"

    sub = SubCall(
        name="probe", system_prompt="sys", user_prompt=builder,
        output_schema=_TRIVIAL_SCHEMA, soft_fail_default={},
    )
    runner = MagicMock()
    runner.call.return_value = _ok_response({})

    run_subcall(sub, {"a": 1, "b": 2}, runner=runner)

    assert captured == {"a": 1, "b": 2}
    assert "got 2 items" in runner.call.call_args.kwargs["user"]


# ---------------------------------------------------------------------------
# DecomposedJudgment.run — orchestration
# ---------------------------------------------------------------------------


def test_decomposed_run_happy_path_passes_subcall_outputs_to_main() -> None:
    """Each sub-call's structured output lands under working[sub.name],
    and the main's user_prompt sees them all."""

    sub_a = SubCall(
        name="step_a", system_prompt="sys-a",
        user_prompt=lambda w: "build-a",
        output_schema=_TRIVIAL_SCHEMA, soft_fail_default={},
    )
    sub_b = SubCall(
        name="step_b", system_prompt="sys-b",
        user_prompt=lambda w: f"build-b sees step_a={w.get('step_a')}",
        output_schema=_TRIVIAL_SCHEMA, soft_fail_default={},
    )

    seen_in_main: dict[str, Any] = {}

    def main_user(working: dict[str, Any]) -> str:
        seen_in_main.update(working)
        return "main-prompt"

    main = MainCall(
        name="verdict", system_prompt="sys-main",
        user_prompt=main_user, output_schema=_TRIVIAL_SCHEMA,
    )

    chain = DecomposedJudgment(
        name="my_chain", sub_calls=(sub_a, sub_b), main=main,
    )

    runner = MagicMock()
    runner.call.side_effect = [
        _ok_response({"a_out": 1}, tier="local_fast"),       # sub_a
        _ok_response({"b_out": 2}, tier="local_fast"),       # sub_b
        _ok_response({"verdict": "ok"}, tier="frontier_balanced"),  # main
    ]

    result = chain.run(
        {"original_input": "value"}, trace_id="t-1", runner=runner,
    )

    assert result.is_error() is False
    assert result.exhausted_step is None
    assert result.main is not None
    assert result.main.structured_output == {"verdict": "ok"}

    # working visible to main contains: original input + both sub outputs.
    assert seen_in_main["original_input"] == "value"
    assert seen_in_main["step_a"] == {"a_out": 1}
    assert seen_in_main["step_b"] == {"b_out": 2}

    # Audits captured for both sub-calls.
    assert set(result.sub_audits.keys()) == {"step_a", "step_b"}
    assert result.sub_audits["step_a"].failed_softly is False
    assert result.sub_audits["step_b"].failed_softly is False

    # Trace ID propagation: each call got "<chain>::<step>::<caller>".
    actual_trace_ids = [
        c.kwargs["trace_id"] for c in runner.call.call_args_list
    ]
    assert actual_trace_ids == [
        "my_chain::step_a::t-1",
        "my_chain::step_b::t-1",
        "my_chain::verdict::t-1",
    ]


def test_decomposed_run_soft_fail_substitutes_default_and_continues() -> None:
    """A soft-failed sub-call's default lands in working; main still runs."""

    sub = SubCall(
        name="hints", system_prompt="sys", user_prompt=lambda w: "u",
        output_schema=_TRIVIAL_SCHEMA, fail_policy="soft",
        soft_fail_default={"has_hint": False, "fallback": True},
    )
    main = _make_maincall()
    chain = DecomposedJudgment(
        name="c", sub_calls=(sub,), main=main,
    )

    runner = MagicMock()
    runner.call.side_effect = [
        _err_response(kind=ErrorKind.TIMEOUT),                   # sub fails
        _ok_response({"verdict": "ok"}, tier="frontier_fast"),   # main runs
    ]

    result = chain.run({}, runner=runner)

    assert result.is_error() is False
    assert result.exhausted_step is None
    assert result.sub_audits["hints"].failed_softly is True
    assert result.sub_audits["hints"].output == {"has_hint": False, "fallback": True}
    assert result.main.structured_output == {"verdict": "ok"}


def test_decomposed_run_hard_fail_short_circuits() -> None:
    """A hard-failed sub-call halts the chain; main is skipped."""
    sub = _make_subcall(name="strict", fail_policy="hard")
    main = _make_maincall()
    chain = DecomposedJudgment(
        name="c", sub_calls=(sub,), main=main,
    )

    runner = MagicMock()
    runner.call.side_effect = [_err_response(kind=ErrorKind.SCHEMA_VIOLATION)]

    result = chain.run({}, runner=runner)

    assert result.is_error() is True
    assert result.exhausted_step == "strict"
    assert result.main is None
    # Runner was called exactly once — main was never invoked.
    assert runner.call.call_count == 1


def test_decomposed_run_default_trace_id_uses_dash() -> None:
    """No trace_id passed: each step's id is '<chain>::<step>::-'."""
    sub = _make_subcall()
    chain = DecomposedJudgment(
        name="c", sub_calls=(sub,), main=_make_maincall(),
    )
    runner = MagicMock()
    runner.call.side_effect = [
        _ok_response({}), _ok_response({}),
    ]

    chain.run({}, runner=runner)

    trace_ids = [c.kwargs["trace_id"] for c in runner.call.call_args_list]
    assert all("::-" in t for t in trace_ids)


# ---------------------------------------------------------------------------
# Config-driven dial resolution
# ---------------------------------------------------------------------------


def test_walk_dotted_returns_nested_dict() -> None:
    cfg = {"a": {"b": {"c": {"x": 1}}}}
    assert _walk_dotted(cfg, "a.b.c") == {"x": 1}


def test_walk_dotted_missing_returns_none() -> None:
    assert _walk_dotted({"a": 1}, "a.b") is None
    assert _walk_dotted({}, "x") is None
    assert _walk_dotted({"a": {"b": "leaf"}}, "a.b.c") is None


def test_resolve_dials_no_config_key_uses_fallbacks() -> None:
    dials = _resolve_dials(None)
    assert dials["tier_chain"] == [
        ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST,
    ]
    assert dials["max_tokens"] == 1024
    assert dials["temperature"] == 0.0
    assert dials["cache_ttl_minutes"] == 0


def test_resolve_dials_unknown_tier_dropped(monkeypatch) -> None:
    """Non-triage namespace: unknown tier names are dropped silently."""
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"my_namespace": {"x": {
            "tier_chain": ["local_fast", "made_up", "frontier_fast"],
            "max_tokens": 99,
        }}},
    )
    dials = _resolve_dials("my_namespace.x")
    assert dials["tier_chain"] == [ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST]
    assert dials["max_tokens"] == 99


def test_resolve_dials_uses_config_block(monkeypatch) -> None:
    """Non-triage namespace: dials come from the resolved config block."""
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"my_namespace": {"my_step": {
            "tier_chain": ["frontier_balanced", "frontier_best"],
            "max_tokens": 4096,
            "temperature": 0.7,
            "cache_ttl_minutes": 60,
        }}},
    )
    dials = _resolve_dials("my_namespace.my_step")
    assert dials["tier_chain"] == [ModelTier.FRONTIER_BALANCED, ModelTier.FRONTIER_BEST]
    assert dials["max_tokens"] == 4096
    assert dials["temperature"] == 0.7
    assert dials["cache_ttl_minutes"] == 60


def test_resolve_dials_load_config_failure_falls_back(monkeypatch) -> None:
    """When the loader raises, fall back rather than crash the call."""
    def boom() -> dict:
        raise RuntimeError("config unreadable")
    monkeypatch.setattr("work_buddy.llm.decomposed.load_config", boom)

    dials = _resolve_dials("my_namespace.x")
    # Falls back silently rather than crashing the LLM call.
    assert dials["tier_chain"] == [
        ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST,
    ]


def test_run_subcall_passes_resolved_dials_to_runner_call(monkeypatch) -> None:
    """End-to-end: config_key resolves, dials reach LLMRunner.call kwargs.

    Uses a non-triage config_key so we can monkey-patch raw load_config
    rather than the triage-loader path. (Triage paths go through
    load_triage_config; that's covered by the dedicated regression test
    below.)
    """
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"my_namespace": {"my_step": {
            "tier_chain": ["local_fast", "frontier_fast"],
            "max_tokens": 256,
            "temperature": 0.3,
            "cache_ttl_minutes": 0,
        }}},
    )
    sub = _make_subcall(config_key="my_namespace.my_step")
    runner = MagicMock()
    runner.call.return_value = _ok_response({"v": 1})

    run_subcall(sub, {}, runner=runner)

    kwargs = runner.call.call_args.kwargs
    assert kwargs["tier"] == ModelTier.LOCAL_FAST
    assert kwargs["escalate_to"] == [ModelTier.FRONTIER_FAST]
    assert kwargs["max_tokens"] == 256
    assert kwargs["temperature"] == 0.3
    assert kwargs["cache_ttl_minutes"] == 0


def test_call_one_rejects_empty_tier_chain(monkeypatch) -> None:
    """Misconfigured empty tier_chain is a loud error, not a silent no-op.

    Uses a non-triage config_key so this exercises the raw load_config
    branch; triage paths have separate coverage.
    """
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"my_namespace": {"broken": {"tier_chain": []}}},
    )
    sub = _make_subcall(config_key="my_namespace.broken")
    runner = MagicMock()

    with pytest.raises(ValueError, match="empty"):
        run_subcall(sub, {}, runner=runner)


# ---------------------------------------------------------------------------
# Trace-id composition
# ---------------------------------------------------------------------------


def test_compose_trace_id_format() -> None:
    assert _compose_trace_id("chain", "step", "caller") == "chain::step::caller"
    assert _compose_trace_id("chain", "step", None) == "chain::step::-"


# ---------------------------------------------------------------------------
# Regression: triage.* keys must route through load_triage_config so that
# TRIAGE_DEFAULTS in-code defaults are honored. (Discovered via live test:
# the framework was silently bypassing the in-code defaults and falling
# back to its own hardcoded chain when config.yaml had no override.)
# ---------------------------------------------------------------------------


def test_resolve_dials_triage_key_uses_triage_defaults(monkeypatch) -> None:
    """``triage.<key>`` must read from load_triage_config() so
    TRIAGE_DEFAULTS values apply even when YAML has no override."""
    from work_buddy.clarify import config as triage_cfg

    # Pretend the user's YAML has no triage block at all. The loader
    # should still return the in-code TRIAGE_DEFAULTS values via the
    # deep-merge in load_triage_config.
    monkeypatch.setattr(
        "work_buddy.config.load_config", lambda: {},
    )

    dials = _resolve_dials("triage.deadline_extract")

    # The shipped TRIAGE_DEFAULTS["deadline_extract"]["tier_chain"] starts
    # with local_tool_calling. If the framework bypassed load_triage_config
    # and fell back to _FALLBACK_TIER_CHAIN, this would not be present.
    assert dials["tier_chain"][0] == ModelTier.LOCAL_TOOL_CALLING
    # And the rest of the shipped chain is preserved.
    assert ModelTier.LOCAL_FAST in dials["tier_chain"]
    assert ModelTier.FRONTIER_FAST in dials["tier_chain"]


def test_resolve_dials_non_triage_key_uses_load_config(monkeypatch) -> None:
    """Non-triage keys go through raw load_config(), unchanged."""
    monkeypatch.setattr(
        "work_buddy.llm.decomposed.load_config",
        lambda: {"some_other_subsystem": {"my_step": {
            "tier_chain": ["frontier_balanced"], "max_tokens": 9999,
        }}},
    )
    dials = _resolve_dials("some_other_subsystem.my_step")
    assert dials["tier_chain"] == [ModelTier.FRONTIER_BALANCED]
    assert dials["max_tokens"] == 9999


# ---------------------------------------------------------------------------
# Integration-style: tier_attempts flatten into DecomposedResult
# ---------------------------------------------------------------------------


def test_decomposed_result_collects_all_tier_attempts() -> None:
    sub = _make_subcall()
    chain = DecomposedJudgment(name="c", sub_calls=(sub,), main=_make_maincall())
    runner = MagicMock()
    runner.call.side_effect = [
        _ok_response({}, tier="local_fast"),
        _ok_response({}, tier="frontier_balanced"),
    ]
    result = chain.run({}, runner=runner)

    # One attempt per call, two calls = two attempts.
    assert len(result.tier_attempts) == 2
    assert {a.tier for a in result.tier_attempts} == {"local_fast", "frontier_balanced"}
