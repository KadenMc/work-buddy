"""Slice 1 schema additions to PoolEntry / TriagePool: state, expires_at,
quarantine_reason, sweep helpers, raw submit, hardened normalization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.triage import background as bg
from work_buddy.triage import sources
from work_buddy.triage.background import (
    POOL_ENTRY_STATES,
    PoolEntry,
    STATE_PENDING,
    STATE_QUARANTINED,
    STATE_REVIEWED,
    STATE_STALE,
    TriagePool,
    item_content_hash,
)
from work_buddy.triage.items import TriageItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_pool(tmp_path: Path, monkeypatch) -> TriagePool:
    pool = TriagePool(pool_dir=tmp_path / "triage_pool")
    bg.set_pool_for_tests(pool)
    monkeypatch.setattr(
        "work_buddy.artifacts.save",
        lambda *a, **kw: type("R", (), {"id": "stub"})(),
    )
    # Stub vault_root so source-descriptor TTL lookups don't blow up.
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(tmp_path / "vault")},
    )
    sources.reset_for_tests()
    yield pool
    bg.set_pool_for_tests(None)
    sources.reset_for_tests()


def _item(i: int = 0, text: str = "hello", source: str = "journal_thread") -> TriageItem:
    return TriageItem(
        id=f"j_{i:06x}",
        text=text,
        label=text[:20],
        source=source,
        metadata={},
    )


# ---------------------------------------------------------------------------
# PoolEntry: state field + backwards-compat
# ---------------------------------------------------------------------------


def test_new_entry_defaults_to_pending(isolated_pool: TriagePool) -> None:
    items = [_item(1)]
    isolated_pool.register_run(
        run_id="r1", adapter="a", source="journal_thread", items=items,
    )
    isolated_pool.submit(
        run_id="r1", item_id=items[0].id,
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    pe = isolated_pool.pending()[0]
    assert pe.state == STATE_PENDING
    assert pe.state_changed_at is not None
    assert pe.quarantine_reason is None


def test_legacy_entry_without_state_inferred_from_reviewed_at() -> None:
    """Pre-Slice-1 entries had no ``state``; from_dict must infer."""
    raw_pending = {
        "run_id": "old1", "adapter": "a", "source": "journal_thread",
        "item_id": "x", "item": {}, "verdict": {}, "created_at": "...",
        # No "state" key, no "reviewed_at" key.
    }
    pe = PoolEntry.from_dict(raw_pending)
    assert pe.state == STATE_PENDING

    raw_reviewed = {
        **raw_pending,
        "reviewed_at": "2026-04-01T00:00:00+00:00",
        "review_outcome": "approved",
    }
    pe2 = PoolEntry.from_dict(raw_reviewed)
    assert pe2.state == STATE_REVIEWED


def test_known_states_set_complete() -> None:
    """All five lifecycle states present in POOL_ENTRY_STATES."""
    assert POOL_ENTRY_STATES == {
        "pending", "stale", "quarantined", "reviewed", "dropped",
    }


# ---------------------------------------------------------------------------
# expires_at from source descriptor
# ---------------------------------------------------------------------------


def test_journal_entry_gets_5d_expires_at(isolated_pool: TriagePool) -> None:
    items = [_item(2)]
    isolated_pool.register_run(
        run_id="r2", adapter="a", source="journal_thread", items=items,
    )
    isolated_pool.submit(
        run_id="r2", item_id=items[0].id,
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    pe = isolated_pool.pending()[0]
    assert pe.expires_at is not None
    created = datetime.fromisoformat(pe.created_at)
    expires = datetime.fromisoformat(pe.expires_at)
    assert (expires - created) == timedelta(days=5)


def test_inline_entry_has_no_expires_at(isolated_pool: TriagePool) -> None:
    """Inline TTL is null per Slice 1 — no auto-expiry."""
    items = [_item(3, source="inline")]
    isolated_pool.register_run(
        run_id="r3", adapter="a", source="inline", items=items,
    )
    isolated_pool.submit(
        run_id="r3", item_id=items[0].id,
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    pe = isolated_pool.pending()[0]
    assert pe.expires_at is None


def test_unknown_source_has_no_expires_at(isolated_pool: TriagePool) -> None:
    """Unknown sources skip TTL — no descriptor, no expiry."""
    items = [_item(4, source="frobnitz")]
    isolated_pool.register_run(
        run_id="r4", adapter="a", source="frobnitz", items=items,
    )
    isolated_pool.submit(
        run_id="r4", item_id=items[0].id,
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    pe = isolated_pool.pending()[0]
    assert pe.expires_at is None


# ---------------------------------------------------------------------------
# Sweep helpers: pending_count, mark_state, quarantine, mark_stale
# ---------------------------------------------------------------------------


def test_pending_count_matches_pending(isolated_pool: TriagePool) -> None:
    items = [_item(i) for i in range(5)]
    isolated_pool.register_run(
        run_id="rc", adapter="a", source="journal_thread", items=items,
    )
    for it in items:
        isolated_pool.submit(
            run_id="rc", item_id=it.id,
            verdict={"recommended_action": "leave", "rationale": "x"},
        )
    assert isolated_pool.pending_count() == 5
    assert isolated_pool.pending_count() == len(isolated_pool.pending())


def test_quarantine_transitions_state_with_reason(isolated_pool: TriagePool) -> None:
    items = [_item(10)]
    isolated_pool.register_run(
        run_id="rq", adapter="a", source="journal_thread", items=items,
    )
    isolated_pool.submit(
        run_id="rq", item_id=items[0].id,
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    n = isolated_pool.quarantine(
        [("rq", items[0].id)], reason="source_removed",
    )
    assert n == 1
    assert isolated_pool.pending_count() == 0
    quarantined = isolated_pool.entries_in_state(STATE_QUARANTINED)
    assert len(quarantined) == 1
    assert quarantined[0].quarantine_reason == "source_removed"


def test_mark_stale_transitions_state(isolated_pool: TriagePool) -> None:
    items = [_item(11)]
    isolated_pool.register_run(
        run_id="rs", adapter="a", source="journal_thread", items=items,
    )
    isolated_pool.submit(
        run_id="rs", item_id=items[0].id,
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    n = isolated_pool.mark_stale([("rs", items[0].id)])
    assert n == 1
    assert isolated_pool.pending_count() == 0
    assert len(isolated_pool.entries_in_state(STATE_STALE)) == 1


def test_mark_state_no_double_stamp(isolated_pool: TriagePool) -> None:
    """Idempotent — calling twice doesn't re-stamp state_changed_at."""
    items = [_item(12)]
    isolated_pool.register_run(
        run_id="rd", adapter="a", source="journal_thread", items=items,
    )
    isolated_pool.submit(
        run_id="rd", item_id=items[0].id,
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    isolated_pool.quarantine(
        [("rd", items[0].id)], reason="source_removed",
    )
    second = isolated_pool.quarantine(
        [("rd", items[0].id)], reason="source_removed",
    )
    assert second == 0


def test_mark_state_rejects_reviewed():
    """mark_state refuses 'reviewed' — that path goes through mark_reviewed."""
    pool = TriagePool()
    with pytest.raises(ValueError, match="mark_reviewed"):
        pool.mark_state([("a", "b")], state=STATE_REVIEWED)


def test_entries_in_state_validates(isolated_pool: TriagePool) -> None:
    with pytest.raises(ValueError, match="Unknown state"):
        isolated_pool.entries_in_state("totally-fake")


def test_mark_reviewed_also_sets_state(isolated_pool: TriagePool) -> None:
    items = [_item(13)]
    isolated_pool.register_run(
        run_id="rr", adapter="a", source="journal_thread", items=items,
    )
    isolated_pool.submit(
        run_id="rr", item_id=items[0].id,
        verdict={"recommended_action": "leave", "rationale": "x"},
    )
    isolated_pool.mark_reviewed([("rr", items[0].id)], outcome="approved")
    reviewed = isolated_pool.entries_in_state(STATE_REVIEWED)
    assert len(reviewed) == 1
    assert reviewed[0].review_outcome == "approved"
    assert reviewed[0].state_changed_at is not None


# ---------------------------------------------------------------------------
# submit_raw (verdict-pass-disabled path)
# ---------------------------------------------------------------------------


def test_submit_raw_writes_raw_verdict(isolated_pool: TriagePool) -> None:
    items = [_item(20)]
    isolated_pool.register_run(
        run_id="raw", adapter="a", source="journal_thread", items=items,
    )
    r = isolated_pool.submit_raw(run_id="raw", item_id=items[0].id)
    assert r["status"] == "ok"
    assert r["raw"] is True
    pe = isolated_pool.pending()[0]
    assert pe.verdict == {"raw": True}
    assert pe.state == STATE_PENDING


def test_submit_raw_rejects_unknown_run(isolated_pool: TriagePool) -> None:
    r = isolated_pool.submit_raw(run_id="ghost", item_id="nope")
    assert r["status"] == "error"
    assert "Unknown run_id" in r["error"]


def test_submit_raw_rejects_duplicate(isolated_pool: TriagePool) -> None:
    items = [_item(21)]
    isolated_pool.register_run(
        run_id="rdup", adapter="a", source="journal_thread", items=items,
    )
    assert isolated_pool.submit_raw(
        run_id="rdup", item_id=items[0].id,
    )["status"] == "ok"
    second = isolated_pool.submit_raw(
        run_id="rdup", item_id=items[0].id,
    )
    assert second["status"] == "error"
    assert "Duplicate" in second["error"]


# ---------------------------------------------------------------------------
# Hardened item_content_hash normalization
# ---------------------------------------------------------------------------


def test_hash_normalizes_unicode_forms() -> None:
    """NFKC: precomposed and decomposed accents hash the same."""
    a = item_content_hash("s", "café")          # precomposed é
    b = item_content_hash("s", "café")    # combining é
    assert a == b


def test_hash_normalizes_case() -> None:
    a = item_content_hash("s", "Hello World")
    b = item_content_hash("s", "hello world")
    assert a == b


def test_hash_strips_leading_markdown_bullets() -> None:
    """- foo, * foo, + foo, 1. foo, > foo all reduce to the same hash."""
    plain = item_content_hash("s", "buy milk")
    assert item_content_hash("s", "- buy milk") == plain
    assert item_content_hash("s", "* buy milk") == plain
    assert item_content_hash("s", "+ buy milk") == plain
    assert item_content_hash("s", "1. buy milk") == plain
    assert item_content_hash("s", "  - buy milk") == plain


def test_hash_collapses_whitespace() -> None:
    a = item_content_hash("s", "alpha   beta\n\ngamma")
    b = item_content_hash("s", "alpha beta gamma")
    assert a == b


def test_hash_scopes_by_source() -> None:
    a = item_content_hash("journal_thread", "same text")
    b = item_content_hash("inline", "same text")
    assert a != b


def test_hash_empty_text_stable() -> None:
    """Empty text doesn't crash; hashes deterministically."""
    a = item_content_hash("s", "")
    b = item_content_hash("s", None)  # type: ignore[arg-type]
    assert a == b
