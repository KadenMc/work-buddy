"""Tests for the resident dense-vector matrix cache."""
from __future__ import annotations

import numpy as np

from work_buddy.vault_index import dense_cache
from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.chunker import chunk_markdown


def setup_function():
    dense_cache.invalidate()  # the cache is a module global — reset between tests


def _cfg(tmp_path) -> dict:
    return {"vault_index": {"db_path": str(tmp_path / "vault-index.db")}}


def _seed_with_vectors(cfg, n_docs=3):
    conn = vstore.get_connection(cfg)
    try:
        for i in range(n_docs):
            vstore.upsert_chunks(
                conn, chunk_markdown(f"# H{i}\n\nbody {i}\n", source_path=f"v/{i}.md"),
                item_id=f"v/{i}.md", vault_id="v",
            )
        pending = vstore.chunks_to_encode(conn)
        vstore.upsert_vectors(conn, [d for d, _ in pending],
                              np.random.rand(len(pending), 768).astype(np.float32))
    finally:
        conn.close()


def test_lazy_load_and_cache_hit(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_with_vectors(cfg)
    loads: list[int] = []
    real = vstore.load_all_vectors
    monkeypatch.setattr(dense_cache.store, "load_all_vectors",
                        lambda conn: (loads.append(1), real(conn))[1])
    m1 = dense_cache.get_matrix(cfg)
    assert m1 is not None and m1[0].shape[1] == 768
    dense_cache.get_matrix(cfg)  # cache hit
    assert len(loads) == 1


def test_version_bump_reloads(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_with_vectors(cfg)
    loads: list[int] = []
    real = vstore.load_all_vectors
    monkeypatch.setattr(dense_cache.store, "load_all_vectors",
                        lambda conn: (loads.append(1), real(conn))[1])
    dense_cache.get_matrix(cfg)
    conn = vstore.get_connection(cfg)
    try:
        vstore.set_meta(conn, dense_cache.VERSION_KEY, "99")
    finally:
        conn.close()
    dense_cache.get_matrix(cfg)  # version changed → reload
    assert len(loads) == 2


def test_release_if_idle(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_with_vectors(cfg)
    dense_cache.get_matrix(cfg)
    assert dense_cache.is_cached()
    assert dense_cache.release_if_idle(ttl_s=-1) is True  # negative ttl → always idle
    assert not dense_cache.is_cached()


def test_none_when_no_vectors(tmp_path):
    cfg = _cfg(tmp_path)
    vstore.get_connection(cfg).close()  # empty store
    assert dense_cache.get_matrix(cfg) is None
    assert not dense_cache.is_cached()
