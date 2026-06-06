"""Tests for the vault-index FTS5 lexical index + `search_lexical`."""
from __future__ import annotations

from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.chunker import chunk_markdown


def _cfg(tmp_path) -> dict:
    return {"vault_index": {"db_path": str(tmp_path / "vault-index.db")}}


def _seed(conn, item_id, vault_id, md):
    vstore.upsert_chunks(conn, chunk_markdown(md, source_path=item_id),
                         item_id=item_id, vault_id=vault_id)


def test_search_lexical_finds_term(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# Methods\n\nwe resample the ECG signal\n")
        _seed(conn, "v/b.md", "v", "# Results\n\nthe model achieved high accuracy\n")
        hits = vstore.search_lexical(conn, "ECG")
        assert hits
        top = max(hits, key=hits.get)
        row = conn.execute("SELECT item_id FROM chunks WHERE doc_id=?", (top,)).fetchone()
        assert row["item_id"] == "v/a.md"
        assert 0 < hits[top] <= 1.0  # normalized
    finally:
        conn.close()


def test_search_lexical_no_match(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# H\n\nhello world\n")
        assert vstore.search_lexical(conn, "zzzznonexistent") == {}
    finally:
        conn.close()


def test_search_lexical_vault_filter(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "A/a.md", "A", "# H\n\nshared keyword here\n")
        _seed(conn, "B/b.md", "B", "# H\n\nshared keyword here\n")
        a_hits = vstore.search_lexical(conn, "keyword", vault_id="A")
        assert a_hits
        for d in a_hits:
            row = conn.execute("SELECT vault_id FROM chunks WHERE doc_id=?", (d,)).fetchone()
            assert row["vault_id"] == "A"
    finally:
        conn.close()


def test_raw_operators_do_not_raise(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# H\n\nsome content here\n")
        assert isinstance(vstore.search_lexical(conn, 'content AND "*(missing'), dict)
        assert isinstance(vstore.search_lexical(conn, "NEAR(foo bar)"), dict)
        assert isinstance(vstore.search_lexical(conn, "*"), dict)
    finally:
        conn.close()


def test_delete_removes_from_fts(tmp_path):
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# H\n\nuniquetokenxyz here\n")
        assert vstore.search_lexical(conn, "uniquetokenxyz")
        vstore.delete_item_chunks(conn, "v/a.md")
        assert vstore.search_lexical(conn, "uniquetokenxyz") == {}
    finally:
        conn.close()


def test_changed_file_updates_fts(tmp_path):
    # Tier-4a changed-file flow: delete_item_chunks then re-upsert → FTS reflects new text.
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# H\n\noldtokenaaa content\n")
        assert vstore.search_lexical(conn, "oldtokenaaa")
        vstore.delete_item_chunks(conn, "v/a.md")
        _seed(conn, "v/a.md", "v", "# H\n\nnewtokenbbb content\n")
        assert vstore.search_lexical(conn, "newtokenbbb")
        assert vstore.search_lexical(conn, "oldtokenaaa") == {}
    finally:
        conn.close()


def test_direct_reupsert_updates_fts(tmp_path):
    # Direct INSERT OR REPLACE (same doc_id, changed text) with no prior delete —
    # recursive_triggers must fire the delete trigger so no stale FTS entry survives.
    conn = vstore.get_connection(_cfg(tmp_path))
    try:
        _seed(conn, "v/a.md", "v", "# Same Heading\n\nfirsttokenccc body\n")
        chunks = chunk_markdown("# Same Heading\n\nsecondtokenddd body\n", source_path="v/a.md")
        vstore.upsert_chunks(conn, chunks, item_id="v/a.md", vault_id="v")
        assert vstore.search_lexical(conn, "secondtokenddd")
        assert vstore.search_lexical(conn, "firsttokenccc") == {}
    finally:
        conn.close()


def test_migrate_backfills_existing_chunks(tmp_path):
    # Mimic a DB whose chunks predate the FTS index: wipe the FTS content + clear the
    # built flag, reopen → _migrate must rebuild the index from the chunks table.
    cfg = _cfg(tmp_path)
    conn = vstore.get_connection(cfg)
    try:
        _seed(conn, "v/a.md", "v", "# H\n\nbackfilltokeneee here\n")
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
        conn.commit()
        vstore.set_meta(conn, "fts_built", "0")
        assert vstore.search_lexical(conn, "backfilltokeneee") == {}  # wiped
    finally:
        conn.close()
    conn2 = vstore.get_connection(cfg)  # _migrate backfills
    try:
        assert vstore.search_lexical(conn2, "backfilltokeneee")
    finally:
        conn2.close()
