"""context_search → consolidated-index re-point (ops/context_consolidated.py).

Covers the consumer gate, the supported-subset guards (single source, no scope,
hybrid-mappable method), the method mapping, the service-failure / empty fallbacks, and
the hit → IR-result-shape adapter. No embedding service — index_search is monkeypatched.
"""
from __future__ import annotations

import pytest

from work_buddy.index.config import IndexConfig
from work_buddy.mcp_server.ops import context_consolidated as cc


def _enable(monkeypatch, on=True, enabled=True):
    cfg = IndexConfig(enabled=enabled, consumers={"context_search": on})
    monkeypatch.setattr(
        "work_buddy.index.config.load_index_config", lambda *a, **k: cfg
    )


def _fake_index_search(monkeypatch, hits, capture=None):
    def fake(query, **kwargs):
        if capture is not None:
            capture.update(kwargs)
            capture["query"] = query
        return hits
    monkeypatch.setattr("work_buddy.embedding.client.index_search", fake)


_HIT = {
    "doc_id": "conversation:s1#3",
    "score": 0.91,
    "signals": {"fused": 0.91, "lexical": 0.4, "default": 0.7},
    "display_text": "we discussed the index gate",
    "metadata": {"session_id": "s1", "project_name": "work-buddy"},
}


class TestGatingAndSubset:
    def test_gate_off_returns_none(self, monkeypatch):
        _enable(monkeypatch, on=False)
        assert cc.search_context_via_consolidated("q", source="conversation") is None

    def test_index_disabled_returns_none(self, monkeypatch):
        _enable(monkeypatch, on=True, enabled=False)  # consumer on but master off
        assert cc.search_context_via_consolidated("q", source="conversation") is None

    def test_all_source_returns_none(self, monkeypatch):
        _enable(monkeypatch)
        assert cc.search_context_via_consolidated("q", source=None) is None

    def test_scoped_returns_none(self, monkeypatch):
        _enable(monkeypatch)
        assert cc.search_context_via_consolidated(
            "q", source="conversation", scope="s1"
        ) is None

    def test_substring_returns_none(self, monkeypatch):
        _enable(monkeypatch)
        assert cc.search_context_via_consolidated(
            "q", source="conversation", method="substring"
        ) is None


class TestRouting:
    def test_happy_path_adapts_hits(self, monkeypatch):
        _enable(monkeypatch)
        _fake_index_search(monkeypatch, [_HIT])
        out = cc.search_context_via_consolidated(
            "q", source="conversation", method="keyword,semantic"
        )
        assert out is not None and len(out) == 1
        r = out[0]
        assert r["source"] == "conversation"
        assert r["doc_id"] == "conversation:s1#3"
        assert r["score"] == 0.91
        assert r["display_text"].startswith("we discussed")
        assert r["metadata"]["session_id"] == "s1"
        assert r["bm25_score"] == 0.4
        assert r["dense_score"] == 0.7      # lone dense projection → dense_score
        assert r["projection_scores"] is None

    @pytest.mark.parametrize("method,expected", [
        ("keyword", "lexical"),
        ("semantic", "dense"),
        ("keyword,semantic", "hybrid"),
        ("semantic,keyword", "hybrid"),
    ])
    def test_method_mapping(self, monkeypatch, method, expected):
        _enable(monkeypatch)
        cap: dict = {}
        _fake_index_search(monkeypatch, [_HIT], capture=cap)
        cc.search_context_via_consolidated("q", source="conversation", method=method)
        assert cap["method"] == expected
        assert cap["partitions"] == ["conversation"]

    def test_service_failure_returns_none(self, monkeypatch):
        _enable(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("service down")
        monkeypatch.setattr("work_buddy.embedding.client.index_search", boom)
        assert cc.search_context_via_consolidated("q", source="conversation") is None

    def test_empty_hits_returns_none(self, monkeypatch):
        _enable(monkeypatch)
        _fake_index_search(monkeypatch, [])
        assert cc.search_context_via_consolidated("q", source="conversation") is None


class TestAdapter:
    def test_single_projection_to_dense_score(self):
        r = cc.adapt_consolidated_hit(
            {"doc_id": "x:1", "score": 0.5, "display_text": "t", "metadata": {"a": 1},
             "signals": {"fused": 0.5, "lexical": 0.2, "default": 0.6}}, "chrome")
        assert r["source"] == "chrome"
        assert r["bm25_score"] == 0.2 and r["dense_score"] == 0.6
        assert r["projection_scores"] is None

    def test_multi_projection_to_projection_scores(self):
        r = cc.adapt_consolidated_hit(
            {"doc_id": "task_note:1", "score": 0.5, "display_text": "t", "metadata": {},
             "signals": {"fused": 0.5, "lexical": 0.2, "line": 0.7, "body": 0.4}},
            "task_note")
        assert r["dense_score"] is None
        assert r["projection_scores"] == {"line": 0.7, "body": 0.4}

    def test_lexical_only_no_dense(self):
        r = cc.adapt_consolidated_hit(
            {"doc_id": "x:1", "score": 0.3, "signals": {"fused": 0.3, "lexical": 0.3}},
            "chrome")
        assert r["bm25_score"] == 0.3 and r["dense_score"] is None
        assert r["projection_scores"] is None
