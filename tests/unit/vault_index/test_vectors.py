"""Tests for vault-index dense-vector persistence (SQLite blob storage)."""
from __future__ import annotations

import numpy as np

from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.chunker import chunk_markdown


def _cfg(tmp_path) -> dict:
    return {"vault_index": {"db_path": str(tmp_path / "vault-index.db")}}


def _seed(conn, item_id, vault_id, md):
    vstore.upsert_chunks(
        conn, chunk_markdown(md, source_path=item_id),
        item_id=item_id, vault_id=vault_id,
    )


def test_upsert_load_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    conn = vstore.get_connection(cfg)
    try:
        _seed(conn, "v/a.md", "v", "# A\n\nbody a\n")
        doc_ids = [d for d, _ in vstore.chunks_to_encode(conn)]
        vecs = np.random.rand(len(doc_ids), 768).astype(np.float32)
        assert vstore.upsert_vectors(conn, doc_ids, vecs) == len(doc_ids)
        assert vstore.vector_count(conn) == len(doc_ids)

        loaded = vstore.load_all_vectors(conn)
        assert loaded is not None
        mat, ids = loaded
        assert sorted(ids) == sorted(doc_ids)
        assert mat.dtype == np.float32 and mat.shape == (len(doc_ids), 768)
        order = {d: i for i, d in enumerate(doc_ids)}
        for i, d in enumerate(ids):
            np.testing.assert_allclose(mat[i], vecs[order[d]], atol=1e-2)  # float16 trip
    finally:
        conn.close()


def test_load_all_vectors_none_when_empty(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        assert vstore.load_all_vectors(conn) is None
        assert vstore.vector_count(conn) == 0
    finally:
        conn.close()


def test_chunks_to_encode_only_missing(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# A\n\nbody a\n")
        _seed(conn, "v/b.md", "v", "# B\n\nbody b\n")
        pending = vstore.chunks_to_encode(conn)
        assert len(pending) >= 2
        first = pending[0][0]
        vstore.upsert_vectors(conn, [first], np.random.rand(1, 768).astype(np.float32))
        remaining = [d for d, _ in vstore.chunks_to_encode(conn)]
        assert first not in remaining
        assert len(remaining) == len(pending) - 1
    finally:
        conn.close()


def test_cascade_delete_chunk_drops_vector(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# A\n\nbody a\n")
        _seed(conn, "v/b.md", "v", "# B\n\nbody b\n")
        pending = vstore.chunks_to_encode(conn)
        vstore.upsert_vectors(conn, [d for d, _ in pending],
                              np.random.rand(len(pending), 768).astype(np.float32))
        before = vstore.vector_count(conn)
        vstore.delete_item_chunks(conn, "v/b.md")  # FK CASCADE drops b's vectors
        after = vstore.vector_count(conn)
        assert after < before
        # no orphans: every remaining vector still has a chunk
        mat, ids = vstore.load_all_vectors(conn)
        chunk_ids = {r["doc_id"] for r in conn.execute("SELECT doc_id FROM chunks")}
        assert set(ids) <= chunk_ids
    finally:
        conn.close()


def test_delete_all_vectors(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# A\n\nbody a\n")
        pending = vstore.chunks_to_encode(conn)
        vstore.upsert_vectors(conn, [d for d, _ in pending],
                              np.random.rand(len(pending), 768).astype(np.float32))
        assert vstore.vector_count(conn) > 0
        vstore.delete_all_vectors(conn)
        assert vstore.vector_count(conn) == 0
    finally:
        conn.close()


def test_no_sidecar_npz_created(tmp_path):
    cfg = _cfg(tmp_path)
    conn = vstore.get_connection(cfg)
    try:
        _seed(conn, "v/a.md", "v", "# A\n\nbody a\n")
        pending = vstore.chunks_to_encode(conn)
        vstore.upsert_vectors(conn, [d for d, _ in pending],
                              np.random.rand(len(pending), 768).astype(np.float32))
    finally:
        conn.close()
    assert list(tmp_path.glob("*.npz")) == []  # vectors live IN the DB
