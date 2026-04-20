"""Unit tests for the background-triage producer, pool, and capabilities.

Covers:
- TriagePool: register → submit → pending → mark_reviewed
- triage_submit capability: unknown run / bad item / duplicate / valid
- BackgroundTriageProducer: empty, unchanged-hash skip, submit success,
  unsubmitted-run accounting
- repair_segmentation: error categorization + available-id extraction
- journal adapter: empty/no-notes, graceful failure when segmenter fails
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from work_buddy.triage import background as bg
from work_buddy.triage.background import (
    BackgroundTriageProducer,
    TriagePool,
    content_hash,
)
from work_buddy.triage.capabilities.triage_submit import triage_submit
from work_buddy.triage.items import TriageItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_pool(tmp_path: Path, monkeypatch) -> TriagePool:
    """A pool that writes to a throwaway directory."""
    pool = TriagePool(pool_dir=tmp_path / "triage_pool")
    # Some callables (triage_submit) fetch the module singleton — override it.
    bg.set_pool_for_tests(pool)
    # Don't let snapshot-to-artifact calls poison tmp with network/disk weirdness
    monkeypatch.setattr(
        "work_buddy.artifacts.save",
        lambda *a, **kw: type("R", (), {"id": "stub"})(),
    )
    yield pool
    bg.set_pool_for_tests(None)


def _item(i: int = 0, text: str = "hello") -> TriageItem:
    return TriageItem(
        id=f"journal_t_{i:06x}",
        text=text,
        label=text[:20],
        source="journal_thread",
        metadata={},
    )


# ---------------------------------------------------------------------------
# TriagePool
# ---------------------------------------------------------------------------


def test_pool_register_and_submit(isolated_pool: TriagePool) -> None:
    items = [_item(1, "a"), _item(2, "b")]
    isolated_pool.register_run(
        run_id="r1", adapter="journal_hourly",
        source="journal_thread", items=items,
    )
    result = isolated_pool.submit(
        run_id="r1",
        item_id=items[0].id,
        verdict={
            "recommended_action": "leave",
            "rationale": "Nothing actionable.",
        },
    )
    assert result["status"] == "ok"
    assert len(isolated_pool.pending()) == 1


def test_pool_rejects_unknown_run(isolated_pool: TriagePool) -> None:
    result = isolated_pool.submit(
        run_id="ghost",
        item_id="journal_t_000000",
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    assert result["status"] == "error"
    assert "Unknown run_id" in result["error"]


def test_pool_rejects_wrong_item(isolated_pool: TriagePool) -> None:
    items = [_item(1)]
    isolated_pool.register_run(
        run_id="r2", adapter="a", source="s", items=items,
    )
    result = isolated_pool.submit(
        run_id="r2",
        item_id="journal_t_ffffff",
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    assert result["status"] == "error"
    assert "does not belong" in result["error"]


def test_pool_rejects_duplicate(isolated_pool: TriagePool) -> None:
    items = [_item(1)]
    isolated_pool.register_run(
        run_id="r3", adapter="a", source="s", items=items,
    )
    v = {"recommended_action": "leave", "rationale": "x"}
    assert isolated_pool.submit(
        run_id="r3", item_id=items[0].id, verdict=v,
    )["status"] == "ok"
    second = isolated_pool.submit(
        run_id="r3", item_id=items[0].id, verdict=v,
    )
    assert second["status"] == "error"
    assert "Duplicate" in second["error"]


def test_pool_rejects_invalid_action(isolated_pool: TriagePool) -> None:
    items = [_item(1)]
    isolated_pool.register_run(
        run_id="r4", adapter="a", source="s", items=items,
    )
    result = isolated_pool.submit(
        run_id="r4",
        item_id=items[0].id,
        verdict={"recommended_action": "nuke", "rationale": "bad"},
    )
    assert result["status"] == "error"


def test_pool_mark_reviewed(isolated_pool: TriagePool) -> None:
    items = [_item(1), _item(2)]
    isolated_pool.register_run(
        run_id="r5", adapter="a", source="s", items=items,
    )
    for it in items:
        isolated_pool.submit(
            run_id="r5",
            item_id=it.id,
            verdict={"recommended_action": "leave", "rationale": "x"},
        )
    assert len(isolated_pool.pending()) == 2
    stamped = isolated_pool.mark_reviewed(
        [("r5", items[0].id)], outcome="approved",
    )
    assert stamped == 1
    assert len(isolated_pool.pending()) == 1


# ---------------------------------------------------------------------------
# triage_submit capability
# ---------------------------------------------------------------------------


def test_triage_submit_accepts_group_intent(
    isolated_pool: TriagePool,
) -> None:
    """`group_intent` is an optional agent-supplied noun-phrase that
    labels the card title in the review UI. Must land in the pool's
    verdict unchanged (up to the 160-char truncation). Must be
    allowed through ``_shape_verdict``."""
    items = [_item(9)]
    isolated_pool.register_run(
        run_id="gi_run", adapter="a", source="s", items=items,
    )
    r = triage_submit(
        run_id="gi_run",
        item_id=items[0].id,
        recommended_action="leave",
        rationale="Nothing to do.",
        group_intent="ETF weekly tracking habit",
    )
    assert r["status"] == "ok"
    pending = isolated_pool.pending()
    assert len(pending) == 1
    assert pending[0].verdict["group_intent"] == "ETF weekly tracking habit"


def test_triage_submit_group_intent_truncated(
    isolated_pool: TriagePool,
) -> None:
    items = [_item(10)]
    isolated_pool.register_run(
        run_id="gi_trunc", adapter="a", source="s", items=items,
    )
    long = "x" * 500
    r = triage_submit(
        run_id="gi_trunc",
        item_id=items[0].id,
        recommended_action="leave",
        rationale="r",
        group_intent=long,
    )
    assert r["status"] == "ok"
    stored = isolated_pool.pending()[0].verdict["group_intent"]
    assert len(stored) == 160


def test_triage_submit_capability_happy_path(
    isolated_pool: TriagePool,
) -> None:
    items = [_item(42)]
    isolated_pool.register_run(
        run_id="cap_run", adapter="a", source="s", items=items,
    )
    result = triage_submit(
        run_id="cap_run",
        item_id=items[0].id,
        recommended_action="create_task",
        rationale="Clear actionable item.",
        confidence=0.75,
        suggested_task_text="Do the thing.",
    )
    assert result["status"] == "ok"
    pending = isolated_pool.pending()
    assert len(pending) == 1
    assert pending[0].verdict["confidence"] == 0.75
    assert pending[0].verdict["suggested_task_text"] == "Do the thing."


def test_triage_submit_validates_confidence_range(
    isolated_pool: TriagePool,
) -> None:
    items = [_item(1)]
    isolated_pool.register_run(
        run_id="conf_run", adapter="a", source="s", items=items,
    )
    r = triage_submit(
        run_id="conf_run",
        item_id=items[0].id,
        recommended_action="leave",
        rationale="x",
        confidence=2.0,
    )
    assert r["status"] == "error"
    assert "between 0 and 1" in r["error"]


def test_triage_submit_structured_error_on_unknown_run(
    isolated_pool: TriagePool,
) -> None:
    r = triage_submit(
        run_id="not_a_run",
        item_id="journal_t_000000",
        recommended_action="leave",
        rationale="x",
    )
    assert r["status"] == "error"
    assert "Unknown run_id" in r["error"]


# ---------------------------------------------------------------------------
# BackgroundTriageProducer
# ---------------------------------------------------------------------------


def test_producer_empty_is_skipped(isolated_pool: TriagePool) -> None:
    producer = BackgroundTriageProducer(
        adapter_name="t_adapter",
        source="journal_thread",
        collect=lambda: ([], None),
        agent=lambda item, run_id: {"content": ""},
        enrich=False,
    )
    result = producer.run()
    assert result.status == "skipped"
    assert result.reason == "no_items"


def test_producer_unchanged_hash_skipped(isolated_pool: TriagePool) -> None:
    items = [_item(1)]

    # Real producer submits on first call
    def submit_once_agent(item: TriageItem, run_id: str) -> dict[str, Any]:
        triage_submit(
            run_id=run_id,
            item_id=item.id,
            recommended_action="leave",
            rationale="x",
        )
        return {"content": "done"}

    producer = BackgroundTriageProducer(
        adapter_name="hash_adapter",
        source="journal_thread",
        collect=lambda: (items, "HASH1"),
        agent=submit_once_agent,
        enrich=False,
    )
    first = producer.run()
    assert first.status == "ok"
    assert first.submitted == 1

    # Second call with same hash → skipped before registering a new run
    second = producer.run()
    assert second.status == "skipped"
    assert second.reason == "unchanged"


def test_producer_counts_unsubmitted_runs(
    isolated_pool: TriagePool,
) -> None:
    items = [_item(1), _item(2)]

    # Agent that submits for item 1 only
    def selective_agent(item: TriageItem, run_id: str) -> dict[str, Any]:
        if item.id.endswith("000001"):
            triage_submit(
                run_id=run_id,
                item_id=item.id,
                recommended_action="leave",
                rationale="x",
            )
        return {"content": ""}

    producer = BackgroundTriageProducer(
        adapter_name="mixed_adapter",
        source="journal_thread",
        collect=lambda: (items, "HMIX"),
        agent=selective_agent,
        enrich=False,
    )
    result = producer.run()
    assert result.status == "ok"
    assert result.submitted == 1
    assert len(result.unsubmitted) == 1
    assert result.unsubmitted[0] == items[1].id


def test_producer_force_bypasses_hash(isolated_pool: TriagePool) -> None:
    items = [_item(1)]

    def agent(item: TriageItem, run_id: str) -> dict[str, Any]:
        triage_submit(
            run_id=run_id, item_id=item.id,
            recommended_action="leave", rationale="x",
        )
        return {}

    producer = BackgroundTriageProducer(
        adapter_name="force_adapter",
        source="journal_thread",
        collect=lambda: (items, "HSTAT"),
        agent=agent,
        enrich=False,
    )
    first = producer.run()
    assert first.status == "ok"
    skipped = producer.run()
    assert skipped.status == "skipped"
    forced = producer.run(force=True)
    assert forced.status == "ok"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def test_producer_dedups_items_already_pending_in_pool(
    isolated_pool: TriagePool,
) -> None:
    """Two producer runs over the same content should not stack two
    pool entries for the same item. The second run's items get
    filtered by matching content hash against the existing pending
    entries."""
    item_a = _item(100, text="check ETFs weekly")
    item_b = _item(101, text="migrate embedding service")

    def submit_both(item: TriageItem, run_id: str) -> dict[str, Any]:
        triage_submit(
            run_id=run_id, item_id=item.id,
            recommended_action="leave", rationale="x",
        )
        return {}

    # First run: both items land in the pool.
    prod = BackgroundTriageProducer(
        adapter_name="dedup_test",
        source="journal_thread",
        collect=lambda: ([item_a, item_b], "H1"),
        agent=submit_both,
        enrich=False,
    )
    r1 = prod.run()
    assert r1.status == "ok"
    assert r1.submitted == 2
    assert len(isolated_pool.pending()) == 2

    # Second run with SAME content but fresh hash (simulates the
    # producer being told "content changed somewhere"): items should
    # be dedup'd since their content hashes match pending entries.
    # New item IDs (random UUIDs in the adapter), same text.
    item_a_dup = _item(200, text="check ETFs weekly")
    item_b_dup = _item(201, text="migrate embedding service")
    prod2 = BackgroundTriageProducer(
        adapter_name="dedup_test",
        source="journal_thread",
        collect=lambda: ([item_a_dup, item_b_dup], "H2"),
        agent=submit_both,
        enrich=False,
    )
    r2 = prod2.run()
    assert r2.status == "skipped"
    assert r2.reason == "all_items_already_pending"
    # Still only 2 pool entries, not 4.
    assert len(isolated_pool.pending()) == 2


def test_producer_runs_novel_items_alongside_already_pending(
    isolated_pool: TriagePool,
) -> None:
    """If some items are new and others are already pending, only
    the new ones get processed."""
    old_item = _item(300, text="existing ETF idea")

    def submit(item: TriageItem, run_id: str) -> dict[str, Any]:
        triage_submit(
            run_id=run_id, item_id=item.id,
            recommended_action="leave", rationale="x",
        )
        return {}

    BackgroundTriageProducer(
        adapter_name="mix_test",
        source="journal_thread",
        collect=lambda: ([old_item], "Hold"),
        agent=submit,
        enrich=False,
    ).run()
    assert len(isolated_pool.pending()) == 1

    new_item = _item(301, text="brand new topic")
    # Old item reappears (same text, different id) alongside a new one.
    old_again = _item(302, text="existing ETF idea")
    r = BackgroundTriageProducer(
        adapter_name="mix_test",
        source="journal_thread",
        collect=lambda: ([old_again, new_item], "Hmix"),
        agent=submit,
        enrich=False,
    ).run()
    assert r.status == "ok"
    # Only the new item made it through.
    assert r.submitted == 1
    assert len(isolated_pool.pending()) == 2


def test_content_hash_stable_and_order_sensitive() -> None:
    a = content_hash(["alpha", "beta"])
    b = content_hash(["alpha", "beta"])
    c = content_hash(["beta", "alpha"])
    assert a == b
    assert a != c
