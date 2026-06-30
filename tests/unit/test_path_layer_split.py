"""Path-layer split: the four roots resolve independently of each other.

Under a clone, install/config/asset/data all coincide at ``repo_root()`` so
behavior is unchanged. With the ``WORK_BUDDY_*`` overrides set they diverge,
which is what lets work-buddy run when it is not a git clone. These tests guard
that contract: the defaults stay collapsed, and each override moves exactly one
root.
"""

import os
import subprocess
import sys

import pytest

from work_buddy import paths


@pytest.fixture
def _clear_paths_cache():
    """Reset the memoized paths-section so env changes take effect mid-test."""
    paths._paths_section_cache = None
    yield
    paths._paths_section_cache = None


class TestDefaultsCoincide:
    def test_all_roots_default_to_repo_root(self, monkeypatch, _clear_paths_cache):
        for var in ("WORK_BUDDY_ASSET_ROOT", "WORK_BUDDY_CONFIG_DIR", "WORK_BUDDY_DATA_DIR"):
            monkeypatch.delenv(var, raising=False)
        root = paths.repo_root()
        assert paths.config_dir() == root
        assert paths.asset_root() == root
        assert paths._data_base().is_relative_to(root)

    def test_install_root_is_the_package_dir(self):
        assert paths.install_root() == paths.repo_root() / "work_buddy"


class TestOverridesDiverge:
    def test_config_dir_override(self, monkeypatch, tmp_path, _clear_paths_cache):
        cfg = tmp_path / "cfg"
        monkeypatch.setenv("WORK_BUDDY_CONFIG_DIR", str(cfg))
        assert paths.config_dir() == cfg

    def test_asset_root_override(self, monkeypatch, tmp_path, _clear_paths_cache):
        assets = tmp_path / "assets"
        monkeypatch.setenv("WORK_BUDDY_ASSET_ROOT", str(assets))
        assert paths.asset_root() == assets

    def test_data_override_drives_resolve_and_data_dir(self, monkeypatch, tmp_path, _clear_paths_cache):
        data = tmp_path / "data"
        monkeypatch.setenv("WORK_BUDDY_DATA_DIR", str(data))
        assert paths.resolve("db/tasks") == data / "db" / "task_metadata.db"
        assert paths.data_dir("runtime") == data / "runtime"

    def test_roots_are_independent(self, monkeypatch, tmp_path, _clear_paths_cache):
        a, c, d = tmp_path / "a", tmp_path / "c", tmp_path / "d"
        monkeypatch.setenv("WORK_BUDDY_ASSET_ROOT", str(a))
        monkeypatch.setenv("WORK_BUDDY_CONFIG_DIR", str(c))
        monkeypatch.setenv("WORK_BUDDY_DATA_DIR", str(d))
        assert paths.asset_root() == a
        assert paths.config_dir() == c
        assert paths.resolve("db/messages") == d / "db" / "messages.db"
        assert len({a, c, d}) == 3


@pytest.mark.component
class TestNonCloneLayoutInSubprocess:
    """Import-time asset/data consumers honor the overrides in a fresh process.

    ``store._STORE_DIR`` and ``prompts._DEFAULTS_DIR`` are computed at import
    time. A packaged install sets the env before the process starts; this proves
    those import-time constants land under the override roots rather than beside
    the package, which is the real non-clone scenario.
    """

    def test_import_time_constants_follow_overrides(self, tmp_path):
        asset = tmp_path / "assets"
        data = tmp_path / "data"
        env = dict(os.environ)
        env.update(
            {
                "WORK_BUDDY_ASSET_ROOT": str(asset),
                "WORK_BUDDY_CONFIG_DIR": str(tmp_path / "cfg"),
                "WORK_BUDDY_DATA_DIR": str(data),
                "WORK_BUDDY_SESSION_ID": "test-path-split",
            }
        )
        code = (
            "from work_buddy.knowledge import store;"
            "from work_buddy import prompts, paths;"
            "print(store._STORE_DIR);"
            "print(prompts._DEFAULTS_DIR);"
            "print(paths.resolve('db/tasks'))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert str(asset / "knowledge" / "store") in out
        assert str(asset / "prompts" / "defaults") in out
        assert str(data / "db" / "task_metadata.db") in out
