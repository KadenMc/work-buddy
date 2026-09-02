from __future__ import annotations

import json
import sqlite3
import tarfile


def test_contracts_database_is_registered_as_vital_data():
    from work_buddy.backups.local import VITAL_DBS
    from work_buddy.paths import RESOURCES

    assert VITAL_DBS["contracts"] == "db/contracts"
    assert RESOURCES["db/contracts"] == "db/contracts.db"


def test_restore_runs_contract_migrations_and_integrity_validation(tmp_path):
    from work_buddy.backups.restore import _apply_migrations_inplace
    from work_buddy.contracts_domain.migrations import CONTRACT_MIGRATIONS
    from work_buddy.contracts_domain.store import ContractStore

    database = tmp_path / "contracts.db"
    sqlite3.connect(database).close()

    _apply_migrations_inplace("contracts", database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            CONTRACT_MIGRATIONS.target_version
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "contracts" in tables
        assert "contract_import_seals" in tables
    finally:
        connection.close()
    ContractStore(database).validate()


def test_restore_schema_ceiling_includes_contracts():
    from work_buddy.backups.restore import _current_known_max_schema_versions
    from work_buddy.contracts_domain.migrations import CONTRACT_MIGRATIONS

    assert _current_known_max_schema_versions()["contracts"] == (
        CONTRACT_MIGRATIONS.target_version
    )


def test_backup_restore_round_trip_preserves_sealed_contract_authority(
    tmp_path, monkeypatch
):
    from work_buddy.backups import local
    from work_buddy.backups.restore import _apply_migrations_inplace
    from work_buddy.contracts_domain.importer import ContractImporter
    from work_buddy.contracts_domain.service import ContractService
    from work_buddy.contracts_domain.store import ContractStore
    from work_buddy.cutover_maintenance import authorize_isolated_rehearsal_root
    from work_buddy.sources import ActorRef, SourceStore, TrustedIngressContext

    source = tmp_path / "legacy"
    source.mkdir()
    (source / "paper.md").write_text(
        "---\n"
        "title: Backup paper\n"
        "status: active\n"
        "type: paper\n"
        "deadline: 2026-10-01\n"
        "---\n"
        "# Claim\nPreserve this contract.\n",
        encoding="utf-8",
    )
    live_db = tmp_path / "live" / "contracts.db"
    store = ContractStore.create(live_db)
    sources = SourceStore.create(tmp_path / "sources")
    tenant = "tenant-contract-backup-test"
    context = TrustedIngressContext(
        issuer=ActorRef("test-authority", "contract-import", "service", tenant),
        issuer_version="test/v1",
        inputter=ActorRef("test-authority", "legacy-owner", "human", tenant),
        service_principal=ActorRef("test-authority", "contracts", "service", tenant),
        tenant_scope_id=tenant,
        surface="contract-history-import",
        namespace="contract-history-import-staging",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="historical_inputter_only",
        authorization_fingerprint="e" * 64,
        permitted_purposes=("contracts.history_import",),
    )
    importer = ContractImporter(store, sources)
    staged = importer.stage(
        source,
        actor="user:test-user",
        intent_id="backup-stage",
        ingress_context=context,
    )
    importer.seal(
        staged["cohort_id"],
        expected_inventory_sha256=staged["inventory_sha256"],
        actor="user:test-user",
        intent_id="backup-seal",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=authorize_isolated_rehearsal_root(
            live_db.parent,
            authority_paths={"contracts": live_db},
        ),
    )

    monkeypatch.setattr(local, "_resolve_vital_dbs", lambda: {"contracts": live_db})
    monkeypatch.setattr(local, "data_dir", lambda name="": tmp_path / name)
    result = local.run_backup(manual=True)
    manifest = json.loads(result["manifest"])
    assert manifest["row_counts"]["contracts"]["contracts"] == 1
    assert manifest["row_counts"]["contracts"]["contract_import_seals"] == 1

    restore_dir = tmp_path / "restore"
    restore_dir.mkdir()
    with tarfile.open(result["tarball_path"], "r:gz") as archive:
        assert "contracts.db" in archive.getnames()
        archive.extract("contracts.db", path=restore_dir)
    restored = restore_dir / "contracts.db"
    _apply_migrations_inplace("contracts", restored)

    restored_store = ContractStore(restored)
    restored_store.validate()
    assert restored_store.authority()["state"] == "native"
    assert ContractService(restored_store).get("paper.md")["title"] == "Backup paper"
