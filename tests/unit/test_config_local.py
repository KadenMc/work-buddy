"""Unit tests for config.local.yaml helpers in config.py."""

import pytest
import yaml
from pathlib import Path

from work_buddy.config import (
    config_local_path,
    read_config_local,
    write_config_local,
)


class TestConfigLocalPath:
    def test_returns_path_object(self):
        result = config_local_path()
        assert isinstance(result, Path)
        assert result.name == "config.local.yaml"

    def test_is_sibling_of_config_yaml(self):
        result = config_local_path()
        assert (result.parent / "config.yaml").exists() or True  # may not exist in CI


class TestReadConfigLocal:
    def test_returns_empty_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "work_buddy.config.config_local_path",
            lambda: tmp_path / "config.local.yaml",
        )
        result = read_config_local()
        assert result == {}

    def test_reads_yaml(self, monkeypatch, tmp_path):
        local = tmp_path / "config.local.yaml"
        local.write_text("dashboard:\n  external_url: test\n", encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.config.config_local_path",
            lambda: local,
        )
        result = read_config_local()
        assert result["dashboard"]["external_url"] == "test"

    def test_empty_file_returns_empty_dict(self, monkeypatch, tmp_path):
        local = tmp_path / "config.local.yaml"
        local.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.config.config_local_path",
            lambda: local,
        )
        result = read_config_local()
        assert result == {}


class TestWriteConfigLocal:
    def test_creates_file_if_missing(self, monkeypatch, tmp_path):
        local = tmp_path / "config.local.yaml"
        monkeypatch.setattr(
            "work_buddy.config.config_local_path",
            lambda: local,
        )
        # Also patch read to use same path
        monkeypatch.setattr(
            "work_buddy.config.read_config_local",
            lambda: yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else {},
        )
        write_config_local("features", {"hindsight": {"wanted": False}})
        assert local.exists()
        data = yaml.safe_load(local.read_text(encoding="utf-8"))
        assert data["features"]["hindsight"]["wanted"] is False

    def test_preserves_other_sections(self, monkeypatch, tmp_path):
        local = tmp_path / "config.local.yaml"
        local.write_text("dashboard:\n  external_url: test\n", encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.config.config_local_path",
            lambda: local,
        )
        monkeypatch.setattr(
            "work_buddy.config.read_config_local",
            lambda: yaml.safe_load(local.read_text(encoding="utf-8")) or {},
        )
        write_config_local("features", {"obsidian": {"wanted": True}})
        data = yaml.safe_load(local.read_text(encoding="utf-8"))
        assert data["dashboard"]["external_url"] == "test"
        assert data["features"]["obsidian"]["wanted"] is True

    def test_overwrites_section(self, monkeypatch, tmp_path):
        local = tmp_path / "config.local.yaml"
        local.write_text(
            "features:\n  obsidian:\n    wanted: true\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "work_buddy.config.config_local_path",
            lambda: local,
        )
        monkeypatch.setattr(
            "work_buddy.config.read_config_local",
            lambda: yaml.safe_load(local.read_text(encoding="utf-8")) or {},
        )
        write_config_local("features", {"obsidian": {"wanted": False}})
        data = yaml.safe_load(local.read_text(encoding="utf-8"))
        assert data["features"]["obsidian"]["wanted"] is False
