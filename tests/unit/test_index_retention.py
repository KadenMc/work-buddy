"""Per-partition retention policy: PartitionConfig parsing + build-prune behavior + the
store orphan/TTL primitives. tmp_path store, FakeEncoder, no embedding service."""
from __future__ import annotations

import time

import numpy as np
import pytest

from work_buddy.index.build import IndexBuilder
from work_buddy.index.config import PartitionConfig
from work_buddy.index.model import (
    Document, ItemRef, Projection, ProjectionKind, ProjectionSpec,
)
from work_buddy.index.resident import ResidentCacheRegistry
from work_buddy.index.store import IndexStore


class FakeEncoder:
    def encode_query(self, texts, kind, model_key=None):
        return np.ones((len(texts), 4), dtype=np.float32)

    def encode_documents(self, texts, kind, model_key=None):
        return np.ones((len(texts), 4), dtype=np.float32)


class FakePartition:
    name = "fake"
    change_key = "hash"

    def __init__(self, items):
        # items: {item_id: {"hash": str, "ts": float | None}}
        self.items = items

    def projection_schema(self):
        return {"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def discover(self):
        return [ItemRef(item_id=i, content_hash=v["hash"]) for i, v in self.items.items()]

    def parse(self, item_id):
        v = self.items[item_id]
        return [Document(
            doc_id=f"fake:{item_id}", partition="fake",
            fields={"name": item_id, "body": f"{item_id} body"},
            display_text=item_id,
            projections={"content": Projection(text=f"{item_id} passage")},
            timestamp=v.get("ts"),
        )]


@pytest.fixture
def store(tmp_path):
    return IndexStore(tmp_path / "ret.db")


def _builder(store, part, retention="track_source", ttl_days=None):
    cfg = PartitionConfig(name="fake", retention=retention, retention_ttl_days=ttl_days)
    return IndexBuilder(
        store, FakeEncoder(), part, cfg=cfg,
        residents=ResidentCacheRegistry(), use_lock=False,
    )


def _state(store, doc_id):
    docs = store.load_documents(partition="fake", doc_ids=[doc_id])
    return (docs.get(doc_id, {}).get("metadata") or {}).get("lifecycle_state")


def _exists(store, doc_id):
    return doc_id in store.load_documents(partition="fake", doc_ids=[doc_id])


class TestConfigParsing:
    def test_default_is_track_source(self):
        c = PartitionConfig.from_dict("x", {})
        assert c.retention == "track_source" and c.retention_ttl_days is None

    def test_retain(self):
        assert PartitionConfig.from_dict("x", {"retention": "retain"}).retention == "retain"

    def test_ttl_dict(self):
        c = PartitionConfig.from_dict("x", {"retention": {"ttl_days": 90}})
        assert c.retention == "ttl" and c.retention_ttl_days == 90.0

    def test_ttl_without_days_degrades_to_retain(self):
        c = PartitionConfig.from_dict("x", {"retention": {}})
        assert c.retention == "retain" and c.retention_ttl_days is None

    def test_unknown_degrades_to_track_source(self):
        assert PartitionConfig.from_dict("x", {"retention": "bogus"}).retention == "track_source"


class TestPrune:
    def test_track_source_deletes_dropped(self, store):
        part = FakePartition({"a": {"hash": "1"}, "b": {"hash": "1"}})
        _builder(store, part).build()
        assert store.doc_count("fake") == 2
        del part.items["b"]
        _builder(store, part).build()
        assert store.doc_count("fake") == 1
        assert not _exists(store, "fake:b")

    def test_retain_keeps_orphans_and_forgets_ledger(self, store):
        part = FakePartition({"a": {"hash": "1"}, "b": {"hash": "1"}})
        _builder(store, part, retention="retain").build()
        del part.items["b"]
        _builder(store, part, retention="retain").build()
        assert store.doc_count("fake") == 2          # b kept
        assert _state(store, "fake:b") == "orphaned"
        assert _state(store, "fake:a") is None        # live item untouched
        assert "b" not in store.get_indexed_items("fake")  # ledger forgot it → no churn

    def test_restored_item_reindexes_fresh(self, store):
        part = FakePartition({"a": {"hash": "1"}, "b": {"hash": "1"}})
        _builder(store, part, retention="retain").build()
        del part.items["b"]
        _builder(store, part, retention="retain").build()
        assert _state(store, "fake:b") == "orphaned"
        part.items["b"] = {"hash": "1"}               # source restores b (same content)
        _builder(store, part, retention="retain").build()
        assert _state(store, "fake:b") is None         # ledger had forgotten it → fresh re-index
        assert "b" in store.get_indexed_items("fake")

    def test_ttl_sweeps_old_orphans_keeps_recent(self, store):
        now = time.time()
        part = FakePartition({
            "recent": {"hash": "1", "ts": now - 5 * 86400},     # 5d old
            "old": {"hash": "1", "ts": now - 100 * 86400},      # 100d old
        })
        _builder(store, part, retention="ttl", ttl_days=30).build()
        del part.items["recent"]
        del part.items["old"]
        _builder(store, part, retention="ttl", ttl_days=30).build()
        assert _state(store, "fake:recent") == "orphaned"  # within window → kept
        assert not _exists(store, "fake:old")              # past window → swept


class TestStorePrimitives:
    def test_mark_items_orphaned(self, store):
        part = FakePartition({"a": {"hash": "1"}})
        _builder(store, part).build()
        store.mark_items_orphaned(["a"], "fake")
        assert _state(store, "fake:a") == "orphaned"
        assert "a" not in store.get_indexed_items("fake")

    def test_prune_orphans_older_than(self, store):
        now = time.time()
        part = FakePartition({"old": {"hash": "1", "ts": now - 100 * 86400}})
        _builder(store, part).build()
        store.mark_items_orphaned(["old"], "fake")
        store.prune_orphans_older_than("fake", now - 200 * 86400)  # cutoff older than doc
        assert store.doc_count("fake") == 1                         # not yet
        store.prune_orphans_older_than("fake", now - 30 * 86400)   # cutoff newer than doc
        assert store.doc_count("fake") == 0                         # swept

    def test_prune_orphans_spares_live_docs(self, store):
        # A non-orphaned doc older than the cutoff is NOT swept (only orphans age out).
        now = time.time()
        part = FakePartition({"live": {"hash": "1", "ts": now - 100 * 86400}})
        _builder(store, part).build()
        store.prune_orphans_older_than("fake", now)  # everything is "old", but none orphaned
        assert store.doc_count("fake") == 1
