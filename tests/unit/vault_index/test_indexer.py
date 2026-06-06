"""Tests for the vault-index incremental build loop.

The destructive-safety tests (unreachable-not-pruned, force-with-unreachable,
deleted/empty/moved reconciliation) are the load-bearing ones — they guard the
data-loss invariants in ``indexer.py``.
"""
from __future__ import annotations

import os
import shutil

import pytest

from work_buddy.vault_index import indexer
from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.source import FilesystemSource


@pytest.fixture(autouse=True)
def notifications(monkeypatch):
    """Capture (don't deliver) every notification the indexer emits."""
    captured: list[dict] = []

    def _capture(title, body, *, tags):
        captured.append({"title": title, "body": body, "tags": tags})

    monkeypatch.setattr(indexer, "_emit_notification", _capture)
    return captured


def _cfg(tmp_path, vaults: dict, *, exclude_folders=None) -> dict:
    return {
        "vault_index": {"vaults": vaults, "db_path": str(tmp_path / "vi.db")},
        "vault_root": "",
        "obsidian": {"exclude_folders": exclude_folders or [".obsidian", ".git", "node_modules"]},
    }


def _write(p, text: str = "# H\n\nsome body text here\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _bump_mtime(path, seconds: float = 100.0):
    st = path.stat()
    os.utime(path, (st.st_mtime + seconds, st.st_mtime + seconds))


def _indexed(cfg, vault_id: str):
    conn = vstore.get_connection(cfg)
    try:
        return set(vstore.get_indexed_items(conn, vault_id=vault_id))
    finally:
        conn.close()


def _count(cfg, vault_id=None):
    conn = vstore.get_connection(cfg)
    try:
        return vstore.chunk_count(conn, vault_id=vault_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Incremental correctness
# --------------------------------------------------------------------------

def test_first_build_indexes_all_new(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    _write(root / "b.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    stats = indexer.build_index(cfg)
    assert stats["files_new"] == 2
    assert stats["files_skipped"] == 0
    assert stats["chunks_total"] > 0
    assert _indexed(cfg, "vault") == {"vault/a.md", "vault/b.md"}


def test_unchanged_file_skipped(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    stats = indexer.build_index(cfg)
    assert stats["files_skipped"] == 1
    assert stats["files_new"] == 0


def test_changed_file_rechunked(tmp_path):
    root = tmp_path / "v"
    f = _write(root / "a.md", "# Old\n\nold body content\n")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    _write(f, "# New\n\nnew body content\n")
    _bump_mtime(f)
    stats = indexer.build_index(cfg)
    assert stats["files_changed"] == 1
    conn = vstore.get_connection(cfg)
    try:
        text = " ".join(c["text"] for c in vstore.load_chunks(conn, item_id="vault/a.md"))
    finally:
        conn.close()
    assert "New" in text and "Old" not in text  # stale chunks gone


def test_build_is_idempotent(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    _write(root / "b.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    s1 = indexer.build_index(cfg)
    s2 = indexer.build_index(cfg)
    assert s1["chunks_total"] == s2["chunks_total"]
    assert s2["files_skipped"] == 2
    assert s2["files_new"] == 0 and s2["files_deleted"] == 0


# --------------------------------------------------------------------------
# Destructive-safety guards (load-bearing)
# --------------------------------------------------------------------------

def test_deleted_file_pruned(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    fb = _write(root / "b.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    fb.unlink()
    stats = indexer.build_index(cfg)
    assert stats["files_deleted"] == 1
    assert _indexed(cfg, "vault") == {"vault/a.md"}
    conn = vstore.get_connection(cfg)
    try:
        assert vstore.load_chunks(conn, item_id="vault/b.md") == []
    finally:
        conn.close()


def test_unreachable_vault_not_pruned(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    _write(root / "b.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    before = _count(cfg, "vault")
    shutil.rmtree(root)  # vault gone → unreachable
    stats = indexer.build_index(cfg)
    assert stats["vaults_unreachable"] == 1
    assert stats["files_deleted"] == 0
    assert _count(cfg, "vault") == before  # KEPT — nothing pruned


def test_reachable_empty_vault_prunes_all(tmp_path, notifications):
    root = tmp_path / "v"
    _write(root / "a.md")
    _write(root / "b.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    for f in root.glob("*.md"):
        f.unlink()  # all notes deleted, dir still reachable
    stats = indexer.build_index(cfg)
    assert stats["files_deleted"] == 2
    assert _count(cfg, "vault") == 0
    assert any("mass-prune" in n["tags"] for n in notifications)  # surfaced


def test_reachable_vs_unreachable_distinguishable(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(a / "x.md")
    _write(b / "y.md")
    cfg = _cfg(tmp_path, {"A": {"path": str(a)}, "B": {"path": str(b)}})
    indexer.build_index(cfg)
    (a / "x.md").unlink()  # A reachable but empty → prune
    shutil.rmtree(b)       # B unreachable → keep
    indexer.build_index(cfg)
    assert _count(cfg, "A") == 0
    assert _count(cfg, "B") > 0


def test_file_moved_across_vaults(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    f = _write(a / "note.md")
    b.mkdir(parents=True)
    cfg = _cfg(tmp_path, {"A": {"path": str(a)}, "B": {"path": str(b)}})
    indexer.build_index(cfg)
    total_before = _count(cfg)
    shutil.move(str(f), str(b / "note.md"))  # move A -> B
    indexer.build_index(cfg)
    conn = vstore.get_connection(cfg)
    try:
        assert vstore.load_chunks(conn, item_id="A/note.md") == []
        assert vstore.load_chunks(conn, item_id="B/note.md") != []
    finally:
        conn.close()
    assert _count(cfg) == total_before  # chunk count conserved


def test_force_with_unreachable_vault_keeps_chunks(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(a / "x.md")
    _write(b / "y.md")
    cfg = _cfg(tmp_path, {"A": {"path": str(a)}, "B": {"path": str(b)}})
    indexer.build_index(cfg)
    b_before = _count(cfg, "B")
    shutil.rmtree(b)  # B unreachable during a force run
    stats = indexer.build_index(cfg, force=True)
    assert stats["vaults_unreachable"] == 1
    assert _count(cfg, "B") == b_before  # force did NOT nuke the offline vault
    assert _count(cfg, "A") > 0          # A rebuilt


def test_force_rebuilds_reachable_vault(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    _write(root / "b.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    stats = indexer.build_index(cfg, force=True)
    assert stats["files_new"] == 2  # all re-treated as new
    assert stats["files_skipped"] == 0


# --------------------------------------------------------------------------
# Resumability
# --------------------------------------------------------------------------

def test_crash_between_upsert_and_mark_resumes(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    # Simulate a crash: upsert the file's chunks but never mark it indexed.
    src = FilesystemSource(cfg)
    files, _ = src.discover()
    conn = vstore.get_connection(cfg)
    try:
        f = files[0]
        vstore.upsert_chunks(conn, src.parse(f.item_id), f.item_id, f.vault_id)
    finally:
        conn.close()
    # A normal build must resume cleanly.
    stats = indexer.build_index(cfg)
    assert stats["files_new"] == 1     # no indexed_items row → treated as new
    assert stats["files_deleted"] == 0  # never falsely pruned
    conn = vstore.get_connection(cfg)
    try:
        keys = [c["chunk_key"] for c in vstore.load_chunks(conn, item_id="vault/a.md")]
        assert len(keys) == len(set(keys))  # no duplicate chunks
        assert "vault/a.md" in vstore.get_indexed_items(conn, vault_id="vault")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Debounce
# --------------------------------------------------------------------------

def _unreachable_notifs(captured):
    return [n for n in captured if "unreachable" in n["tags"]]


def test_unreachable_warns_once(tmp_path, notifications):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    shutil.rmtree(root)
    indexer.build_index(cfg)
    indexer.build_index(cfg)  # still unreachable
    assert len(_unreachable_notifs(notifications)) == 1


def test_returning_reachable_then_unreachable_warns_again(tmp_path, notifications):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    shutil.rmtree(root)
    indexer.build_index(cfg)        # warn #1
    _write(root / "a.md")           # reachable again → clears the flag
    indexer.build_index(cfg)
    shutil.rmtree(root)
    indexer.build_index(cfg)        # warn #2
    assert len(_unreachable_notifs(notifications)) == 2


def test_force_does_not_reset_debounce(tmp_path, notifications):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    indexer.build_index(cfg)
    shutil.rmtree(root)
    indexer.build_index(cfg)              # warn #1 (flag set)
    indexer.build_index(cfg, force=True)  # still unreachable; must not re-warn
    assert len(_unreachable_notifs(notifications)) == 1


# --------------------------------------------------------------------------
# Stats / meta / CLI
# --------------------------------------------------------------------------

def test_stats_shape_and_meta(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    stats = indexer.build_index(cfg)
    for k in ("vaults", "files_new", "files_changed", "files_deleted",
              "files_skipped", "chunks_total", "vaults_unreachable", "build_time_s"):
        assert k in stats
    conn = vstore.get_connection(cfg)
    try:
        assert vstore.get_meta(conn, "last_build:vault_index")
    finally:
        conn.close()


def test_main_runs_and_prints(tmp_path, capsys, monkeypatch):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    from work_buddy.vault_index import __main__ as m
    monkeypatch.setattr(
        m, "build_all",
        lambda force=False, encode=True: indexer.build_all(cfg, force=force, encode=False),
    )
    rc = m.main(["--no-encode"])
    assert rc == 0
    assert "chunks_total" in capsys.readouterr().out
