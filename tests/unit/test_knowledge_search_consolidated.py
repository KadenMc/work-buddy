"""Tests for the agent_docs knowledge-search re-point to the consolidated index.

`knowledge.search._search_via_consolidated` (the gated helper) is tested in isolation
(index_search + load_index_config mocked); the `_search()` branch is tested with light
fakes for the store + the live index, asserting the consolidated path is used when gated
on, falls back to live otherwise, preserves the result shape, and bounds the pool to top_n.
"""

from __future__ import annotations

import importlib

# NB: ``import work_buddy.knowledge.search as ks`` would bind ``ks`` to the
# ``search`` *function* re-exported by ``work_buddy/knowledge/__init__.py``
# (it shadows the submodule attribute), not the module. Import the module
# object directly so monkeypatching its members works.
ks = importlib.import_module("work_buddy.knowledge.search")


def _cfg(consumer_on: bool):
    from work_buddy.index.config import IndexConfig
    return IndexConfig(enabled=True, consumers={"agent_docs": consumer_on})


# ---------------------------------------------------------------------------
# _search_via_consolidated — the gated helper (isolated)
# ---------------------------------------------------------------------------

class TestSearchViaConsolidated:
    def test_gate_off_returns_none_without_calling_service(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: _cfg(False))
        monkeypatch.setattr(
            "work_buddy.embedding.client.index_search",
            lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or [],
        )
        assert ks._search_via_consolidated("q", "system", 8) is None
        assert calls["n"] == 0  # service never touched when the consumer gate is off

    def test_gate_on_returns_scored_pairs(self, monkeypatch):
        monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: _cfg(True))
        hits = [
            {"doc_id": "knowledge:architecture/x", "score": 0.9, "metadata": {"path": "architecture/x"}},
            {"doc_id": "knowledge:context/y", "score": 0.5, "metadata": {"path": "context/y"}},
        ]
        monkeypatch.setattr("work_buddy.embedding.client.index_search", lambda *a, **k: hits)
        out = ks._search_via_consolidated("q", "system", 8)
        assert out == [{"path": "architecture/x", "score": 0.9}, {"path": "context/y", "score": 0.5}]

    def test_doc_id_fallback_when_metadata_path_absent(self, monkeypatch):
        monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: _cfg(True))
        monkeypatch.setattr(
            "work_buddy.embedding.client.index_search",
            lambda *a, **k: [{"doc_id": "knowledge:foo/bar", "score": 1.0, "metadata": {}}],
        )
        assert ks._search_via_consolidated("q", "system", 8) == [{"path": "foo/bar", "score": 1.0}]

    def test_none_when_service_down(self, monkeypatch):
        monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: _cfg(True))
        monkeypatch.setattr("work_buddy.embedding.client.index_search", lambda *a, **k: None)
        assert ks._search_via_consolidated("q", "system", 8) is None

    def test_none_when_empty(self, monkeypatch):
        monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: _cfg(True))
        monkeypatch.setattr("work_buddy.embedding.client.index_search", lambda *a, **k: [])
        assert ks._search_via_consolidated("q", "system", 8) is None

    def test_none_when_service_raises(self, monkeypatch):
        monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: _cfg(True))

        def _boom(*a, **k):
            raise RuntimeError("service exploded")

        monkeypatch.setattr("work_buddy.embedding.client.index_search", _boom)
        assert ks._search_via_consolidated("q", "system", 8) is None

    def test_knowledge_scope_maps_to_filters(self, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: _cfg(True))

        def _cap(query, **k):
            seen.clear()
            seen.update(k)
            return [{"doc_id": "knowledge:a", "score": 1.0, "metadata": {"path": "a"}}]

        monkeypatch.setattr("work_buddy.embedding.client.index_search", _cap)
        ks._search_via_consolidated("q", "system", 8)
        assert seen["filters"] == {"scope": "system"} and seen["partitions"] == ["knowledge"]
        ks._search_via_consolidated("q", "personal", 8)
        assert seen["filters"] == {"scope": "personal"}
        ks._search_via_consolidated("q", "all", 8)
        assert seen["filters"] is None  # "all" → no scope filter → omit (both system + personal)

    def test_filters_pushed_down(self, monkeypatch):
        """kind/category/severity → metadata filters; subtree-scope → doc_id prefix; top_k==top_n.

        This is the recall fix: the index filter-then-ranks within exactly the qualifying set,
        so a tight filter returns a full top_n (no bounded-pool post-filter truncation)."""
        seen: dict = {}
        monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: _cfg(True))

        def _cap(query, **k):
            seen.clear()
            seen.update(k)
            return [{"doc_id": "knowledge:a", "score": 1.0, "metadata": {"path": "a"}}]

        monkeypatch.setattr("work_buddy.embedding.client.index_search", _cap)
        ks._search_via_consolidated(
            "q", "personal", 7,
            scope="tasks/", kind="capability", category="work_pattern", severity="HIGH",
        )
        assert seen["filters"] == {
            "scope": "personal", "kind": "capability",
            "category": "work_pattern", "severity": "HIGH",
        }
        assert seen["scope"] == "knowledge:tasks/"  # subtree → doc_id prefix
        assert seen["top_k"] == 7  # no over-fetch: pre-filtering makes the pool unnecessary


