"""Tests for index/build.py (IndexBuilder) + index/partition.py (registry/accessors).

A FakePartition supplies items/docs; a FakeEncoder returns deterministic vectors.
tmp_path IndexStore; no real embedding service.
"""

from __future__ import annotations

import numpy as np
import pytest

from work_buddy.index.build import IndexBuilder
from work_buddy.index.model import Document, ItemRef, Projection, ProjectionKind, ProjectionSpec
from work_buddy.index.partition import (
    PartitionRegistry,
    get_change_key,
    get_projection_schema,
    hydrate,
)
from work_buddy.index.resident import ResidentCacheRegistry
from work_buddy.index.store import IndexStore


class FakeEncoder:
    def __init__(self, dim=4, down=False):
        self.dim = dim
        self.down = down

    def encode_query(self, texts, kind, model_key=None):
        return None if self.down else np.ones((len(texts), self.dim), dtype=np.float32)

    def encode_documents(self, texts, kind, model_key=None):
        return None if self.down else np.ones((len(texts), self.dim), dtype=np.float32)


class RecordingEncoder(FakeEncoder):
    def __init__(self, dim=4, down=False):
        super().__init__(dim=dim, down=down)
        self.document_calls: list[list[str]] = []

    def encode_documents(self, texts, kind, model_key=None):
        self.document_calls.append(list(texts))
        if self.down:
            return None
        return np.asarray(
            [
                [
                    len(text),
                    sum(map(ord, text)),
                    text.count(" "),
                    sum((index + 1) * ord(char) for index, char in enumerate(text)),
                ]
                for text in texts
            ],
            dtype=np.float32,
        )


