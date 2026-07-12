"""First-run storage behavior for contracts."""

from pathlib import Path

import work_buddy.contracts as contracts


def test_unconfigured_vault_is_read_only_and_empty(tmp_path, monkeypatch):
    missing_vault = tmp_path / "path" / "to" / "vault"
    monkeypatch.setattr(
        contracts,
        "load_config",
        lambda: {
            "vault_root": str(missing_vault),
            "contracts": {"vault_path": "work-buddy/contracts"},
        },
    )

    expected = missing_vault / "work-buddy" / "contracts"
    assert contracts.get_contracts_dir() == expected
    assert contracts.load_all_contracts() == []
    assert not missing_vault.exists()


def test_configured_vault_creates_default_contracts_dir(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(
        contracts,
        "load_config",
        lambda: {
            "vault_root": str(vault),
            "contracts": {"vault_path": "work-buddy/contracts"},
        },
    )

    expected = vault / "work-buddy" / "contracts"
    assert contracts.get_contracts_dir() == expected
    assert expected.is_dir()


def test_explicit_contracts_dir_still_creates_parents(tmp_path):
    explicit = tmp_path / "custom" / "contracts"

    assert contracts.load_all_contracts(Path(explicit)) == []
    assert explicit.is_dir()
