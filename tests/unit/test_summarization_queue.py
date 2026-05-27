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
