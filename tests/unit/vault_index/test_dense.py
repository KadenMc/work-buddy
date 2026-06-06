"""Tests for the vault-index dense encode loop (`dense.py::build_vectors`).

All tests mock the encoder — no real model load.
"""
from __future__ import annotations

import numpy as np
import pytest

from work_buddy.vault_index import dense
from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.chunker import chunk_markdown


@pytest.fixture
def fake_encoder(monkeypatch):
    calls: list[str] = []

    def _fake(texts, *, batch_size=32, kind="passage"):
        calls.extend(texts)
        return np.full((len(texts), 768), 0.1, dtype=np.float32)

    monkeypatch.setattr(dense, "_encode_bulk_direct", _fake)
    return calls


def _cfg(tmp_path) -> dict:
    return {"vault_index": {"db_path": str(tmp_path / "vault-index.db")}}


def _seed(cfg, items):
    conn = vstore.get_connection(cfg)
    try:
        for item_id, vault_id, md in items:
            vstore.upsert_chunks(conn, chunk_markdown(md, source_path=item_id),
                                 item_id=item_id, vault_id=vault_id)
    finally:
        conn.close()


def _vcount(cfg) -> int:
    conn = vstore.get_connection(cfg)
    try:
        return vstore.vector_count(conn)
    finally:
        conn.close()


def test_cold_encode_all(tmp_path, fake_encoder):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# A\n\nbody a\n"), ("v/b.md", "v", "# B\n\nbody b\n")])
    stats = dense.build_vectors(cfg)
    assert stats["status"] == "ok"
    assert stats["vectors_new"] == stats["vectors_total"] > 0
    assert stats["dims"] == 768
    assert _vcount(cfg) == stats["vectors_total"]


def test_incremental_only_missing(tmp_path, fake_encoder):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# A\n\nbody a\n")])
    dense.build_vectors(cfg)
    _seed(cfg, [("v/b.md", "v", "# B\n\nbody b\n")])
    fake_encoder.clear()
    stats = dense.build_vectors(cfg)
    assert stats["vectors_new"] == len(fake_encoder) > 0  # only b's chunks


def test_up_to_date_does_not_call_encoder(tmp_path, fake_encoder):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# A\n\nbody a\n")])
    dense.build_vectors(cfg)
    fake_encoder.clear()
    stats = dense.build_vectors(cfg)
    assert stats["status"] == "up_to_date"
    assert stats["vectors_new"] == 0
    assert fake_encoder == []


def test_force_reencodes_all(tmp_path, fake_encoder):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# A\n\nbody a\n")])
    dense.build_vectors(cfg)
    total = _vcount(cfg)
    fake_encoder.clear()
    stats = dense.build_vectors(cfg, force=True)
    assert stats["vectors_new"] == total > 0
    assert len(fake_encoder) == total


def test_deleted_chunk_not_reencoded(tmp_path, fake_encoder):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# A\n\nbody a\n"), ("v/b.md", "v", "# B\n\nbody b\n")])
    dense.build_vectors(cfg)
    before = _vcount(cfg)
    conn = vstore.get_connection(cfg)
    try:
        vstore.delete_item_chunks(conn, "v/b.md")  # CASCADE drops b's vectors
    finally:
        conn.close()
    assert _vcount(cfg) < before
    fake_encoder.clear()
    stats = dense.build_vectors(cfg)
    assert stats["vectors_new"] == 0  # nothing new
    assert fake_encoder == []


def test_no_chunks(tmp_path, fake_encoder):
    cfg = _cfg(tmp_path)
    vstore.get_connection(cfg).close()  # empty schema
    stats = dense.build_vectors(cfg)
    assert stats["status"] == "up_to_date"
    assert _vcount(cfg) == 0


def test_checkpoints_and_heartbeat(tmp_path, fake_encoder, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, [(f"v/{i}.md", "v", f"# H{i}\n\nbody {i}\n") for i in range(6)])
    monkeypatch.setattr(dense, "CHECKPOINT_ROWS", 2)
    upserts: list[int] = []
    real = dense.store.upsert_vectors
    monkeypatch.setattr(dense.store, "upsert_vectors",
                        lambda *a, **k: (upserts.append(1), real(*a, **k))[1])
    cps: list[int] = []
    dense.build_vectors(cfg, on_checkpoint=lambda: cps.append(1))
    assert len(upserts) > 1            # multiple batches committed
    assert len(cps) == len(upserts)    # heartbeat fired per batch


def test_resume_from_partial(tmp_path, fake_encoder):
    cfg = _cfg(tmp_path)
    _seed(cfg, [("v/a.md", "v", "# A\n\nbody a\n"), ("v/b.md", "v", "# B\n\nbody b\n")])
    conn = vstore.get_connection(cfg)
    try:
        a_ids = [c["doc_id"] for c in vstore.load_chunks(conn, item_id="v/a.md")]
        vstore.upsert_vectors(conn, a_ids, np.full((len(a_ids), 768), 0.2, np.float32))
    finally:
        conn.close()
    fake_encoder.clear()
    stats = dense.build_vectors(cfg)
    assert stats["vectors_new"] == len(fake_encoder) > 0  # only b's remainder
    assert stats["vectors_total"] == len(a_ids) + stats["vectors_new"]
