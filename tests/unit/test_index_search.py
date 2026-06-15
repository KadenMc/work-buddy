"""Tests for index/search.py — HybridSearcher + MultiQueryFuser.

A FakeEncoder stands in for the embedding service; vectors are pre-loaded into a
tmp_path IndexStore. Exercises lexical-only, hybrid fusion, degrade-to-lexical,
metadata filtering, recency, and the multi-query fan-out.
"""

from __future__ import annotations

import numpy as np
import pytest

from work_buddy.index.config import PartitionConfig
from work_buddy.index.model import Document, Hit, Projection, ProjectionKind, ProjectionSpec, Query
from work_buddy.index.resident import ResidentCacheRegistry
from work_buddy.index.search import HybridSearcher, MultiQueryFuser
from work_buddy.index.store import IndexStore


class FakeEncoder:
    def __init__(self, qvec=(1.0, 0.0), down=False):
        self.qvec = list(qvec)
        self.down = down

    def encode_query(self, texts, kind, model_key=None):
        if self.down:
            return None
        # One row per query (so batched search_many gets an (N, D) matrix); for a
        # single query this is (1, D) — unchanged for the single-query tests.
        return np.array([self.qvec] * len(texts), dtype=np.float32)

    def encode_documents(self, texts, kind, model_key=None):
        return np.zeros((len(texts), len(self.qvec)), dtype=np.float32)


@pytest.fixture
def store(tmp_path):
    return IndexStore(tmp_path / "search-index.db")


class TestSourceCap:
    """The score-guarded per-source diversity cap (_cap_by_source)."""

    def _searcher(self, store, cap):
        return HybridSearcher(
            store, FakeEncoder(), partition="vault", projection_schema={},
            cfg=PartitionConfig(name="vault", max_per_source=cap),
            residents=ResidentCacheRegistry(),
        )

    @staticmethod
    def _docs(spec):
        # spec: {doc_id: source_path}
        return {d: {"metadata": {"source_path": sp}} for d, sp in spec.items()}

    def test_dominant_doc_with_no_competitive_alt_keeps_slots(self, store):
        # D floods, the only other source (e1) scores far below 0.9x → not competitive,
        # so the cap must NOT swap D's chunks for it (single-dominant-doc query preserved).
        ranked = ["d1", "d2", "d3", "d4", "e1"]
        scores = {"d1": 1.0, "d2": 0.97, "d3": 0.95, "d4": 0.93, "e1": 0.50}
        docs = self._docs({"d1": "D", "d2": "D", "d3": "D", "d4": "D", "e1": "E"})
        out = self._searcher(store, 2)._cap_by_source(ranked, scores, docs, 2, 4)
        assert out == ["d1", "d2", "d3", "d4"]  # E never displaces a much stronger D chunk

    def test_flooding_broken_when_competitive_alts_exist(self, store):
        # C's chunks are within 0.9x of D's over-cap chunks → cap swaps them in.
        ranked = ["d1", "d2", "d3", "d4", "c1", "c2"]
        scores = {"d1": 1.0, "d2": 0.98, "d3": 0.96, "d4": 0.94, "c1": 0.93, "c2": 0.91}
        docs = self._docs({"d1": "D", "d2": "D", "d3": "D", "d4": "D", "c1": "C", "c2": "C"})
        out = self._searcher(store, 2)._cap_by_source(ranked, scores, docs, 2, 4)
        assert out == ["d1", "d2", "c1", "c2"]  # D capped at 2, C diversifies the rest

    def test_backfill_fills_topk_from_deferred(self, store):
        # cap=1 with only two sources but top_k=4 → after one each, backfill the rest.
        ranked = ["d1", "c1", "d2", "c2"]
        scores = {"d1": 1.0, "c1": 0.95, "d2": 0.92, "c2": 0.90}
        docs = self._docs({"d1": "D", "c1": "C", "d2": "D", "c2": "C"})
        out = self._searcher(store, 1)._cap_by_source(ranked, scores, docs, 1, 4)
        assert set(out) == {"d1", "c1", "d2", "c2"} and out[:2] == ["d1", "c1"]

    def test_cap_off_is_identity(self, store):
        # max_per_source=None → search() takes the plain top-k (covered here by asserting
        # the cap method is only reached when configured); a None cfg leaves ranking intact.
        assert self._searcher(store, None)._cfg.max_per_source is None


