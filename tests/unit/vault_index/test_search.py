"""Tests for the vault-index hybrid search (`search.py`). Mocks the embedding client."""
from __future__ import annotations

import numpy as np
import pytest

from work_buddy.vault_index import dense_cache, search
from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.chunker import chunk_markdown


def setup_function():
    dense_cache.invalidate()


def _cfg(tmp_path) -> dict:
    return {"vault_index": {"db_path": str(tmp_path / "vault-index.db")}}


def _seed(cfg, items, *, with_vectors=True):
    conn = vstore.get_connection(cfg)
    try:
        for item_id, vault_id, md in items:
            vstore.upsert_chunks(conn, chunk_markdown(md, source_path=item_id),
                                 item_id=item_id, vault_id=vault_id)
        if with_vectors:
            pending = vstore.chunks_to_encode(conn)
            vecs = np.random.RandomState(0).rand(len(pending), 768).astype(np.float32)
            vstore.upsert_vectors(conn, [d for d, _ in pending], vecs)
    finally:
        conn.close()


@pytest.fixture
def mock_embed(monkeypatch):
    """Patch the (in-service-aware) query encoder to a fixed (1, 768) vector (service 'up')."""
    import work_buddy.ir.dense as dense
    monkeypatch.setattr(
        dense, "encode_query",
        lambda query, kind="passage": np.random.RandomState(1).rand(1, 768).astype(np.float32),
    )


def test_hybrid_returns_both_signals(tmp_path, mock_embed):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# Methods\n\nECG resampling at 250 Hz\n"),
                ("v/b.md", "v", "# Other\n\nunrelated text about cats\n")])
    results = search.search("ECG resampling", method="hybrid", cfg=cfg)
    assert results
    r = results[0]
    assert {"doc_id", "score", "bm25_score", "dense_score", "source",
            "display_text", "metadata"} <= set(r)
    assert r["source"] == "vault_index"
    assert r["metadata"].keys() >= {"source_path", "heading_path", "vault_id"}
    assert any(x["bm25_score"] > 0 for x in results)  # lexical contributed


def test_degrades_to_lexical_when_service_down(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# H\n\nuniquephrasezzz appears here\n")])
    import work_buddy.ir.dense as dense
    monkeypatch.setattr(dense, "encode_query", lambda *a, **k: None)  # service down
    results = search.search("uniquephrasezzz", method="hybrid", cfg=cfg)
    assert results
    assert results[0]["dense_score"] == 0.0  # no dense signal, no error


def test_method_lexical(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# H\n\nlexonlytoken here\n")], with_vectors=False)
    results = search.search("lexonlytoken", method="lexical", cfg=cfg)
    assert results and results[0]["bm25_score"] > 0


def test_method_dense(tmp_path, mock_embed):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# H\n\nsome body text\n")])
    results = search.search("anything", method="dense", cfg=cfg)
    assert results
    assert all(r["bm25_score"] == 0.0 for r in results)  # no lexical in dense-only


def test_empty_query(tmp_path):
    assert search.search("   ", cfg=_cfg(tmp_path)) == []


def test_empty_index(tmp_path, mock_embed):
    cfg = _cfg(tmp_path)
    vstore.get_connection(cfg).close()
    assert search.search("anything", cfg=cfg) == []
