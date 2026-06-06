"""A1 — ir.store.index_status counts vectors across a source's projection files.

Multi-projection sources (task_note → line + body .npz) must report their distinct
vectored docs, not 0 (the old code read only the legacy single ``<source>.npz``).
"""
from __future__ import annotations

import numpy as np
import pytest

from work_buddy.ir.sources.base import Document
from work_buddy.ir.store import _npz_path, get_connection, index_status, upsert_documents


@pytest.fixture
def tmp_ir_db(tmp_path, monkeypatch):
    db = tmp_path / "ir.db"
    monkeypatch.setattr("work_buddy.ir.store._db_path", lambda cfg=None: db)
    return db


def _save_npz(path, doc_ids):
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(str(path), vectors=np.zeros((len(doc_ids), 8), dtype=np.float16),
             doc_ids=np.array(doc_ids))


def _seed(source, n):
    conn = get_connection()
    try:
        docs = [
            Document(doc_id=f"{source}:{i}", source=source, fields={"body": f"t {i}"},
                     dense_text=f"t {i}", display_text=f"t {i}", metadata={"session_id": "s"})
            for i in range(n)
        ]
        upsert_documents(conn, docs, item_id="seed")
    finally:
        conn.close()
    return [f"{source}:{i}" for i in range(n)]


def test_multiprojection_source_sums_distinct_docs(tmp_ir_db):
    # task_note declares two projections (line, body); each doc is in BOTH files.
    ids = _seed("task_note", 3)
    _save_npz(_npz_path(None, source="task_note", projection="line"), ids)
    _save_npz(_npz_path(None, source="task_note", projection="body"), ids)

    vinfo = index_status().get("vectors", {}).get("task_note")
    assert vinfo is not None
    assert vinfo["vector_count"] == 3       # distinct docs, NOT 6 (3 docs × 2 projections)
    assert vinfo["pending_eligible"] == 0
    assert len(vinfo["vector_files"]) == 2


def test_legacy_single_projection_unchanged(tmp_ir_db):
    ids = _seed("conversation", 2)  # conversation has no projection schema
    _save_npz(_npz_path(None, source="conversation"), ids)

    vinfo = index_status().get("vectors", {}).get("conversation")
    assert vinfo is not None
    assert vinfo["vector_count"] == 2 and vinfo["pending_eligible"] == 0