# ---------------------------------------------------------------------------
# _search() branch — consolidated vs live fallback (light fakes)
# ---------------------------------------------------------------------------

class _FakeUnit:
    def __init__(self, path: str, kind: str = "system"):
        self.path = path
        self.kind = kind

    def search_phrases(self):
        return [self.path]

    def tier(self, depth, store=None, dev=False, recursive_mode="default", max_depth=None):
        return {"name": self.path, "description": "", "kind": self.kind}


class _FakeIdx:
    def __init__(self, result):
        self.result = result
        self.called = 0

    def search(self, query, candidates=None, top_n=8, **k):
        self.called += 1
        return self.result


class TestSearchRouting:
    def _wire(self, monkeypatch, store, *, consolidated, live):
        monkeypatch.setattr(ks, "load_store", lambda scope=None: store)
        monkeypatch.setattr(
            ks, "_search_via_consolidated", lambda q, ks_scope, top_n, **kw: consolidated
        )
        import work_buddy.knowledge.index as kidx
        idx = _FakeIdx(live)
        monkeypatch.setattr(kidx, "ensure_index", lambda **k: idx)
        return idx

    def test_gate_on_uses_consolidated_and_skips_live(self, monkeypatch):
        store = {"architecture/x": _FakeUnit("architecture/x"), "context/y": _FakeUnit("context/y")}
        idx = self._wire(
            monkeypatch, store,
            consolidated=[{"path": "architecture/x", "score": 0.9}],
            live=[{"path": "context/y", "score": 0.1}],
        )
        res = ks.search(query="needle", knowledge_scope="system", top_n=5)
        assert res["mode"] == "search"
        assert [r["path"] for r in res["results"]] == ["architecture/x"]
        assert idx.called == 0  # the live in-process index was NOT built/queried

    def test_falls_back_to_live_when_consolidated_none(self, monkeypatch):
        store = {"architecture/x": _FakeUnit("architecture/x"), "context/y": _FakeUnit("context/y")}
        idx = self._wire(
            monkeypatch, store,
            consolidated=None,  # gate off / service down / empty
            live=[{"path": "context/y", "score": 0.7}],
        )
        res = ks.search(query="needle", knowledge_scope="system", top_n=5)
        assert idx.called == 1  # fell back to the live index
        assert [r["path"] for r in res["results"]] == ["context/y"]

    def test_consolidated_pool_bounded_to_top_n(self, monkeypatch):
        store = {f"u/{i}": _FakeUnit(f"u/{i}") for i in range(20)}
        self._wire(
            monkeypatch, store,
            consolidated=[{"path": f"u/{i}", "score": 1.0 - i * 0.01} for i in range(20)],
            live=[],
        )
        res = ks.search(query="needle", knowledge_scope="system", top_n=3)
        assert len(res["results"]) == 3  # 20-hit pool trimmed to top_n


# ---------------------------------------------------------------------------
# KnowledgePartition metadata — category/severity present so filters push down
# ---------------------------------------------------------------------------

class TestPartitionMetadata:
    def _doc(self, path, **attrs):
        from types import SimpleNamespace
        from work_buddy.knowledge.partition import KnowledgePartition

        base = dict(name="N", description="d", tags=[], kind="reference",
                    aliases=[], content={"summary": "s", "full": "f"})
        base.update(attrs)
        return KnowledgePartition()._to_document(path, SimpleNamespace(**base), {})

    def test_category_severity_indexed(self):
        doc = self._doc("personal/x", category="work_pattern", severity="HIGH")
        assert doc.metadata["category"] == "work_pattern"
        assert doc.metadata["severity"] == "HIGH"

    def test_missing_category_severity_default_empty(self):
        doc = self._doc("architecture/x")  # no category/severity attrs → "" via getattr
        assert doc.metadata["category"] == "" and doc.metadata["severity"] == ""
