"""Opt-in scale regression for consolidated-index FTS maintenance.

The production failure involved roughly 215k FTS rows.  Keeping that corpus in the
ordinary CI suite would add unnecessary I/O on every change, so this test is opt-in:

    WB_RUN_INDEX_SCALE_TESTS=1 pytest tests/component/test_index_store_fts_scale.py

The always-on unit suite separately asserts the rowid-aligned schema/query plan and
all deletion-path correctness.  This test proves the same design remains bounded at
the observed production scale.
"""

from __future__ import annotations

import os
import time

import pytest

from work_buddy.index.store import IndexStore


pytestmark = pytest.mark.skipif(
    os.environ.get("WB_RUN_INDEX_SCALE_TESTS") != "1",
    reason="set WB_RUN_INDEX_SCALE_TESTS=1 to run the 200k-row FTS regression",
)


def test_deleting_large_item_is_bounded_at_200k_fts_rows(tmp_path):
    store = IndexStore(tmp_path / "scale.db")
    store.prepare_schema()
    conn = store._connect()
    try:
        now = "2026-01-01T00:00:00+00:00"
        batch_size = 5000
        corpus_size = 200_000
        changed_size = 2_000
        for start in range(0, corpus_size, batch_size):
            rows = []
            for index in range(start, min(start + batch_size, corpus_size)):
                is_changed = index < changed_size
                rows.append(
                    (
                        f"conversation:{index}",
                        "conversation",
                        "changed-session" if is_changed else f"stable:{index // 100}",
                        "{}",
                        "{}",
                        "",
                        "{}",
                        "",
                        now,
                        "changedtoken" if is_changed else "survivortoken",
                        f"body {index}",
                        "",
                    )
                )
            conn.executemany(
                "INSERT INTO documents "
                "(doc_id, partition, item_id, fields, projections, display_text, "
                "metadata, content_hash, indexed_at, title, body, tags) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        conn.commit()
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == corpus_size
        assert conn.execute("SELECT count(*) FROM doc_fts").fetchone()[0] == corpus_size
    finally:
        conn.close()

    started = time.perf_counter()
    deleted = store.delete_item_docs("changed-session", partition="conversation")
    elapsed = time.perf_counter() - started
    print(f"deleted {deleted:,} rows from {corpus_size:,}-row FTS corpus in {elapsed:.3f}s")

    assert deleted == changed_size
    # Rowid trigger deletion is normally well below one second on this corpus.  Ten
    # seconds leaves broad CI/antivirus headroom while still rejecting the legacy
    # per-document full scan, which took hours for a comparable production batch.
    assert elapsed < 10.0, f"2k-row deletion from 200k FTS corpus took {elapsed:.2f}s"
    assert store.search_lexical("changedtoken", partition="conversation") == {}
    assert store.search_lexical("survivortoken", partition="conversation")
