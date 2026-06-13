"""The include_orphaned search filter — exclude retained-but-source-gone docs
(``lifecycle_state="orphaned"``) for a live-only view, across lexical / dense / hybrid
and the store SQL builders. tmp_path store, FakeEncoder, no embedding service.

Orphaned docs are created via parse-time metadata so this stays independent of the
retention build path (which sets the stamp in real use)."""
from __future__ import annotations

import numpy as np
import pytest

from work_buddy.index.build import IndexBuilder
from work_buddy.index.config import PartitionConfig
from work_buddy.index.model import (
    Document, ItemRef, Projection, ProjectionKind, ProjectionSpec, Query,
)
from work_buddy.index.resident import ResidentCacheRegistry
from work_buddy.index.search import HybridSearcher
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
        self.items = items  # {id: {"hash": str, "orphaned": bool}}

    def projection_schema(self):
        return {"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def discover(self):
        return [ItemRef(item_id=i, content_hash=v["hash"]) for i, v in self.items.items()]

    def parse(self, item_id):
        v = self.items[item_id]
        meta = {"lifecycle_state": "orphaned"} if v.get("orphaned") else {}
        return [Document(
            doc_id=f"fake:{item_id}", partition="fake",
            fields={"name": item_id, "body": "shared term here"},
            display_text=item_id, metadata=meta,
            projections={"content": Projection(text="shared term passage")},
        )]


@pytest.fixture
def built(tmp_path):
    store = IndexStore(tmp_path / "orph.db")
    part = FakePartition({"live": {"hash": "1"}, "ghost": {"hash": "1", "orphaned": True}})
    IndexBuilder(
        store, FakeEncoder(), part, residents=ResidentCacheRegistry(), use_lock=False,
    ).build()
    return store


def _searcher(store):
    return HybridSearcher(
        store, FakeEncoder(), partition="fake",
        projection_schema={"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)},
        cfg=PartitionConfig(name="fake"), residents=ResidentCacheRegistry(),
    )


class TestSearchFilter:
    def test_includes_orphaned_by_default(self, built):
        ids = {h.doc_id for h in _searcher(built).search(
            Query(text="shared", method="hybrid", include_orphaned=True))}
        assert ids == {"fake:live", "fake:ghost"}

    def test_excludes_orphaned_when_off(self, built):
        ids = {h.doc_id for h in _searcher(built).search(
            Query(text="shared", method="hybrid", include_orphaned=False))}
        assert ids == {"fake:live"}

    @pytest.mark.parametrize("method", ["lexical", "dense", "hybrid"])
    def test_exclude_across_methods(self, built, method):
        ids = {h.doc_id for h in _searcher(built).search(
            Query(text="shared", method=method, include_orphaned=False))}
        assert "fake:ghost" not in ids
        assert "fake:live" in ids

    def test_search_many_excludes(self, built):
        res = _searcher(built).search_many(["shared"], method="hybrid", include_orphaned=False)
        assert {h.doc_id for h in res[0]} == {"fake:live"}

    def test_search_many_includes_by_default(self, built):
        res = _searcher(built).search_many(["shared"], method="hybrid")
        assert {h.doc_id for h in res[0]} == {"fake:live", "fake:ghost"}


class TestStoreExclusion:
    def test_search_lexical_exclude(self, built):
        incl = built.search_lexical("shared", partition="fake", exclude_orphaned=False)
        excl = built.search_lexical("shared", partition="fake", exclude_orphaned=True)
        assert "fake:ghost" in incl and "fake:live" in incl
        assert "fake:ghost" not in excl and "fake:live" in excl

    def test_load_documents_exclude(self, built):
        assert "fake:ghost" in built.load_documents(partition="fake", exclude_orphaned=False)
        excl = built.load_documents(partition="fake", exclude_orphaned=True)
        assert "fake:ghost" not in excl and "fake:live" in excl
