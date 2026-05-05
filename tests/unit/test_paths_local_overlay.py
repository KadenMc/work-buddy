"""Unit tests for ``paths._load_data_root_from_config``.

Specifically the overlay behavior added so ``config.local.yaml``
overrides ``config.yaml`` for the ``paths.data_root`` key — symmetrical
to how ``config.load_config()`` merges, but reachable from
``paths.py`` without an import cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import work_buddy.paths as pmod


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Point ``paths.repo_root()`` at a tmp dir so we control config files."""
    monkeypatch.setattr(pmod, "repo_root", lambda: tmp_path)
    return tmp_path


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_default_when_no_config(fake_repo):
    """No config files at all → falls back to ``data``."""
    assert pmod._load_data_root_from_config() == "data"


def test_base_config_only(fake_repo):
    """Only ``config.yaml`` is present → that value wins."""
    _write(fake_repo / "config.yaml", "paths:\n  data_root: '.data'\n")
    assert pmod._load_data_root_from_config() == ".data"


def test_local_overrides_base(fake_repo):
    """``config.local.yaml`` overrides ``config.yaml`` for the same key."""
    _write(fake_repo / "config.yaml", "paths:\n  data_root: 'data'\n")
    _write(fake_repo / "config.local.yaml", "paths:\n  data_root: '.data'\n")
    assert pmod._load_data_root_from_config() == ".data"


def test_local_only(fake_repo):
    """Only ``config.local.yaml`` present → still picked up."""
    _write(fake_repo / "config.local.yaml", "paths:\n  data_root: '/abs/elsewhere'\n")
    assert pmod._load_data_root_from_config() == "/abs/elsewhere"


def test_local_does_not_clobber_unrelated_keys(fake_repo):
    """Overlay only merges the ``paths`` subtree; we do not look at others.

    Specifically: if ``config.local.yaml`` lacks a ``paths`` block,
    the base value wins (no implicit reset to default).
    """
    _write(fake_repo / "config.yaml", "paths:\n  data_root: '.data'\n")
    _write(fake_repo / "config.local.yaml", "obsidian:\n  api_port: 27124\n")
    assert pmod._load_data_root_from_config() == ".data"


def test_unreadable_files_do_not_crash(fake_repo):
    """Malformed YAML in either file is logged-and-skipped, not raised.

    The function must never raise — the loader runs at module-import
    time of any work_buddy submodule, so a parse error would cascade
    catastrophically. Returns the next-best value (or default).
    """
    _write(fake_repo / "config.yaml", "paths:\n  data_root: '.data'\n")
    _write(fake_repo / "config.local.yaml", ":\n  not yaml: [\n")  # malformed
    # Should fall back gracefully to base config's value.
    assert pmod._load_data_root_from_config() == ".data"


def test_paths_block_can_be_null(fake_repo):
    """An explicit ``paths: null`` does not crash; falls back to default."""
    _write(fake_repo / "config.yaml", "paths: null\n")
    assert pmod._load_data_root_from_config() == "data"


def test_data_dir_picks_up_overlay(fake_repo, monkeypatch):
    """End-to-end: ``data_dir()`` follows the overlay decision.

    Ensures the new function is the only path to ``data_root``
    resolution across ``data_dir()`` / ``resolve()``.
    """
    _write(fake_repo / "config.yaml", "paths:\n  data_root: 'data'\n")
    _write(fake_repo / "config.local.yaml", "paths:\n  data_root: '.data'\n")
    result = pmod.data_dir()
    assert result == fake_repo / ".data"
    assert result.exists() and result.is_dir()