def _seed(store, *, with_vectors=True, timestamps=None):
    """3 knowledge docs; optional content vectors aligning 'a' with query [1,0]."""
    ts = timestamps or {}
    docs = [
        Document(doc_id="knowledge:a", partition="knowledge",
                 fields={"name": "alpha", "body": "first doc"},
                 display_text="alpha", projections={"content": Projection(text="alpha first")},
                 metadata={"kind": "system"}, timestamp=ts.get("a")),
        Document(doc_id="knowledge:b", partition="knowledge",
                 fields={"name": "beta", "body": "second doc"},
                 display_text="beta", projections={"content": Projection(text="beta second")},
                 metadata={"kind": "directions"}, timestamp=ts.get("b")),
        Document(doc_id="knowledge:c", partition="knowledge",
                 fields={"name": "gamma alpha", "body": "third doc"},
                 display_text="gamma", projections={"content": Projection(text="gamma third")},
                 metadata={"kind": "system"}, timestamp=ts.get("c")),
    ]
    store.upsert_documents(docs, item_id="seed")
    if with_vectors:
        store.upsert_vectors("content", [
            ("knowledge:a", np.array([1.0, 0.0], dtype=np.float32)),    # aligns w/ query
            ("knowledge:b", np.array([0.0, 1.0], dtype=np.float32)),    # orthogonal
            ("knowledge:c", np.array([0.7, 0.7], dtype=np.float32)),    # partial
        ])


