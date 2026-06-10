"""Unit tests for the consolidated index value types + config (work_buddy/index/).

No embedding service / DB needed — pure value-type + config behavior.
"""

from __future__ import annotations

from work_buddy.index.model import (
    Document,
    Hit,
    ItemRef,
    PoolStrategy,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    Query,
    content_hash,
    make_doc_id,
)
from work_buddy.index.config import (
    DEFAULT_RRF_K,
    IndexConfig,
    PartitionConfig,
    load_index_config,
)


# ---------------------------------------------------------------------------
# Enums: string-equal to the IR engine's Literal values (interop, no 3.11 floor)
# ---------------------------------------------------------------------------

class TestEnums:
    def test_projection_kind_is_str_equal(self):
        assert ProjectionKind.LABEL == "label"
        assert ProjectionKind.PASSAGE == "passage"
        # usable as a plain string (dict keys, JSON, IR interop)
        assert {"k": ProjectionKind.PASSAGE}["k"] == "passage"
        assert f"{ProjectionKind.LABEL}" in ("label", "ProjectionKind.LABEL")

    def test_pool_strategy_is_str_equal(self):
        assert PoolStrategy.NONE == "none"
        assert PoolStrategy.MAX == "max"
        assert PoolStrategy.MEAN == "mean"

    def test_projection_spec_defaults(self):
        spec = ProjectionSpec(kind=ProjectionKind.PASSAGE)
        assert spec.pool == PoolStrategy.NONE
        assert spec.model_key is None
        # frozen
        import dataclasses
        try:
            spec.kind = ProjectionKind.LABEL  # type: ignore[misc]
            assert False, "ProjectionSpec must be frozen"
        except dataclasses.FrozenInstanceError:
            pass


# ---------------------------------------------------------------------------
# content_hash + doc id
# ---------------------------------------------------------------------------

class TestHashingAndIds:
    def test_content_hash_deterministic_and_16_chars(self):
        h1 = content_hash("hello world")
        h2 = content_hash("hello world")
        assert h1 == h2
        assert len(h1) == 16
        assert content_hash("hello world") != content_hash("hello worlD")

    def test_content_hash_handles_empty(self):
        assert isinstance(content_hash(""), str)
        assert len(content_hash("")) == 16

    def test_make_doc_id(self):
        assert make_doc_id("knowledge", "tasks/triage") == "knowledge:tasks/triage"


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------

class TestDocument:
    def test_defaults(self):
        d = Document(doc_id="knowledge:x", partition="knowledge", fields={"name": "X"})
        assert d.display_text == ""
        assert d.metadata == {}
        assert d.projections == {}
        assert d.content_hash == ""
        assert d.timestamp is None

    def test_ensure_hash_populates_and_is_stable(self):
        d = Document(
            doc_id="knowledge:x", partition="knowledge",
            fields={"name": "Alpha", "body": "beta gamma"},
            projections={"content": Projection(text="beta gamma")},
        )
        h = d.ensure_hash()
        assert h and d.content_hash == h
        # idempotent
        assert d.ensure_hash() == h
        # changing a field changes the hashable text (fresh doc → different hash)
        d2 = Document(
            doc_id="knowledge:x", partition="knowledge",
            fields={"name": "Alpha", "body": "beta DELTA"},
            projections={"content": Projection(text="beta gamma")},
        )
        assert d2.ensure_hash() != h

    def test_hashable_text_is_order_independent_over_fields(self):
        a = Document(doc_id="p:1", partition="p", fields={"a": "1", "b": "2"})
        b = Document(doc_id="p:1", partition="p", fields={"b": "2", "a": "1"})
        assert a.hashable_text() == b.hashable_text()


# ---------------------------------------------------------------------------
# Query / Hit / ItemRef
# ---------------------------------------------------------------------------

class TestQueryHitItemRef:
    def test_query_defaults(self):
        q = Query(text="find me")
        assert q.top_k == 10
        assert q.method == "hybrid"
        assert q.filters == {}
        assert q.scope is None
        assert q.recency is False
        assert q.rrf_k is None

    def test_hit_defaults(self):
        h = Hit(doc_id="p:1", score=0.5)
        assert h.signals == {}
        assert h.metadata == {}

    def test_itemref(self):
        ref = ItemRef(item_id="f.md", mtime=123.0)
        assert ref.content_hash is None
        ref2 = ItemRef(item_id="f.md", content_hash="abc")
        assert ref2.mtime == 0.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_load_defaults_when_absent(self):
        cfg = load_index_config({})  # no index: block
        assert cfg.enabled is False
        assert cfg.db_path is None
        assert cfg.partitions == {}

    def test_load_disabled_by_default_even_with_block(self):
        cfg = load_index_config({"index": {}})
        assert cfg.enabled is False

    def test_partition_fallback_for_unlisted(self):
        cfg = load_index_config({"index": {"enabled": True}})
        assert cfg.enabled is True
        pc = cfg.partition("knowledge")
        assert pc.name == "knowledge"
        assert pc.rrf_k == DEFAULT_RRF_K

    def test_partition_overrides(self):
        cfg = load_index_config({
            "index": {
                "enabled": True,
                "partitions": {"knowledge": {"rrf_k": 15, "recency": True}},
            }
        })
        pc = cfg.partition("knowledge")
        assert pc.rrf_k == 15
        assert pc.recency is True
        # unlisted still gets defaults
        assert cfg.partition("conversation").rrf_k == DEFAULT_RRF_K

    def test_malformed_block_degrades_to_off(self):
        cfg = load_index_config({"index": "not a dict"})
        assert cfg.enabled is False

    def test_resolved_db_path_default(self):
        cfg = load_index_config({})
        p = cfg.resolved_db_path()
        assert p.name == "index-consolidated.db"
