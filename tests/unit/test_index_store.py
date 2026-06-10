"""Tests for index/store.py — IndexStore SQLite + FTS5 + blob vectors.

Uses a tmp_path DB; no embedding service. Vectors are synthetic numpy arrays.
"""

from __future__ import annotations

import numpy as np
import pytest

from work_buddy.index.model import Document, Projection
from work_buddy.index.store import IndexStore


@pytest.fixture
def store(tmp_path):
    return IndexStore(tmp_path / "t-index.db")


def _doc(doc_id, partition="knowledge", *, name="", body="", tags="", meta=None, ts=None):
    return Document(
        doc_id=doc_id, partition=partition,
        fields={"name": name, "body": body, "tags": tags},
        display_text=f"{name}: {body}",
        metadata=meta or {},
        timestamp=ts,
    )


class TestUpsertAndLexical:
    def test_upsert_and_fts_search(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="Consent System", body="approval grants ttl"),
            _doc("knowledge:b", name="Telegram Bot", body="mobile notifications inline keyboard"),
        ], item_id="seed")
        hits = store.search_lexical("approval grants")
        assert "knowledge:a" in hits
        assert hits["knowledge:a"] > 0

    def test_title_weighted_over_body(self, store):
        # 'alpha' in a's title, in b's body. Default weights title(3) > body(1).
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", body="filler text"),
            _doc("knowledge:b", name="other", body="alpha appears in body only"),
        ], item_id="seed")
        hits = store.search_lexical("alpha")
        assert hits["knowledge:a"] >= hits["knowledge:b"]

    def test_partition_scoping(self, store):
        store.upsert_documents([_doc("knowledge:a", name="shared term")], item_id="i1")
        store.upsert_documents(
            [_doc("vault:a", partition="vault", name="shared term")], item_id="i2"
        )
        hits = store.search_lexical("shared", partition="vault")
        assert set(hits) == {"vault:a"}

    def test_scope_prefix(self, store):
        store.upsert_documents([
            _doc("knowledge:tasks/x", name="triage flow"),
            _doc("knowledge:obsidian/y", name="triage flow"),
        ], item_id="i")
        hits = store.search_lexical("triage", scope="knowledge:tasks/")
        assert set(hits) == {"knowledge:tasks/x"}

    def test_empty_query_returns_empty(self, store):
        store.upsert_documents([_doc("knowledge:a", name="x")], item_id="i")
        assert store.search_lexical("") == {}
        assert store.search_lexical("  !! ") == {}

    def test_fts_refreshed_on_reupsert(self, store):
        store.upsert_documents([_doc("knowledge:a", name="original")], item_id="i")
        assert "knowledge:a" in store.search_lexical("original")
        store.upsert_documents([_doc("knowledge:a", name="replaced")], item_id="i")
        assert store.search_lexical("original") == {}
        assert "knowledge:a" in store.search_lexical("replaced")


class TestMetadataFilter:
    def test_equality_filter(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", meta={"kind": "system"}),
            _doc("knowledge:b", name="alpha", meta={"kind": "directions"}),
        ], item_id="i")
        hits = store.search_lexical("alpha", filters={"kind": "system"})
        assert set(hits) == {"knowledge:a"}

    def test_set_membership_filter(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", meta={"kind": "system"}),
            _doc("knowledge:b", name="alpha", meta={"kind": "directions"}),
            _doc("knowledge:c", name="alpha", meta={"kind": "capability"}),
        ], item_id="i")
        hits = store.search_lexical("alpha", filters={"kind": ["system", "capability"]})
        assert set(hits) == {"knowledge:a", "knowledge:c"}

    def test_load_documents_with_filter(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", meta={"scope": "system"}),
            _doc("knowledge:b", name="beta", meta={"scope": "personal"}),
        ], item_id="i")
        docs = store.load_documents(partition="knowledge", filters={"scope": "personal"})
        assert set(docs) == {"knowledge:b"}
        assert docs["knowledge:b"]["fields"]["name"] == "beta"


class TestVectors:
    def test_blob_roundtrip(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="a"), _doc("knowledge:b", name="b"),
        ], item_id="i")
        rng = np.random.default_rng(0)
        v_a = rng.normal(size=8).astype(np.float32)
        v_b = rng.normal(size=8).astype(np.float32)
        n = store.upsert_vectors("content", [("knowledge:a", v_a), ("knowledge:b", v_b)])
        assert n == 2
        loaded = store.load_all_vectors("knowledge", "content")
        assert loaded is not None
        mat, doc_ids = loaded
        assert mat.shape == (2, 8)
        assert doc_ids == ["knowledge:a", "knowledge:b"]
        # float16 round-trip tolerance
        idx = doc_ids.index("knowledge:a")
        assert np.allclose(mat[idx], v_a, atol=1e-2)

    def test_load_vectors_empty(self, store):
        assert store.load_all_vectors("knowledge", "content") is None

    def test_fk_cascade_deletes_vectors(self, store):
        store.upsert_documents([_doc("knowledge:a", name="a")], item_id="item1")
        store.upsert_vectors("content", [("knowledge:a", np.ones(4, dtype=np.float32))])
        assert store.vector_count("knowledge", "content") == 1
        store.delete_item_docs("item1", partition="knowledge")
        # vectors cascade-deleted with the document
        assert store.vector_count("knowledge", "content") == 0
        assert store.doc_count("knowledge") == 0
        # FTS row gone too
        assert store.search_lexical("a", partition="knowledge") == {}

    def test_docs_missing_vectors_worklist(self, store):
        store.upsert_documents([
            Document(doc_id="knowledge:a", partition="knowledge", fields={"name": "a"},
                     projections={"content": Projection(text="alpha body")}),
            Document(doc_id="knowledge:b", partition="knowledge", fields={"name": "b"},
                     projections={"content": Projection(text="beta body")}),
        ], item_id="i")
        work = store.docs_missing_vectors("knowledge", "content")
        assert {d for d, _ in work} == {"knowledge:a", "knowledge:b"}
        # encode one → it drops off the work-list
        store.upsert_vectors("content", [("knowledge:a", np.ones(4, dtype=np.float32))])
        work2 = store.docs_missing_vectors("knowledge", "content")
        assert {d for d, _ in work2} == {"knowledge:b"}


class TestLedgerAndVersion:
    def test_indexed_items_ledger(self, store):
        store.mark_item_indexed("f.md", "knowledge", mtime=123.0, content_hash="abc", doc_count=2)
        items = store.get_indexed_items("knowledge")
        assert items["f.md"] == (123.0, "abc")

    def test_build_version_bump(self, store):
        assert store.build_version("knowledge") == 0
        assert store.bump_version("knowledge") == 1
        assert store.bump_version("knowledge") == 2
        assert store.build_version("knowledge") == 2
        # per-partition isolation
        assert store.build_version("vault") == 0

    def test_meta_roundtrip(self, store):
        assert store.get_meta("k") is None
        store.set_meta("k", "v")
        assert store.get_meta("k") == "v"

    def test_partitions_listing(self, store):
        store.upsert_documents([_doc("knowledge:a", name="a")], item_id="i")
        store.upsert_documents([_doc("vault:a", partition="vault", name="a")], item_id="j")
        assert store.partitions() == ["knowledge", "vault"]
