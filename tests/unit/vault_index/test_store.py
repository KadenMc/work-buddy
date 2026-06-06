"""Tests for the vault-index SQLite chunk store."""
from __future__ import annotations

import pytest

from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.chunker import chunk_markdown

# A doc that exercises dup_index AND split_index so the identity key is tested
# across both axes.
_DOC = (
    "Preamble prose before any heading.\n\n"
    "# P\n\n## Foo\n\nFirst foo body.\n\n"
    "## Foo\n\n" + ("This is a sentence that repeats. " * 60) + "\n"
)


@pytest.fixture
def conn(tmp_path):
    cfg = {"vault_index": {"db_path": str(tmp_path / "vi.db")}}
    c = vstore.get_connection(cfg)
    try:
        yield c
    finally:
        c.close()


def test_round_trip_preserves_chunks(conn):
    chunks = chunk_markdown(_DOC, source_path="myvault/note.md", max_chars=1200)
    written = vstore.upsert_chunks(conn, chunks, item_id="note.md", vault_id="myvault")
    assert written == len(chunks)

    loaded = vstore.load_chunks(conn, item_id="note.md")
    assert len(loaded) == len(chunks)

    # Reconstruct and compare against the originals (keyed by the readable key).
    by_key = {c.key: c for c in chunks}
    for row in loaded:
        rebuilt = vstore.row_to_chunk(row)
        original = by_key[rebuilt.key]
        assert rebuilt.text == original.text
        assert rebuilt.heading_path == original.heading_path
        assert rebuilt.split_index == original.split_index
        assert rebuilt.split_count == original.split_count
        assert rebuilt.dup_index == original.dup_index
        # The stored derived columns still match the recomputed properties.
        assert row["chunk_key"] == original.key
        assert row["embed_input"] == original.embed_input


def test_doc_ids_unique_across_dup_and_split(conn):
    chunks = chunk_markdown(_DOC, source_path="myvault/note.md", max_chars=1200)
    vstore.upsert_chunks(conn, chunks, item_id="note.md", vault_id="myvault")
    # Every distinct chunk gets a distinct doc_id — the collision-safe identity.
    ids = {vstore.chunk_doc_id(c) for c in chunks}
    assert len(ids) == len(chunks)
    assert vstore.chunk_count(conn) == len(chunks)


def test_upsert_is_idempotent(conn):
    chunks = chunk_markdown(_DOC, source_path="myvault/note.md")
    vstore.upsert_chunks(conn, chunks, item_id="note.md", vault_id="myvault")
    n_first = vstore.chunk_count(conn)
    vstore.upsert_chunks(conn, chunks, item_id="note.md", vault_id="myvault")
    assert vstore.chunk_count(conn) == n_first  # INSERT OR REPLACE, no duplicates


def test_incremental_bookkeeping_round_trips(conn):
    vstore.mark_item_indexed(
        conn, "note.md", mtime=1234.5, vault_id="myvault", size=42, chunk_count=3
    )
    assert vstore.get_indexed_items(conn) == {"note.md": 1234.5}
    assert vstore.get_indexed_items(conn, vault_id="myvault") == {"note.md": 1234.5}
    assert vstore.get_indexed_items(conn, vault_id="other") == {}


def test_delete_item_chunks_removes_rows_and_item(conn):
    chunks = chunk_markdown(_DOC, source_path="myvault/note.md")
    vstore.upsert_chunks(conn, chunks, item_id="note.md", vault_id="myvault")
    vstore.mark_item_indexed(conn, "note.md", mtime=1.0, vault_id="myvault")
    assert vstore.chunk_count(conn) > 0

    deleted = vstore.delete_item_chunks(conn, "note.md")
    assert deleted == len(chunks)
    assert vstore.chunk_count(conn) == 0
    assert vstore.get_indexed_items(conn) == {}


def test_meta_round_trips(conn):
    assert vstore.get_meta(conn, "last_build") is None
    vstore.set_meta(conn, "last_build", "2026-06-04T00:00:00+00:00")
    assert vstore.get_meta(conn, "last_build") == "2026-06-04T00:00:00+00:00"


def test_vault_filter_isolates_chunks(conn):
    a = chunk_markdown("# A\n\nbody a\n", source_path="va/a.md")
    b = chunk_markdown("# B\n\nbody b\n", source_path="vb/b.md")
    vstore.upsert_chunks(conn, a, item_id="a.md", vault_id="va")
    vstore.upsert_chunks(conn, b, item_id="b.md", vault_id="vb")
    assert vstore.chunk_count(conn, vault_id="va") == len(a)
    assert vstore.chunk_count(conn, vault_id="vb") == len(b)
    assert {r["vault_id"] for r in vstore.load_chunks(conn, vault_id="va")} == {"va"}


def test_default_db_path_is_under_data_root_not_home_claude():
    # The design REQUIRES paths.resolve under data_root — not the IR engine's
    # out-of-tree ~/.claude path. Resolve without a config override.
    p = vstore._db_path({})
    assert p.name == "vault-index.db"
    assert p.parent.name == "db"
    assert ".claude" not in p.parts
