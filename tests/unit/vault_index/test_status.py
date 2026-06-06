"""vault_index.status.index_status — per-vault + whole-store counts."""
from __future__ import annotations

import numpy as np

from work_buddy.vault_index import status as vstatus
from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.chunker import chunk_markdown


def _cfg(tmp_path) -> dict:
    return {"vault_index": {"db_path": str(tmp_path / "vi.db")}}


def _seed_chunk(conn, vault_id, item_id):
    vstore.upsert_chunks(conn, chunk_markdown(f"# H\n\nbody of {item_id}\n", source_path=item_id),
                         item_id=item_id, vault_id=vault_id)


def test_no_index_before_build(tmp_path):
    assert vstatus.index_status(_cfg(tmp_path))["status"] == "no_index"


def test_configured_vaults_dict_shape():
    # A2: vaults is a DICT keyed by id (not a list).
    assert vstatus._configured_vaults({"vault_index": {"vaults": {"a": {"path": "/a"}}}}) == {"a": {"path": "/a"}}
    assert vstatus._configured_vaults({"vault_index": {"vaults": {}}}) == {}   # default mode
    assert vstatus._configured_vaults({"vault_index": {}}) == {}
    assert vstatus._configured_vaults({"vault_index": {"vaults": ["x"]}}) == {}  # legacy list → default mode


def test_effective_vault_configs_default_and_orphan(tmp_path):
    # A3: default-mode vault is is_default+in_config; a chunk-bearing vault absent
    # from config (and not the default) is an orphan.
    cfg = {"vault_index": {"db_path": str(tmp_path / "vi.db"), "vaults": {}},
           "vault_root": str(tmp_path)}
    conn = vstore.get_connection(cfg)
    try:
        _seed_chunk(conn, "vault", "vault/a.md")
        _seed_chunk(conn, "ghost", "ghost/b.md")
    finally:
        conn.close()
    rows = {r["id"]: r for r in vstatus.effective_vault_configs(cfg)}
    assert rows["vault"]["is_default"] is True and rows["vault"]["in_config"] is True
    assert rows["vault"]["path"] == str(tmp_path) and rows["vault"]["include"] == ["**/*.md"]
    assert rows["vault"]["chunk_count"] >= 1
    assert rows["ghost"]["is_default"] is False and rows["ghost"]["in_config"] is False


def test_effective_vault_configs_explicit(tmp_path):
    cfg = {"vault_index": {"db_path": str(tmp_path / "vi.db"),
                           "vaults": {"docs": {"path": str(tmp_path), "exclude": ["x/**"]}}}}
    conn = vstore.get_connection(cfg)
    try:
        _seed_chunk(conn, "docs", "docs/a.md")
    finally:
        conn.close()
    rows = {r["id"]: r for r in vstatus.effective_vault_configs(cfg)}
    assert rows["docs"]["is_default"] is False and rows["docs"]["in_config"] is True
    assert rows["docs"]["exclude"] == ["x/**"]


def test_counts_and_per_vault_breakdown(tmp_path):
    cfg = _cfg(tmp_path)
    conn = vstore.get_connection(cfg)
    try:
        for vid, n in (("A", 0), ("A", 1), ("B", 2)):
            item = f"{vid}/{n}.md"
            chunks = chunk_markdown(f"# H{n}\n\nbody {n} text here\n", source_path=item)
            vstore.upsert_chunks(conn, chunks, item_id=item, vault_id=vid)
            vstore.mark_item_indexed(conn, item, mtime=1.0, vault_id=vid, chunk_count=len(chunks))
        # Encode ONLY vault A's chunks → vault B stays pending.
        a_docs = {r["doc_id"] for r in conn.execute("SELECT doc_id FROM chunks WHERE vault_id='A'")}
        pending = [(d, t) for d, t in vstore.chunks_to_encode(conn) if d in a_docs]
        vstore.upsert_vectors(
            conn, [d for d, _ in pending],
            np.random.rand(len(pending), 768).astype(np.float32),
        )
    finally:
        conn.close()

    st = vstatus.index_status(cfg)
    assert st["status"] == "ok"
    assert st["size_on_disk_mb"] > 0
    assert set(st["vaults"]) == {"A", "B"}
    assert st["vaults"]["A"]["pending"] == 0          # A fully encoded
    assert st["vaults"]["A"]["vector_count"] >= 2
    assert st["vaults"]["B"]["pending"] >= 1          # B not encoded
    assert st["vaults"]["B"]["vector_count"] == 0
    assert st["vaults"]["A"]["file_count"] == 2
    # whole-store totals
    assert st["total_chunks"] == sum(v["chunk_count"] for v in st["vaults"].values())


def test_unreachable_health_from_meta(tmp_path):
    cfg = _cfg(tmp_path)
    conn = vstore.get_connection(cfg)
    try:
        item = "A/0.md"
        vstore.upsert_chunks(conn, chunk_markdown("# H\n\nbody text\n", source_path=item),
                             item_id=item, vault_id="A")
        vstore.set_meta(conn, "unreachable_warned:A", "2026-06-05T00:00:00")  # flagged offline
    finally:
        conn.close()
    st = vstatus.index_status(cfg)
    assert st["vaults"]["A"]["health"] == "unreachable"
