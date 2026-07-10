"""Tests for the install provisioning orchestrator (``work_buddy.provision``).

Everything runs into throwaway temp dirs: ``WORK_BUDDY_CONFIG_DIR`` redirects
where config.yaml / .env / .mcp.json land, and ``--data-dir`` is a temp path, so
the real repo and the user's live config are never touched. ``start=False`` keeps
the tests from spawning a real sidecar.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from work_buddy import compat, provision as prov


@pytest.fixture(autouse=True)
def _stub_cli_shim(monkeypatch):
    """provision() publishes a wbuddy PATH shim; stub it so tests never touch
    the host's registry or ~/.local/bin. The shim itself is covered by
    test_userpath.py against temp paths."""
    from work_buddy import userpath

    monkeypatch.setattr(
        userpath, "install_cli_shim",
        lambda home: {"ok": True, "changed": False, "detail": "stubbed in tests"},
    )
    monkeypatch.setattr(
        userpath, "uninstall_cli_shim",
        lambda home: {"ok": True, "detail": "stubbed in tests"},
    )


@pytest.fixture
def tmp_install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    # config_dir -> temp home (asset_root stays the real repo, so the shipped
    # config.example.yaml is still readable). No data-dir env var: the config
    # value written by provision drives data resolution.
    monkeypatch.setenv("WORK_BUDDY_CONFIG_DIR", str(home))
    monkeypatch.delenv("WORK_BUDDY_DATA_DIR", raising=False)
    return home, data


def test_user_data_dir_windows_branch(monkeypatch):
    if not compat.IS_WINDOWS:
        pytest.skip("windows-only branch")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert compat.user_data_dir() == Path(r"C:\Users\test\AppData\Local") / "work-buddy"


def test_provision_writes_config_env_mcp_and_pins_interpreter(tmp_install):
    home, data = tmp_install
    res = prov.provision(
        data_dir=str(data),
        vault_root=str(home),
        timezone="America/Toronto",
        anthropic_key="sk-test-000000000000000000000000",
        start=False,
    )

    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["paths"]["data_root"] == str(data.resolve())
    assert cfg["sidecar"]["python_executable"] == sys.executable
    assert cfg["timezone"] == "America/Toronto"

    assert "SUBAGENT_ANTHROPIC_API_KEY" in (home / ".env").read_text(encoding="utf-8")

    mcp = json.loads((home / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["work-buddy"]["type"] == "http"
    assert mcp["mcpServers"]["work-buddy"]["url"].endswith("/mcp")

    assert data.exists()  # data tree relocated under the temp data dir
    assert res["home"] == str(home)
    assert res["data_dir"] == str(data.resolve())
    assert res["sidecar"] is None
    assert "bootstrap" in res and "summary" in res["bootstrap"]


def test_provision_ok_when_feature_config_missing(tmp_install):
    """A fresh install with no vault and no API key still succeeds: the core work
    (writable data, MCP wiring) is done and feature config is deferred to the
    wizard. The unmet required checks must be surfaced in ``bootstrap`` for the
    user, but must NOT fail provisioning (else every default install "fails")."""
    home, data = tmp_install
    res = prov.provision(data_dir=str(data), start=False)
    assert res["ok"] is True
    # the unmet feature-config requirements are still reported, just not fatal
    failed_ids = {r["id"] for r in res["bootstrap"]["results"] if not r["ok"]}
    assert any("vault" in i or "anthropic" in i for i in failed_ids)


def test_provision_is_idempotent(tmp_install):
    home, data = tmp_install
    prov.provision(data_dir=str(data), start=False)
    prov.provision(data_dir=str(data), start=False)  # re-run must not corrupt
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["paths"]["data_root"] == str(data.resolve())
    assert (home / "config.local.yaml").exists()


def test_provision_seeds_config_local_stub(tmp_install):
    home, data = tmp_install
    prov.provision(data_dir=str(data), start=False)
    assert (home / "config.local.yaml").exists()


def test_provision_home_flag_redirects_config(tmp_path, monkeypatch):
    """`home=` explicitly targets an install dir, so config/.mcp.json land there
    regardless of the ambient config_dir. Protects a real clone from a test run."""
    monkeypatch.delenv("WORK_BUDDY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("WORK_BUDDY_DATA_DIR", raising=False)
    home = tmp_path / "explicit-home"
    home.mkdir()
    prov.provision(home=str(home), data_dir=str(tmp_path / "d"), start=False)
    assert (home / "config.yaml").exists()
    assert (home / ".mcp.json").exists()


def test_provision_can_select_setup_ready_harness(tmp_install, monkeypatch):
    home, data = tmp_install
    seen = {}

    class FakeSync:
        ok = True
        generated_paths = ["CLAUDE.md", ".mcp.json"]
        error = ""
        stderr = ""
        returncode = 0

    def fake_sync_harnesses(ids, output_root=None, **kwargs):
        seen["ids"] = tuple(ids)
        seen["output_root"] = output_root
        seen["install_toolchain"] = kwargs.get("install_toolchain")
        return FakeSync()

    monkeypatch.setattr("work_buddy.harness.sync.sync_harnesses", fake_sync_harnesses)

    res = prov.provision(data_dir=str(data), start=False, harness="claudecode")

    assert res["ok"] is True
    assert seen == {
        "ids": ("claudecode",),
        "output_root": home,
        "install_toolchain": True,
    }
    assert res["harness"]["id"] == "claudecode"
    assert res["harness"]["ok"] is True

    local_cfg = yaml.safe_load((home / "config.local.yaml").read_text(encoding="utf-8"))
    assert local_cfg["harness"]["enabled"] == ["claudecode"]
    assert local_cfg["harness"]["primary"] == "claudecode"


def test_provision_can_select_codex_as_setup_ready_harness(tmp_install, monkeypatch):
    home, data = tmp_install

    class FakeSync:
        ok = True
        generated_paths = ["AGENTS.md", ".codex/config.toml"]
        error = ""
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        "work_buddy.harness.sync.sync_harnesses",
        lambda ids, output_root=None, **kwargs: FakeSync(),
    )

    res = prov.provision(
        data_dir=str(data),
        start=False,
        harness="codexcli",
    )

    assert res["ok"] is True
    assert res["harness"]["id"] == "codexcli"
    assert res["harness"]["setup_ready"] is True
    assert (home / "config.local.yaml").exists()
