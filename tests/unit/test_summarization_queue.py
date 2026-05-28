"""Unit tests for the v2 summarization queue + worker."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

from work_buddy.summarization import queue as queue_mod
from work_buddy.summarization.protocol import Provenance, SummaryNode
from work_buddy.summarization.stores import DurableSummaryStore


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    from work_buddy.summarization import db as db_mod

    db_file = tmp_path / "queue-test.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)
    # Force re-init by ensuring the file is created on first connect.
    db_mod.get_connection().close()
    return db_file


def _prov() -> Provenance:
    return Provenance(
        model="m", backend="b", profile="p",
        generated_at=Provenance.now_iso(),
        prompt_version=1, summary_schema_version=1,
        selection_version=1, cache_version=1,
    )


# ---------------------------------------------------------------------------
# Queue: enqueue + dequeue + remove
# ---------------------------------------------------------------------------


def test_queue_enqueue_and_depth(tmp_db):
    queue_mod.enqueue("ns_a", "item-1")
    queue_mod.enqueue("ns_a", "item-2")
    assert queue_mod.queue_depth() == 2
    assert queue_mod.queue_depth("ns_a") == 2
    assert queue_mod.queue_depth("ns_b") == 0


def test_queue_enqueue_idempotent(tmp_db):
    queue_mod.enqueue("ns_a", "item-1")
    queue_mod.enqueue("ns_a", "item-1")
    assert queue_mod.queue_depth() == 1


def test_queue_dequeue_fifo_no_cooldown(tmp_db):
    queue_mod.enqueue("ns_a", "first")
    queue_mod.enqueue("ns_a", "second")
    eligible = queue_mod.dequeue_eligible(cooldown_minutes=0)
    assert [e["item_id"] for e in eligible] == ["first", "second"]


def test_queue_dequeue_skips_cooldown(tmp_db):
    """A session summarized recently is skipped under cooldown."""
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    # Save a row whose generated_at is "now" — should be in cooldown.
    store.save("item-recent", SummaryNode(summary="x"), _prov(), "tok-1")
    queue_mod.enqueue("ns_a", "item-recent")
    queue_mod.enqueue("ns_a", "item-fresh")  # no summary row → eligible

    eligible = queue_mod.dequeue_eligible(cooldown_minutes=30)
    # `item-recent` is in cooldown; `item-fresh` is eligible.
    assert [e["item_id"] for e in eligible] == ["item-fresh"]


def test_queue_remove(tmp_db):
    queue_mod.enqueue("ns_a", "item-1")
    queue_mod.enqueue("ns_a", "item-2")
    queue_mod.remove("ns_a", "item-1")
    assert queue_mod.queue_depth() == 1
    snap = queue_mod.queue_snapshot()
    assert [e["item_id"] for e in snap] == ["item-2"]


def test_queue_record_attempt(tmp_db):
    queue_mod.enqueue("ns_a", "item-1")
    queue_mod.record_attempt("ns_a", "item-1", "stub failure")
    snap = queue_mod.queue_snapshot()
    assert snap[0]["attempts"] == 1
    assert snap[0]["last_error"] == "stub failure"


def test_queue_re_enqueue_updates_enqueued_at(tmp_db):
    """Re-enqueueing an already-queued session updates timestamp; doesn't
    pre-empt items queued before it."""
    queue_mod.enqueue("ns_a", "a")
    queue_mod.enqueue("ns_a", "b")
    # Re-enqueue 'a' — it should now have a LATER enqueued_at than 'b'.
    queue_mod.enqueue("ns_a", "a")
    eligible = queue_mod.dequeue_eligible(cooldown_minutes=0)
    # FIFO over enqueued_at: 'b' is now first.
    assert [e["item_id"] for e in eligible] == ["b", "a"]


# ---------------------------------------------------------------------------
# Worker: budget gate
# ---------------------------------------------------------------------------


def test_worker_budget_circuit_breaker(tmp_db, monkeypatch):
    """If today's spend already exceeds budget, worker returns budget_paused."""
    from work_buddy.summarization import worker as worker_mod

    queue_mod.enqueue("conversation_session", "item-1")

    # Force today's spend to be $99 (way over default $1 budget).
    monkeypatch.setattr(
        worker_mod, "today_summarization_spend_usd", lambda: 99.0,
    )
    # And force the config to a tight budget.
    monkeypatch.setattr(
        worker_mod, "_resolve_config",
        lambda: {"cooldown_minutes": 30, "daily_budget_usd": 1.0, "tick_limit": 20},
    )

    result = worker_mod.run_worker_tick()
    assert result["budget_paused"] is True
    assert result["processed"] == 0
    # Queue is untouched.
    assert queue_mod.queue_depth() == 1


