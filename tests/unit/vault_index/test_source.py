"""Tests for the vault-index FilesystemSource (multi-vault discovery)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from work_buddy.vault_index import store as vstore
from work_buddy.vault_index.chunker import chunk_markdown
from work_buddy.vault_index.source import FilesystemSource, load_vault_configs

_DEFAULT_EXCLUDES = [".obsidian", ".trash", ".git", "node_modules", "repos"]


def _cfg(vaults: dict, *, vault_root: str = "", exclude_folders=None) -> dict:
    return {
        "vault_index": {"vaults": vaults},
        "vault_root": vault_root,
        "obsidian": {"exclude_folders": exclude_folders or _DEFAULT_EXCLUDES},
    }


def _write(p: Path, text: str = "# H\n\nsome body text here\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _ids(files) -> set[str]:
    return {f.item_id for f in files}


def test_discover_honours_include_exclude(tmp_path):
    root = tmp_path / "v"
    _write(root / "a.md")
    _write(root / "archive" / "b.md")
    _write(root / "notes" / "c.md")
    _write(root / "d.txt", "not markdown")
    src = FilesystemSource(_cfg({"vault": {"path": str(root), "exclude": ["archive/**"]}}))
    files, statuses = src.discover()
    assert _ids(files) == {"vault/a.md", "vault/notes/c.md"}
    assert statuses[0].reachable and statuses[0].file_count == 2


def test_discover_skips_dot_dirs_and_exclude_folders(tmp_path):
    root = tmp_path / "v"
    _write(root / "keep.md")
    _write(root / ".obsidian" / "x.md")
    _write(root / "node_modules" / "y.md")
    _write(root / ".hidden" / "z.md")
    src = FilesystemSource(_cfg({"vault": {"path": str(root)}}))
    files, _ = src.discover()
    assert _ids(files) == {"vault/keep.md"}


def test_include_negation_rescues_file(tmp_path):
    root = tmp_path / "v"
    _write(root / "keep.md")
    _write(root / "drafts" / "x.md")
    src = FilesystemSource(_cfg(
        {"vault": {"path": str(root), "include": ["**/*.md", "!drafts/**"]}}
    ))
    files, _ = src.discover()
    assert _ids(files) == {"vault/keep.md"}


def test_nested_vault_precedence(tmp_path):
    root = tmp_path / "root"
    _write(root / "top.md")
    _write(root / "inner" / "n.md")
    src = FilesystemSource(_cfg({
        "A": {"path": str(root)},
        "B": {"path": str(root / "inner")},
    }))
    files, _ = src.discover()
    ids = _ids(files)
    assert "A/top.md" in ids
    assert "B/n.md" in ids
    assert "A/inner/n.md" not in ids          # inner belongs to B only
    assert len(files) == 2


def test_overlap_warns_at_config_load(tmp_path, caplog):
    root = tmp_path / "root"
    (root / "inner").mkdir(parents=True)
    with caplog.at_level(logging.WARNING):
        load_vault_configs(_cfg({"A": {"path": str(root)}, "B": {"path": str(root / "inner")}}))
    assert any("overlap" in r.message.lower() for r in caplog.records)


def test_unreachable_vault_detected_not_pruned(tmp_path):
    good = tmp_path / "good"
    _write(good / "g.md")
    missing = tmp_path / "does_not_exist"
    src = FilesystemSource(_cfg({
        "good": {"path": str(good)},
        "gone": {"path": str(missing)},
    }))
    files, statuses = src.discover()
    by_id = {s.vault_id: s for s in statuses}
    assert by_id["gone"].reachable is False
    assert by_id["gone"].reason == "not_a_directory"
    assert by_id["good"].reachable is True
    assert "good/g.md" in _ids(files)


def test_vault_id_keying_in_source_path(tmp_path):
    root = tmp_path / "v"
    _write(root / "sub" / "deep" / "note.md")
    src = FilesystemSource(_cfg({"myvault": {"path": str(root)}}))
    files, _ = src.discover()
    f = next(iter(files))
    assert f.item_id == "myvault/sub/deep/note.md"
    assert f.source_path == f.item_id
    assert "\\" not in f.source_path  # posix separators even on Windows


def test_parse_produces_chunks_via_handler(tmp_path):
    root = tmp_path / "v"
    text = "# Title\n\nFirst body.\n\n## Sub\n\nSecond body.\n"
    _write(root / "note.md", text)
    src = FilesystemSource(_cfg({"vault": {"path": str(root)}}))
    parsed = src.parse("vault/note.md")
    direct = chunk_markdown(text, source_path="vault/note.md")
    assert [c.key for c in parsed] == [c.key for c in direct]
    assert parsed[0].source_path == "vault/note.md"


def test_parse_unknown_extension_returns_empty(tmp_path):
    root = tmp_path / "v"
    _write(root / "x.pdf", "data")
    src = FilesystemSource(_cfg({"vault": {"path": str(root)}}))
    assert src.parse("vault/x.pdf") == []


def test_parse_missing_file_returns_empty(tmp_path):
    root = tmp_path / "v"
    root.mkdir(parents=True)
    src = FilesystemSource(_cfg({"vault": {"path": str(root)}}))
    assert src.parse("vault/nope.md") == []


def test_incremental_mtime_via_store(tmp_path):
    root = tmp_path / "v"
    f1 = _write(root / "one.md")
    _write(root / "two.md")
    cfg = _cfg({"vault": {"path": str(root)}})
    cfg["vault_index"]["db_path"] = str(tmp_path / "vi.db")
    src = FilesystemSource(cfg)

    files, _ = src.discover()
    conn = vstore.get_connection(cfg)
    try:
        for df in files:
            chunks = src.parse(df.item_id)
            vstore.upsert_chunks(conn, chunks, item_id=df.item_id, vault_id=df.vault_id)
            vstore.mark_item_indexed(conn, df.item_id, mtime=df.mtime,
                                     vault_id=df.vault_id, size=df.size,
                                     chunk_count=len(chunks))
        stored = vstore.get_indexed_items(conn, vault_id="vault")
        assert set(stored) == {"vault/one.md", "vault/two.md"}

        # Bump one file's mtime deterministically.
        newer = max(stored.values()) + 100
        os.utime(f1, (newer, newer))
        files2, _ = src.discover()
        by_id = {df.item_id: df for df in files2}
        assert by_id["vault/one.md"].mtime > stored["vault/one.md"]    # changed
        assert by_id["vault/two.md"].mtime <= stored["vault/two.md"]   # unchanged
    finally:
        conn.close()


def test_zero_config_default_vault(tmp_path):
    root = tmp_path / "v"
    root.mkdir(parents=True)
    cfg = _cfg({}, vault_root=str(root), exclude_folders=["repos", "node_modules"])
    configs = load_vault_configs(cfg)
    assert len(configs) == 1
    assert configs[0].id == "vault"
    assert configs[0].root == Path(str(root))
    assert {"repos", "node_modules"} <= configs[0].dir_excludes


def test_default_excludes_applied(tmp_path):
    root = tmp_path / "v"
    _write(root / "keep.md")
    _write(root / "repos" / "r.md")
    _write(root / "node_modules" / "n.md")
    cfg = _cfg({}, vault_root=str(root), exclude_folders=["repos", "node_modules"])
    src = FilesystemSource(cfg)
    files, _ = src.discover()
    assert _ids(files) == {"vault/keep.md"}


def test_exclude_dirs_overrides_obsidian(tmp_path):
    root = tmp_path / "v"
    _write(root / "keep.md")
    _write(root / "repos" / "r.md")
    _write(root / "node_modules" / "n.md")
    # vault_index.exclude_dirs excludes node_modules but NOT repos → repos is indexed,
    # even though obsidian.exclude_folders would have excluded it.
    cfg = {
        "vault_index": {
            "vaults": {"vault": {"path": str(root)}},
            "exclude_dirs": ["node_modules"],
        },
        "vault_root": "",
        "obsidian": {"exclude_folders": ["repos", "node_modules"]},
    }
    files, _ = FilesystemSource(cfg).discover()
    ids = _ids(files)
    assert "vault/repos/r.md" in ids             # repos INCLUDED via the override
    assert "vault/keep.md" in ids
    assert "vault/node_modules/n.md" not in ids  # still excluded