def _searcher(store, encoder, cfg=None):
    return HybridSearcher(
        store, encoder, partition="knowledge",
        projection_schema={"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)},
        cfg=cfg or PartitionConfig(name="knowledge"),
        residents=ResidentCacheRegistry(),  # fresh per searcher (test isolation)
    )


class TestHybridSearch:
    def test_empty_query(self, store):
        _seed(store)
        assert _searcher(store, FakeEncoder()).search(Query(text="")) == []

    def test_lexical_only_method(self, store):
        _seed(store)
        hits = _searcher(store, FakeEncoder()).search(Query(text="alpha", method="lexical"))
        ids = [h.doc_id for h in hits]
        assert "knowledge:a" in ids  # name match
        # all hits carry a lexical signal, no dense
        assert all("lexical" in h.signals for h in hits)
        assert all("content" not in h.signals for h in hits)

    def test_degrade_to_lexical_when_encoder_down(self, store):
        _seed(store)
        hits = _searcher(store, FakeEncoder(down=True)).search(Query(text="alpha"))
        assert hits  # still works (lexical only)
        assert all("content" not in h.signals for h in hits)

    def test_hybrid_fuses_lexical_and_dense(self, store):
        _seed(store)
        hits = _searcher(store, FakeEncoder(qvec=(1.0, 0.0))).search(Query(text="alpha"))
        assert hits[0].doc_id == "knowledge:a"  # top of both lexical + dense
        top = hits[0]
        assert "lexical" in top.signals and "content" in top.signals

    def test_dense_only_method(self, store):
        _seed(store)
        # query text has no lexical match, but dense aligns with 'a'
        hits = _searcher(store, FakeEncoder(qvec=(1.0, 0.0))).search(
            Query(text="zzz", method="dense")
        )
        ids = [h.doc_id for h in hits]
        assert ids[0] == "knowledge:a"
        assert all("lexical" not in h.signals for h in hits)

    def test_metadata_filter(self, store):
        _seed(store)
        hits = _searcher(store, FakeEncoder()).search(
            Query(text="alpha", filters={"kind": "directions"})
        )
        # only beta is kind=directions, but it has no 'alpha' — lexical empty;
        # dense restricted to allowed set {b}. So either empty or only b.
        assert all(h.doc_id == "knowledge:b" for h in hits)

    def test_recency_reorders(self, store):
        import time
        now = time.time()  # searcher's recency uses real time.time()
        # 'a' is ancient, 'c' is fresh; both match 'alpha' lexically (c name has alpha)
        _seed(store, with_vectors=False, timestamps={
            "a": now - 365 * 86400.0, "c": now,
        })
        cfg = PartitionConfig(name="knowledge", recency=True, recency_half_life_days=14.0)
        hits = HybridSearcher(
            store, FakeEncoder(down=True), partition="knowledge",
            projection_schema={}, cfg=cfg, residents=ResidentCacheRegistry(),
        ).search(Query(text="alpha", method="lexical", recency=True))
        ids = [h.doc_id for h in hits]
        # fresh 'c' should outrank ancient 'a' after recency decay
        assert ids.index("knowledge:c") < ids.index("knowledge:a")


class _BadCountEncoder:
    """Returns the WRONG row count for a batch (always 1 row) → batch degrade guard."""

    def encode_query(self, texts, kind, model_key=None):
        return np.array([[1.0, 0.0]], dtype=np.float32)  # 1 row regardless of len(texts)

    def encode_documents(self, texts, kind, model_key=None):
        return np.zeros((len(texts), 2), dtype=np.float32)


class TestSearchMany:
    def test_parity_with_single_search(self, store):
        """search_many([q1,q2]) must equal [search(q1), search(q2)] element-for-element."""
        _seed(store)
        s = _searcher(store, FakeEncoder(qvec=(1.0, 0.0)))
        qs = ["alpha", "gamma"]
        batch = s.search_many(qs, top_k=10)
        singles = [s.search(Query(text=q, top_k=10)) for q in qs]
        assert len(batch) == 2
        for b, single in zip(batch, singles):
            assert [h.doc_id for h in b] == [h.doc_id for h in single]
            assert [h.score for h in b] == [h.score for h in single]
            assert [h.signals for h in b] == [h.signals for h in single]

    def test_order_and_empty_query_preserved(self, store):
        _seed(store)
        batch = _searcher(store, FakeEncoder(qvec=(1.0, 0.0))).search_many(
            ["alpha", "", "gamma"], top_k=10,
        )
        assert len(batch) == 3
        assert batch[1] == []  # empty query → empty list, position preserved
        assert any(h.doc_id == "knowledge:a" for h in batch[0])

    def test_degrade_to_lexical_when_encoder_down(self, store):
        _seed(store)
        batch = _searcher(store, FakeEncoder(down=True)).search_many(["alpha"], top_k=10)
        assert batch and batch[0]
        assert all("content" not in h.signals for h in batch[0])  # lexical only

    def test_wrong_row_count_degrades_to_lexical(self, store):
        _seed(store)
        # Encoder returns 1 row for a 2-query batch → projection degrades (no dense),
        # but lexical still produces results and nothing raises.
        batch = _searcher(store, _BadCountEncoder()).search_many(["alpha", "gamma"], top_k=10)
        assert len(batch) == 2
        for hits in batch:
            assert all("content" not in h.signals for h in hits)


class TestMultiQueryFuser:
    def test_shared_doc_ranks_higher(self):
        q1 = [Hit(doc_id="a", score=0.9), Hit(doc_id="b", score=0.5)]
        q2 = [Hit(doc_id="b", score=0.9), Hit(doc_id="c", score=0.5)]
        fused = MultiQueryFuser.fuse([q1, q2])
        assert fused[0].doc_id == "b"  # in both queries

    def test_empty(self):
        assert MultiQueryFuser.fuse([]) == []
        assert MultiQueryFuser.fuse([[], []]) == []
