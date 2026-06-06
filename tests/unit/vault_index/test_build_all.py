"""Tests for `indexer.build_all` — the lock-wrapped index + encode orchestration."""
from __future__ import annotations

import numpy as np
import pytest

from work_buddy.utils import index_lock
from work_buddy.vault_index import dense, indexer
from work_buddy.vault_index import store as vstore


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch):
    monkeypatch.setattr(indexer, "_emit_notification", lambda *a, **k: None)


@pytest.fixture
def fake_encoder(monkeypatch):
    monkeypatch.setattr(
        dense, "_encode_bulk_direct",
        lambda texts, **k: np.full((len(texts), 768), 0.1, np.float32),
    )


def _cfg(tmp_path, vaults) -> dict:
    return {
        "vault_index": {"vaults": vaults, "db_path": str(tmp_path / "vault-index.db")},
        "vault_root": "",
        "obsidian": {"exclude_folders": []},
    }


def _write(p, text="# H\n\nbody text here\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _vcount(cfg) -> int:
    conn = vstore.get_connection(cfg)
    try:
        return vstore.vector_count(conn)
    finally:
        conn.close()


def test_build_all_indexes_encodes_and_releases(tmp_path, fake_encoder):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    stats = indexer.build_all(cfg)
    assert stats["files_new"] == 1
    assert stats["vectors"]["vectors_new"] > 0
    assert _vcount(cfg) > 0
    assert index_lock.is_locked(vstore._db_path(cfg)) is False  # released


def test_no_encode_skips_dense(tmp_path, fake_encoder):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    stats = indexer.build_all(cfg, encode=False)
    assert "vectors" not in stats
    assert _vcount(cfg) == 0


def test_lock_held_during_encode(tmp_path, fake_encoder, monkeypatch):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    seen = {}
    real_bv = dense.build_vectors

    def _spy(c=None, *, force=False, on_checkpoint=None):
        seen["locked"] = index_lock.is_locked(vstore._db_path(cfg))
        return real_bv(c, force=force, on_checkpoint=on_checkpoint)

    monkeypatch.setattr(dense, "build_vectors", _spy)
    indexer.build_all(cfg)
    assert seen["locked"] is True  # the lock was held while encoding ran
    assert index_lock.is_locked(vstore._db_path(cfg)) is False


def test_exception_releases_lock(tmp_path, monkeypatch):
    root = tmp_path / "v"
    _write(root / "a.md")
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})

    def _boom(*a, **k):
        raise RuntimeError("encode failed")

    monkeypatch.setattr(dense, "build_vectors", _boom)
    with pytest.raises(RuntimeError):
        indexer.build_all(cfg)
    assert index_lock.is_locked(vstore._db_path(cfg)) is False  # released despite error


def test_cron_skips_when_build_holds_lock(tmp_path):
    root = tmp_path / "v"
    root.mkdir(parents=True)
    cfg = _cfg(tmp_path, {"vault": {"path": str(root)}})
    target = vstore._db_path(cfg)
    with index_lock.index_lock(target):
        # The 5-minute cron checks is_locked and skips a run already in progress.
        assert index_lock.is_locked(target) is True
    assert index_lock.is_locked(target) is False