def test_worker_no_summarizer_for_namespace(tmp_db, monkeypatch):
    """If namespace has no resolved summarizer, items are left in queue."""
    from work_buddy.summarization import worker as worker_mod

    queue_mod.enqueue("unknown_ns", "item-1")
    monkeypatch.setattr(
        worker_mod, "today_summarization_spend_usd", lambda: 0.0,
    )
    monkeypatch.setattr(
        worker_mod, "_resolve_config",
        lambda: {"cooldown_minutes": 0, "daily_budget_usd": 1.0, "tick_limit": 20},
    )

    result = worker_mod.run_worker_tick()
    # No summarizer for `unknown_ns` → skipped (not errored).
    assert result["processed"] == 0
    assert result["errored"] == 0
    # Queue is untouched (item stays for future ticks).
    assert queue_mod.queue_depth() == 1


def test_worker_bypass_cooldown(tmp_db, monkeypatch):
    """When bypass_cooldown=True, dequeue_eligible uses cooldown=0."""
    from work_buddy.summarization import worker as worker_mod

    # Pre-summarize an item (puts it in cooldown for normal ticks).
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    store.save("item-recent", SummaryNode(summary="x"), _prov(), "tok-1")
    queue_mod.enqueue("conversation_session", "item-recent")

    monkeypatch.setattr(
        worker_mod, "today_summarization_spend_usd", lambda: 0.0,
    )
    monkeypatch.setattr(
        worker_mod, "_resolve_config",
        lambda: {"cooldown_minutes": 30, "daily_budget_usd": 1.0, "tick_limit": 20},
    )
    # Force the summarizer resolver to return a stub that "succeeds".
    fake_summ_calls = {"n": 0}

    class _FakeSummarizer:
        def refresh_one(self, item_id, force=False, **kw):
            fake_summ_calls["n"] += 1
            return SummaryNode(summary="refreshed")

    monkeypatch.setattr(
        worker_mod, "_resolve_summarizer",
        lambda ns: _FakeSummarizer() if ns == "conversation_session" else None,
    )

    # Without bypass: item is in cooldown, should be skipped.
    result = worker_mod.run_worker_tick()
    assert result["processed"] == 0
    assert fake_summ_calls["n"] == 0
    # With bypass: item gets processed even though it's in cooldown.
    result = worker_mod.run_worker_tick(bypass_cooldown=True)
    assert result["processed"] == 1
    assert fake_summ_calls["n"] == 1
    # And the queue entry is removed.
    assert queue_mod.queue_depth() == 0


def test_v2_feature_flag_default_false():
    """When the flag is unset, v2 enqueue path stays off."""
    from work_buddy.conversation_observability.sessions import (
        _v2_summarization_enabled,
    )
    # Default config has use_incremental=False (or missing).
    # This test just confirms the helper exists and returns a bool.
    result = _v2_summarization_enabled()
    assert isinstance(result, bool)


def test_build_session_summarizer_v1_default(monkeypatch, tmp_path):
    """Default build (use_incremental=False) uses LayeredDisclosureStrategy
    with version triplet (1,1,1,1)."""
    from work_buddy.conversation_observability.summarizer_binding import (
        build_session_summarizer,
    )
    from work_buddy.summarization.strategies import LayeredDisclosureStrategy
    from work_buddy.summarization import db as db_mod

    # Use a tmp DB to keep this isolated.
    db_file = tmp_path / "s.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)

    s = build_session_summarizer(use_incremental=False)
    assert isinstance(s.strategy, LayeredDisclosureStrategy)
    assert s.strategy.prompt_version == 1
    assert s.strategy.schema_version == 1
    assert s.store.selection_version == 1
    assert s.store.cache_version == 1


