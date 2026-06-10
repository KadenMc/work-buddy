"""Tests for index/fusion.py (RRF, parity with ir.store) + index/recency.py."""

from __future__ import annotations

import pytest

from work_buddy.index.fusion import RRF_K, rrf_fuse
from work_buddy.index.recency import apply_recency_bias, recency_weight
from work_buddy.index.model import Hit


# ---------------------------------------------------------------------------
# RRF + parity with the IR engine's implementation
# ---------------------------------------------------------------------------

class TestRrfFuse:
    def test_default_k(self):
        assert RRF_K == 60

    def test_single_ranking_preserves_order(self):
        r = {"a": 0.9, "b": 0.5, "c": 0.1}
        fused = rrf_fuse([r])
        ranked = sorted(fused, key=fused.get, reverse=True)
        assert ranked == ["a", "b", "c"]

    def test_shared_doc_ranks_higher(self):
        # b appears in both rankings → should fuse to the top.
        r1 = {"a": 0.9, "b": 0.5}
        r2 = {"b": 0.9, "c": 0.5}
        fused = rrf_fuse([r1, r2])
        top = max(fused, key=fused.get)
        assert top == "b"

    def test_empty_rankings(self):
        assert rrf_fuse([]) == {}
        assert rrf_fuse([{}, {}]) == {}

    def test_k_changes_spread_not_order(self):
        r1 = {"a": 0.9, "b": 0.5}
        r2 = {"b": 0.9, "a": 0.5}
        f60 = rrf_fuse([r1, r2], k=60)
        f15 = rrf_fuse([r1, r2], k=15)
        # symmetric → both tie; but the key property: same key set, valid scores
        assert set(f60) == set(f15) == {"a", "b"}

    def test_parity_with_ir_store(self):
        """Verbatim-lift parity: index.fusion.rrf_fuse == ir.store.rrf_fuse."""
        from work_buddy.ir.store import rrf_fuse as ir_rrf
        cases = [
            [{"a": 0.9, "b": 0.5, "c": 0.1}],
            [{"a": 0.9, "b": 0.5}, {"b": 0.8, "c": 0.4, "d": 0.2}],
            [{}, {"x": 1.0}],
            [{"p": 0.3, "q": 0.3, "r": 0.3}],  # ties
        ]
        for ks in (60, 20, 15, 1):
            for c in cases:
                assert rrf_fuse(c, k=ks) == ir_rrf(c, k=ks), (c, ks)


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------

class TestRecencyWeight:
    def test_none_timestamp_no_penalty(self):
        assert recency_weight(None) == 1.0

    def test_zero_half_life_disabled(self):
        assert recency_weight(1000.0, half_life_days=0, now_epoch=1_000_000.0) == 1.0

    def test_fresh_is_near_one(self):
        now = 1_000_000.0
        assert recency_weight(now, now_epoch=now) == pytest.approx(1.0)

    def test_one_half_life_is_midpoint(self):
        now = 0.0
        ts = -14 * 86400.0  # 14 days old, half_life=14
        w = recency_weight(ts, half_life_days=14.0, floor=0.15, now_epoch=now)
        # decay = 0.5 → floor + (1-floor)*0.5 = 0.15 + 0.425 = 0.575
        assert w == pytest.approx(0.575, abs=1e-3)

    def test_very_old_approaches_floor(self):
        now = 0.0
        ts = -3650 * 86400.0  # ~10 years
        w = recency_weight(ts, half_life_days=14.0, floor=0.15, now_epoch=now)
        assert w == pytest.approx(0.15, abs=1e-3)

    def test_monotonic_decreasing_with_age(self):
        now = 0.0
        weights = [recency_weight(-d * 86400.0, now_epoch=now) for d in (0, 7, 14, 60)]
        assert weights == sorted(weights, reverse=True)


class TestApplyRecencyBias:
    def test_mutates_and_resorts(self):
        now = 0.0
        hits = [Hit(doc_id="old", score=0.9), Hit(doc_id="new", score=0.6)]
        ts = {"old": -100 * 86400.0, "new": now}  # old is heavily decayed
        out = apply_recency_bias(hits, ts, half_life_days=14.0, floor=0.15, now_epoch=now)
        # 'new' should now outrank 'old' despite lower raw score
        assert out[0].doc_id == "new"
        # signals recorded
        assert "recency_weight" in out[0].signals
        assert out[0].signals["raw_score"] == 0.6

    def test_empty(self):
        assert apply_recency_bias([], {}) == []

    def test_disabled_half_life_noop(self):
        hits = [Hit(doc_id="a", score=0.5)]
        out = apply_recency_bias(hits, {"a": 0.0}, half_life_days=0, now_epoch=1.0)
        assert out[0].score == 0.5  # untouched
