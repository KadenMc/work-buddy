"""B — the vault_config write capability (config.local.yaml round-trip, validation)."""
from __future__ import annotations

import work_buddy.mcp_server.ops.vault_ops as vo


def _patch_config(monkeypatch, initial=None):
    """Redirect read/write_config_local to an in-memory dict — never the real file."""
    store = {"data": dict(initial or {})}
    monkeypatch.setattr("work_buddy.config.read_config_local", lambda: dict(store["data"]))

    def _write(section, data):
        store["data"][section] = data

    monkeypatch.setattr("work_buddy.config.write_config_local", _write)
    return store


def test_set_promotes_default_and_preserves_other_sections(monkeypatch):
    store = _patch_config(monkeypatch, {"features": {"telegram": {"wanted": True}}})
    out = vo._vault_config_dispatch(
        action="set", id="vault", path="C:/x", include=["**/*.md"], exclude=["repos/**"],
    )
    assert out["success"] is True
    # unrelated section preserved (exercises read-modify-write)
    assert store["data"]["features"] == {"telegram": {"wanted": True}}
    assert store["data"]["vault_index"]["vaults"]["vault"] == {
        "path": "C:/x", "include": ["**/*.md"], "exclude": ["repos/**"],
    }
    # a non-existent path is saved with a warning, not blocked
    assert out["warning"]


def test_set_defaults_include_when_empty(monkeypatch):
    store = _patch_config(monkeypatch)
    vo._vault_config_dispatch(action="set", id="notes", path="C:/n", include=[], exclude=[])
    assert store["data"]["vault_index"]["vaults"]["notes"]["include"] == ["**/*.md"]


def _patch_default_vault(monkeypatch):
    """Make load_vault_configs synthesize a default 'vault' (default-mode)."""
    from pathlib import Path
    from work_buddy.vault_index.source import VaultConfig
    monkeypatch.setattr(
        "work_buddy.vault_index.source.load_vault_configs",
        lambda cfg=None: [VaultConfig(id="vault", root=Path("/vaults/example"),
                                      include=("**/*.md",), exclude=(), dir_excludes=frozenset())],
    )


def test_set_first_explicit_preserves_default(monkeypatch):
    # Adding the FIRST explicit vault must snapshot the default so the main vault
    # isn't silently orphaned (the bug live-testing caught).
    store = _patch_config(monkeypatch)  # empty → default mode
    _patch_default_vault(monkeypatch)
    out = vo._vault_config_dispatch(action="set", id="docs", path="C:/docs")
    assert out["success"] is True and out["promoted_default"] is True
    vaults = store["data"]["vault_index"]["vaults"]
    assert set(vaults) == {"vault", "docs"}            # BOTH — default preserved
    assert "example" in vaults["vault"]["path"]


def test_set_promote_default_in_place(monkeypatch):
    # Editing the default itself just overrides its snapshotted entry (no dup).
    store = _patch_config(monkeypatch)
    _patch_default_vault(monkeypatch)
    out = vo._vault_config_dispatch(action="set", id="vault",
                                    path="/vaults/example", exclude=["repos/**"])
    assert out["success"] is True and out["promoted_default"] is False
    vaults = store["data"]["vault_index"]["vaults"]
    assert set(vaults) == {"vault"} and vaults["vault"]["exclude"] == ["repos/**"]


def test_set_validation_errors(monkeypatch):
    _patch_config(monkeypatch)
    assert "errors_by_field" in vo._vault_config_dispatch(action="set", id="a/b", path="/x")
    assert "errors_by_field" in vo._vault_config_dispatch(action="set", id="ok", path="")


def test_remove_reports_orphan(monkeypatch):
    store = _patch_config(monkeypatch, {"vault_index": {"vaults": {"docs": {"path": "/d"}}}})
    monkeypatch.setattr(vo, "_vault_has_chunks", lambda vid: True)
    out = vo._vault_config_dispatch(action="remove", id="docs")
    assert out["success"] is True and out["orphaned_chunks"] is True
    assert "docs" not in store["data"]["vault_index"]["vaults"]


def test_remove_no_chunks(monkeypatch):
    _patch_config(monkeypatch, {"vault_index": {"vaults": {"docs": {"path": "/d"}}}})
    monkeypatch.setattr(vo, "_vault_has_chunks", lambda vid: False)
    out = vo._vault_config_dispatch(action="remove", id="docs")
    assert out["success"] is True and out["orphaned_chunks"] is False


def test_bad_action(monkeypatch):
    _patch_config(monkeypatch)
    assert vo._vault_config_dispatch(action="frobnicate", id="x")["success"] is False