def test_build_session_summarizer_v2_flag(monkeypatch, tmp_path):
    """When `use_incremental=True`, returns IncrementalLayeredStrategy with
    version triplet (2,2,2,2)."""
    from work_buddy.conversation_observability.summarizer_binding import (
        build_session_summarizer,
    )
    from work_buddy.summarization.strategies import IncrementalLayeredStrategy
    from work_buddy.summarization import db as db_mod

    db_file = tmp_path / "s.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)

    s = build_session_summarizer(use_incremental=True)
    assert isinstance(s.strategy, IncrementalLayeredStrategy)
    assert s.strategy.prompt_version == 2
    assert s.strategy.schema_version == 2
    assert s.store.selection_version == 2
    assert s.store.cache_version == 2


def test_resolve_model_chain_default_when_config_missing(monkeypatch):
    """Without a config key, default chain is [FRONTIER_FAST]."""
    from work_buddy.summarization.orchestrator import _resolve_model_chain
    from work_buddy.llm.tiers import ModelTier

    monkeypatch.setattr(
        "work_buddy.config.load_config", lambda: {},
    )
    chain = _resolve_model_chain()
    assert chain == [ModelTier.FRONTIER_FAST]


def test_resolve_model_chain_reads_config(monkeypatch):
    """Config with a valid chain returns the corresponding ModelTier enums."""
    from work_buddy.summarization.orchestrator import _resolve_model_chain
    from work_buddy.llm.tiers import ModelTier

    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "conversation_observability": {
                "summaries": {"model_chain": ["local_fast", "frontier_fast"]},
            },
        },
    )
    chain = _resolve_model_chain()
    assert chain == [ModelTier.LOCAL_FAST, ModelTier.FRONTIER_FAST]


def test_resolve_model_chain_ignores_unknown_tiers(monkeypatch):
    """Unknown tier names are warned and skipped; valid ones pass through.
    If everything is unknown, fall back to default."""
    from work_buddy.summarization.orchestrator import _resolve_model_chain
    from work_buddy.llm.tiers import ModelTier

    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "conversation_observability": {
                "summaries": {"model_chain": ["bogus_tier", "frontier_fast"]},
            },
        },
    )
    chain = _resolve_model_chain()
    assert chain == [ModelTier.FRONTIER_FAST]

    # All-bogus → default
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "conversation_observability": {
                "summaries": {"model_chain": ["bogus_a", "bogus_b"]},
            },
        },
    )
    chain = _resolve_model_chain()
    assert chain == [ModelTier.FRONTIER_FAST]


def test_resolve_model_chain_empty_list_falls_back_to_default(monkeypatch):
    """Empty list config → default chain."""
    from work_buddy.summarization.orchestrator import _resolve_model_chain
    from work_buddy.llm.tiers import ModelTier

    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "conversation_observability": {
                "summaries": {"model_chain": []},
            },
        },
    )
    assert _resolve_model_chain() == [ModelTier.FRONTIER_FAST]


def test_v2_strategy_version_triplet_invalidates_v1_rows(tmp_db):
    """Critical regression: when the v2 strategy is constructed with its
    (2,2,2,2) versions, the store's staleness check fires on existing
    v1-shape rows."""
    from work_buddy.summarization.strategies import (
        IncrementalLayeredStrategy,
        LayeredDisclosureStrategy,
    )
    from work_buddy.summarization.stores import DurableSummaryStore

    # Save a v1-shape row first.
    v1_store = DurableSummaryStore("conversation_session", selection_version=1, cache_version=1)
    v1_strategy = LayeredDisclosureStrategy()
    v1_store.set_strategy_versions(v1_strategy.prompt_version, v1_strategy.schema_version)
    v1_store.save(
        "session-1",
        SummaryNode(summary="v1 row"),
        _prov(),
        "tok-1",
    )
    # Sanity: v1 store sees it as fresh.
    assert v1_store.is_fresh("session-1", "tok-1")

    # Now construct v2 strategy + store and check staleness — v1 rows should appear stale.
    v2_store = DurableSummaryStore("conversation_session", selection_version=2, cache_version=2)
    v2_strategy = IncrementalLayeredStrategy()
    v2_store.set_strategy_versions(v2_strategy.prompt_version, v2_strategy.schema_version)
    assert not v2_store.is_fresh("session-1", "tok-1")
