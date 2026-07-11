"""Activation, health, and backfill coverage for session summaries."""

from __future__ import annotations


def test_activation_is_on_by_default(monkeypatch):
    from work_buddy.summarization import policy

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted", lambda _component: None,
    )
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    assert policy.summaries_active() is True


def test_activation_preference_is_authoritative(monkeypatch):
    from work_buddy.summarization import policy

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted", lambda _component: False,
    )
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"conversation_observability": {"summaries": {
            "use_incremental": True,
        }}},
    )
    assert policy.summaries_active() is False

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted", lambda _component: True,
    )
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"conversation_observability": {"summaries": {
            "use_incremental": False,
        }}},
    )
    assert policy.summaries_active() is True


def test_activation_honors_explicit_legacy_flag_when_undecided(monkeypatch):
    from work_buddy.summarization import policy

    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted", lambda _component: None,
    )
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"conversation_observability": {"summaries": {
            "use_incremental": False,
        }}},
    )
    assert policy.summaries_active() is False


def test_component_and_topology_are_registered():
    from work_buddy.control.graph_static import SUBSYSTEMS
    from work_buddy.health.components import COMPONENT_CATALOG
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    component = COMPONENT_CATALOG["conversation_summaries"]
    assert component.is_core is False
    assert component.health_source == "custom"
    assert component.requirements == [
        "services/conversation-summaries/llm-backend",
    ]
    node = next(
        item for item in SUBSYSTEMS
        if item["id"] == "subsystem:session-summaries"
    )
    assert node["component_deps"] == ["conversation_summaries"]
    assert all(req in REQUIREMENT_REGISTRY for req in node["requirement_ids"])


def test_backfill_observes_then_reconciles(monkeypatch):
    from work_buddy.mcp_server.ops import conversation_observability_ops as ops

    calls = {}
    monkeypatch.setattr(
        "work_buddy.summarization.policy.summaries_active", lambda: True,
    )

    def _refresh(**kwargs):
        calls["refresh"] = kwargs
        return {"observed": 12}

    monkeypatch.setattr(
        "work_buddy.conversation_observability.sessions.refresh_observed_sessions",
        _refresh,
    )
    monkeypatch.setattr(
        "work_buddy.summarization.worker.enqueue_missing",
        lambda **_kwargs: {
            "candidates": 12, "enqueued": 9, "queue_depth": 9,
        },
    )

    result = ops.summarization_backfill(days=365, max_sessions=50)
    assert calls["refresh"] == {
        "days": 365, "stale_only": True, "max_sessions": 50,
    }
    assert result["active"] is True
    assert result["enqueued"] == 9


def test_backfill_respects_opt_out(monkeypatch):
    from work_buddy.mcp_server.ops import conversation_observability_ops as ops

    monkeypatch.setattr(
        "work_buddy.summarization.policy.summaries_active", lambda: False,
    )
    result = ops.summarization_backfill()
    assert result["active"] is False
    assert result["enqueued"] == 0


def test_primary_model_tier_sets_default_budget(monkeypatch):
    from work_buddy.llm.tiers import ModelTier
    from work_buddy.summarization import incremental

    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    monkeypatch.setattr(
        "work_buddy.summarization.orchestrator._resolve_model_chain",
        lambda: [ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST],
    )
    assert incremental._resolve_per_call_budget() == 8_000

    monkeypatch.setattr(
        "work_buddy.summarization.orchestrator._resolve_model_chain",
        lambda: [ModelTier.FRONTIER_FAST],
    )
    assert incremental._resolve_per_call_budget() == 32_000


def test_local_backend_plausibility_requires_model_and_url(monkeypatch):
    from types import SimpleNamespace

    from work_buddy.llm.tiers import ModelTier
    from work_buddy.summarization import orchestrator

    monkeypatch.setattr(
        orchestrator, "_resolve_model_chain", lambda: [ModelTier.LOCAL_FAST],
    )
    monkeypatch.setattr(
        "work_buddy.llm.tiers.resolve_tier",
        lambda _tier: SimpleNamespace(
            backend="openai_compat", profile="local_general",
        ),
    )
    monkeypatch.setattr(
        "work_buddy.llm.profiles.resolve_profile",
        lambda _name: {"base_url": "", "model": "qwen"},
    )
    assert orchestrator.chain_has_plausible_backend() is False

    monkeypatch.setattr(
        "work_buddy.llm.profiles.resolve_profile",
        lambda _name: {"base_url": "http://localhost:1234/v1", "model": "qwen"},
    )
    assert orchestrator.chain_has_plausible_backend() is True