class FakePartition:
    name = "fake"
    change_key = "hash"

    def __init__(self, items):
        # items: {item_id: {"hash": str, "docs": [(suffix, name, proj_text)]}}
        self.items = items

    def field_weights(self):
        return {"title": 3.0, "body": 1.0}

    def projection_schema(self):
        return {"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def discover(self):
        return [ItemRef(item_id=iid, content_hash=v["hash"]) for iid, v in self.items.items()]

    def parse(self, item_id):
        out = []
        for suffix, name, proj_text in self.items[item_id]["docs"]:
            out.append(Document(
                doc_id=f"fake:{suffix}", partition="fake",
                fields={"name": name, "body": f"{name} body"},
                display_text=name,
                projections={"content": Projection(text=proj_text)},
            ))
        return out

    def hydrate(self, hits, **opts):
        return hits


@pytest.fixture
def store(tmp_path):
    return IndexStore(tmp_path / "build-index.db")


def _builder(store, partition, encoder=None):
    return IndexBuilder(
        store, encoder or FakeEncoder(), partition,
        residents=ResidentCacheRegistry(), use_lock=False,
    )


class TestBuild:
    def test_build_indexes_docs_and_vectors(self, store):
        part = FakePartition({
            "i1": {"hash": "h1", "docs": [("a", "alpha", "alpha passage")]},
            "i2": {"hash": "h2", "docs": [("b", "beta", "beta passage")]},
        })
        stats = _builder(store, part).build()
        assert stats["changed"] == 2
        assert stats["doc_count"] == 2
        assert stats["version"] == 1
        assert store.vector_count("fake", "content") == 2
        # lexical works
        assert "fake:a" in store.search_lexical("alpha", partition="fake")

    def test_incremental_no_change(self, store):
        part = FakePartition({"i1": {"hash": "h1", "docs": [("a", "alpha", "p")]}})
        _builder(store, part).build()
        stats2 = _builder(store, part).build()
        assert stats2["changed"] == 0
        assert stats2["deleted"] == 0
        assert stats2["version"] == 1  # unchanged → no bump

    def test_changed_item_reindexed(self, store):
        part = FakePartition({"i1": {"hash": "h1", "docs": [("a", "alpha", "p")]}})
        _builder(store, part).build()
        # mutate the item's hash + content
        part.items["i1"] = {"hash": "h2", "docs": [("a", "alphaedited", "p2")]}
        stats = _builder(store, part).build()
        assert stats["changed"] == 1
        assert stats["version"] == 2
        assert "fake:a" in store.search_lexical("alphaedited", partition="fake")
        assert store.search_lexical("alpha", partition="fake") == {} or \
            "fake:a" not in store.search_lexical("alphaXYZ", partition="fake")

    def test_deleted_item_pruned(self, store):
        part = FakePartition({
            "i1": {"hash": "h1", "docs": [("a", "alpha", "p")]},
            "i2": {"hash": "h2", "docs": [("b", "beta", "p")]},
        })
        _builder(store, part).build()
        assert store.doc_count("fake") == 2
        del part.items["i2"]
        stats = _builder(store, part).build()
        assert stats["deleted"] == 1
        assert store.doc_count("fake") == 1
        assert store.vector_count("fake", "content") == 1  # cascade

    def test_force_reindexes_all(self, store):
        part = FakePartition({"i1": {"hash": "h1", "docs": [("a", "alpha", "p")]}})
        _builder(store, part).build()
        stats = _builder(store, part).build(force=True)
        assert stats["changed"] == 1  # forced despite unchanged hash

    def test_encode_unavailable_still_indexes_lexical(self, store):
        part = FakePartition({"i1": {"hash": "h1", "docs": [("a", "alpha", "p")]}})
        stats = _builder(store, part, encoder=FakeEncoder(down=True)).build()
        assert stats["doc_count"] == 1            # docs indexed
        assert store.vector_count("fake", "content") == 0  # no vectors (service down)
        assert "fake:a" in store.search_lexical("alpha", partition="fake")  # lexical OK

    def test_changed_vectors_are_immediate_by_default(self, store):
        part = FakePartition({
            "i1": {"hash": "h1", "docs": [("a", "alpha", "alpha passage")]},
            "i2": {"hash": "h2", "docs": [("b", "beta", "beta passage")]},
        })
        encoder = RecordingEncoder()

        _builder(store, part, encoder=encoder).build()

        assert encoder.document_calls == [["alpha passage"], ["beta passage"]]

    def test_deferred_changed_vectors_use_global_resume_batches(
        self, tmp_path, monkeypatch,
    ):
        items = {
            f"i{n}": {
                "hash": f"h{n}",
                "docs": [(f"d{n}", f"name{n}", f"text {n}")],
            }
            for n in range(5)
        }
        part = FakePartition(items)
        default_store = IndexStore(tmp_path / "default.db")
        deferred_store = IndexStore(tmp_path / "deferred.db")
        default_encoder = RecordingEncoder()
        deferred_encoder = RecordingEncoder()
        callback_observation = {}
        monkeypatch.setattr(IndexBuilder, "_ENCODE_MISSING_BATCH", 2)

        _builder(default_store, part, encoder=default_encoder).build()
        _builder(deferred_store, part, encoder=deferred_encoder).build(
            defer_changed_vectors=True,
            after_build=lambda stats: callback_observation.update(
                vector_count=deferred_store.vector_count("fake", "content"),
                docs_indexed=stats["docs_indexed"],
            ),
        )

        assert [len(call) for call in default_encoder.document_calls] == [1] * 5
        assert [len(call) for call in deferred_encoder.document_calls] == [2, 2, 1]
        default_vectors = default_store.load_all_vectors("fake", "content")
        deferred_vectors = deferred_store.load_all_vectors("fake", "content")
        assert default_vectors is not None and deferred_vectors is not None
        np.testing.assert_array_equal(default_vectors[0], deferred_vectors[0])
        assert default_vectors[1] == deferred_vectors[1]
        assert callback_observation == {"vector_count": 5, "docs_indexed": 5}

    def test_deferred_vector_build_resumes_after_lexical_progress(self, store):
        class InterruptingPartition(FakePartition):
            fail_item = "i2"

            def parse(self, item_id):
                if item_id == self.fail_item:
                    raise RuntimeError("synthetic parse interruption")
                return super().parse(item_id)

        part = InterruptingPartition({
            "i1": {"hash": "h1", "docs": [("a", "alpha", "alpha passage")]},
            "i2": {"hash": "h2", "docs": [("b", "beta", "beta passage")]},
        })
        encoder = RecordingEncoder()
        builder = _builder(store, part, encoder=encoder)

        with pytest.raises(RuntimeError, match="synthetic parse interruption"):
            builder.build(defer_changed_vectors=True)

        assert set(store.get_indexed_items("fake")) == {"i1"}
        assert "fake:a" in store.search_lexical("alpha", partition="fake")
        assert store.vector_count("fake", "content") == 0
        assert encoder.document_calls == []

        part.fail_item = ""
        recovered = builder.build(defer_changed_vectors=True)
        assert recovered["changed"] == 1
        assert set(store.get_indexed_items("fake")) == {"i1", "i2"}
        assert store.vector_count("fake", "content") == 2
        assert encoder.document_calls == [["alpha passage", "beta passage"]]

    def test_pooled_projection_build(self, store):
        class PooledPartition(FakePartition):
            def projection_schema(self):
                return {"aliases": ProjectionSpec(kind=ProjectionKind.LABEL, pool="max")}

            def parse(self, item_id):
                return [Document(
                    doc_id="fake:a", partition="fake", fields={"name": "alpha"},
                    projections={"aliases": Projection(text=["a1", "a2", "a3"])},
                )]
        part = PooledPartition({"i1": {"hash": "h1", "docs": [("a", "alpha", "")]}})
        _builder(store, part).build()
        # 3 alias sub-vectors for the one doc
        loaded = store.load_all_vectors("fake", "aliases")
        assert loaded is not None and loaded[0].shape[0] == 3
        assert store.vector_count("fake", "aliases") == 1  # distinct docs

    def test_build_with_lock_completes(self, store):
        part = FakePartition({"i1": {"hash": "h1", "docs": [("a", "alpha", "p")]}})
        b = IndexBuilder(store, FakeEncoder(), part, residents=ResidentCacheRegistry(), use_lock=True)
        assert b.build()["doc_count"] == 1


class TestConcurrencySafety:
    def test_build_holds_db_gate_and_partition_lock(self, store):
        # The builder must hold BOTH the DB-wide writer gate and its partition lock
        # while building (SQLite = one writer per DB; partitions share the DB), and
        # release both afterwards. Observe mid-build via the partition's discover().
        db = store.db_path
        gate = db.parent / f"{db.name}.build.lock"
        mine = db.parent / f"{db.name}.fake.lock"
        seen = {}

        class Spy(FakePartition):
            def discover(self):
                seen["gate"] = gate.exists()
                seen["mine"] = mine.exists()
                return super().discover()

        part = Spy({"i1": {"hash": "h1", "docs": [("a", "alpha", "p")]}})
        IndexBuilder(
            store, FakeEncoder(), part,
            residents=ResidentCacheRegistry(), use_lock=True,
        ).build()
        assert seen == {"gate": True, "mine": True}
        assert not gate.exists() and not mine.exists()  # both released

    def test_encode_missing_writes_in_batches(self, store, monkeypatch):
        # Backfill must be analyze-a-little-write-a-little: vectors land in bounded
        # batches (durable progress + short writer holds), never one giant commit.
        items = {
            f"i{n}": {"hash": f"h{n}", "docs": [(f"d{n}", f"name{n}", f"text {n}")]}
            for n in range(5)
        }
        part = FakePartition(items)
        _builder(store, part, encoder=FakeEncoder(down=True)).build()  # docs, no vectors
        assert store.vector_count("fake", "content") == 0

        monkeypatch.setattr(IndexBuilder, "_ENCODE_MISSING_BATCH", 2)
        calls: list[int] = []
        orig = store.upsert_vectors
        monkeypatch.setattr(
            store, "upsert_vectors",
            lambda projection, rows: (calls.append(len(rows)), orig(projection, rows))[1],
        )
        _builder(store, part).build()  # unchanged items → pure backfill resume
        assert store.vector_count("fake", "content") == 5
        assert calls == [2, 2, 1]  # batched, not one giant write


class TestPartitionRegistry:
    def test_lazy_register_and_get(self):
        reg = PartitionRegistry()
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return FakePartition({})

        reg.register("fake", factory)
        assert calls["n"] == 0          # lazy — not built at register
        p = reg.get("fake")
        assert calls["n"] == 1
        assert reg.get("fake") is p     # cached
        assert reg.names() == ["fake"]

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            PartitionRegistry().get("missing")

    def test_accessors(self):
        part = FakePartition({})
        assert get_change_key(part) == "hash"
        assert "content" in get_projection_schema(part)
        # a partition without optional methods
        class Bare:
            name = "bare"
            change_key = "mtime"
        assert get_projection_schema(Bare()) == {}
        assert hydrate(Bare(), ["x"]) == ["x"]  # default passthrough
