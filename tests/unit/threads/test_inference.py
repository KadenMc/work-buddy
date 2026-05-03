"""v5 Stage 2.3 — unified Inference class.

Pins:
- One entry point, parameterized by target.
- Targets registered: intent, context, action.
- Stub runner returns deterministic empty proposal (zero confidence).
- Pluggable runner via set_llm_runner / per-call override.
- Records *_inferred event with full provenance when record_event=True.
- Unknown targets raise.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import inference, store
from work_buddy.threads.enums import InferenceTarget, ReasoningTier
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_CONTEXT_INFERRED,
    KIND_INTENT_INFERRED,
)
from work_buddy.threads.models import Proposal, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    # Reset the module-global runner between tests
    inference.set_llm_runner(inference._stub_runner)
    yield db
    inference.set_llm_runner(inference._stub_runner)


# ---------------------------------------------------------------------------
# Target registry
# ---------------------------------------------------------------------------


class TestTargetRegistry:
    def test_three_default_targets(self):
        for t in (InferenceTarget.INTENT, InferenceTarget.CONTEXT,
                  InferenceTarget.ACTION):
            spec = inference.TARGETS[t]
            assert spec.target == t
            assert spec.event_kind  # non-empty
            assert spec.prompt_template
            assert spec.output_schema

    def test_intent_target_event_kind(self):
        assert inference.TARGETS[InferenceTarget.INTENT].event_kind == KIND_INTENT_INFERRED

    def test_context_target_event_kind(self):
        assert inference.TARGETS[InferenceTarget.CONTEXT].event_kind == KIND_CONTEXT_INFERRED

    def test_action_target_event_kind(self):
        assert inference.TARGETS[InferenceTarget.ACTION].event_kind == KIND_ACTION_INFERRED

    def test_register_target_overrides(self):
        from work_buddy.threads.events import KIND_INTENT_INFERRED as K
        original = inference.TARGETS[InferenceTarget.INTENT]
        try:
            new = inference.TargetSpec(
                target=InferenceTarget.INTENT,
                event_kind=K,
                default_tier=ReasoningTier.FRONTIER_BEST,
                prompt_template="custom",
                output_schema={},
            )
            inference.register_target(new)
            assert inference.TARGETS[InferenceTarget.INTENT].prompt_template == "custom"
        finally:
            inference.register_target(original)

    def test_all_object_schemas_have_additional_properties_false(self):
        """REGRESSION (2026-05-03): Anthropic's structured-output
        validator rejects every ``"type": "object"`` schema that
        doesn't EXPLICITLY set ``"additionalProperties": false``.
        It checks recursively, so nested object types in arrays
        (e.g. ``items``) and properties also need it.

        Without this, the LLM call returns HTTP 400 BadRequestError
        and the inference adapter's exception handler silently
        returns an empty payload — making it look like the agent
        had nothing to say. Walks every TargetSpec's output_schema
        and asserts every object schema is correctly tagged.
        """
        def walk(node, path):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False, (
                        f"Object schema at {path} missing "
                        f"`additionalProperties: false` — "
                        f"Anthropic will reject it. Schema: {node}"
                    )
                for k, v in node.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, f"{path}[{i}]")

        for target, spec in inference.TARGETS.items():
            walk(spec.output_schema, target.value)


# ---------------------------------------------------------------------------
# Pluggable LLM runner
# ---------------------------------------------------------------------------


class TestRunner:
    def test_default_is_stub(self):
        assert inference.get_llm_runner() is inference._stub_runner

    def test_set_llm_runner_replaces(self):
        def my_runner(prompt, schema, tier, thread):
            return {
                "payload": {"intent": "x"}, "confidence": 0.9,
                "model": "test-model", "cost_usd": 0.01,
                "trace_pointer": None,
            }
        inference.set_llm_runner(my_runner)
        assert inference.get_llm_runner() is my_runner

    def test_per_call_runner_override_takes_precedence(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        def stub_a(prompt, schema, tier, thread):
            return {"payload": {"v": "a"}, "confidence": 0.1, "model": "a",
                    "cost_usd": 0.0, "trace_pointer": None}

        def stub_b(prompt, schema, tier, thread):
            return {"payload": {"v": "b"}, "confidence": 0.99, "model": "b",
                    "cost_usd": 0.0, "trace_pointer": None}

        inference.set_llm_runner(stub_a)
        result = inference.run(t, InferenceTarget.INTENT, runner=stub_b)
        assert result.payload == {"v": "b"}
        assert result.model_used == "b"


# ---------------------------------------------------------------------------
# Run() output
# ---------------------------------------------------------------------------


class TestRun:
    def test_unknown_target_raises(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        infer = inference.Inference()
        # AUTONOMY isn't registered by default
        with pytest.raises(inference.UnknownTarget):
            infer.run(t, InferenceTarget.AUTONOMY)

    def test_returns_proposal_with_provenance(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        def runner(prompt, schema, tier, thread):
            assert tier == ReasoningTier.FRONTIER_FAST  # intent default
            return {
                "payload": {"intent": "schedule a call"},
                "confidence": 0.85,
                "model": "test-model",
                "cost_usd": 0.002,
                "trace_pointer": "trace-id-123",
            }
        inference.set_llm_runner(runner)
        result = inference.run(t, InferenceTarget.INTENT)

        assert isinstance(result, Proposal)
        assert result.target == "intent"
        assert result.payload == {"intent": "schedule a call"}
        assert result.confidence == 0.85
        assert result.tier_used == ReasoningTier.FRONTIER_FAST
        assert result.model_used == "test-model"
        assert result.cost_usd == 0.002
        assert result.reasoning_trace_pointer == "trace-id-123"

    def test_explicit_tier_override(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        seen: list = []

        def runner(prompt, schema, tier, thread):
            seen.append(tier)
            return {"payload": {}, "confidence": 0.0, "model": None,
                    "cost_usd": 0.0, "trace_pointer": None}

        inference.set_llm_runner(runner)
        inference.run(t, InferenceTarget.INTENT,
                      tier=ReasoningTier.FRONTIER_BEST)
        assert seen == [ReasoningTier.FRONTIER_BEST]

    def test_records_event_by_default(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        def runner(prompt, schema, tier, thread):
            return {"payload": {"intent": "x"}, "confidence": 0.5,
                    "model": "m", "cost_usd": 0.001, "trace_pointer": None}

        inference.set_llm_runner(runner)
        inference.run(t, InferenceTarget.INTENT)

        events = store.list_events(t.thread_id)
        assert len(events) == 1
        assert events[0].kind == KIND_INTENT_INFERRED
        assert events[0].inference_tier == "frontier_fast"
        assert events[0].data["payload"] == {"intent": "x"}
        assert events[0].data["confidence"] == 0.5
        assert events[0].data["cost_usd"] == 0.001

    def test_record_event_false_skips_persistence(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        inference.run(t, InferenceTarget.INTENT, record_event=False)
        events = store.list_events(t.thread_id)
        assert events == []

    def test_stub_runner_returns_zero_confidence(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        # No runner registered → uses stub
        result = inference.run(t, InferenceTarget.INTENT)
        assert result.confidence == 0.0
        assert result.payload == {}

    def test_action_target_uses_balanced_default_tier(self, fresh_db):
        t = Thread()
        store.insert_thread(t)

        seen: list = []

        def runner(prompt, schema, tier, thread):
            seen.append(tier)
            return {"payload": {"kind": "standard"}, "confidence": 0.7,
                    "model": "m", "cost_usd": 0.01, "trace_pointer": None}

        inference.set_llm_runner(runner)
        inference.run(t, InferenceTarget.ACTION)
        assert seen == [ReasoningTier.FRONTIER_BALANCED]


# ---------------------------------------------------------------------------
# Per-instance overrides (Inference class)
# ---------------------------------------------------------------------------


class TestInstanceOverrides:
    def test_instance_override_does_not_mutate_global(self, fresh_db):
        infer = inference.Inference(
            overrides={
                InferenceTarget.INTENT: inference.TargetSpec(
                    target=InferenceTarget.INTENT,
                    event_kind=KIND_INTENT_INFERRED,
                    default_tier=ReasoningTier.FRONTIER_BEST,
                    prompt_template="instance prompt",
                    output_schema={},
                ),
            },
        )
        spec = infer.get_spec(InferenceTarget.INTENT)
        assert spec.prompt_template == "instance prompt"
        assert spec.default_tier == ReasoningTier.FRONTIER_BEST
        # Module-global is unchanged
        assert inference.TARGETS[InferenceTarget.INTENT].prompt_template != "instance prompt"
