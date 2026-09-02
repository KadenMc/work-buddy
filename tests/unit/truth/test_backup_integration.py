"""Backup coverage for the truth registry and registered stores."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.truth.identity import new_id, sha256_bytes
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.export import TruthImportError, export_store, import_store
from work_buddy.truth.store import TruthStore


NOW = "2026-07-17T16:00:00.000+00:00"


def _profile(
    store_id: str,
    *,
    export_committed: bool = True,
) -> dict[str, object]:
    return {
        "store_id": store_id,
        "profile": "test",
        "title": "Backup test",
        "allowed_claim_kinds": ["fact"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
            "confirmation_surfaces": ["cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": export_committed,
    }


def _store(
    root: Path,
    *,
    export_committed: bool = True,
    with_causality: bool = True,
) -> TruthStore:
    root.mkdir()
    store_id = new_id()
    store = TruthStore.create(
        root,
        _profile(store_id, export_committed=export_committed),
    )
    if with_causality:
        DocumentCausalityStore(store.paths.sidecar)
    return store


def test_truth_registry_is_a_vital_migrated_database(tmp_path: Path) -> None:
    from work_buddy.backups.local import VITAL_DBS, _resolve_vital_dbs
    from work_buddy.backups.restore import (
        _apply_migrations_inplace,
        _current_known_max_schema_versions,
    )
    from work_buddy.paths import resolve
    from work_buddy.truth.registry_migrations import TRUTH_REGISTRY_MIGRATIONS

    assert VITAL_DBS["truth_registry"] == "db/truth-registry"
    assert resolve("db/truth-registry").name == "truth_registry.db"
    assert _resolve_vital_dbs()["truth_registry"].name == "truth_registry.db"
    assert (
        _current_known_max_schema_versions()["truth_registry"]
        == TRUTH_REGISTRY_MIGRATIONS.target_version
    )

    restored = tmp_path / "truth_registry.db"
    sqlite3.connect(restored).close()
    _apply_migrations_inplace("truth_registry", restored)
    conn = sqlite3.connect(restored)
    try:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == TRUTH_REGISTRY_MIGRATIONS.target_version
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'truth_stores'"
        ).fetchone()
    finally:
        conn.close()


def test_backup_stages_portable_payload_and_reports_unreachable_store(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from work_buddy.backups import local

    registry_path = tmp_path / "live" / "truth_registry.db"
    registry = TruthStoreRegistry(registry_path)
    included = _store(tmp_path / "included")
    unavailable = _store(tmp_path / "unavailable", with_causality=False)
    unavailable_id = unavailable.store_id
    registry.register(included)
    registry.register(unavailable)
    unavailable.paths.sidecar.rename(tmp_path / "unavailable-offline")

    monkeypatch.setattr(
        local,
        "_resolve_vital_dbs",
        lambda: {"truth_registry": registry_path},
    )
    monkeypatch.setattr(local, "data_dir", lambda name="": tmp_path / name)

    result = local.run_backup(manual=True)
    manifest = json.loads(result["manifest"])
    by_id = {item["store_id"]: item for item in manifest["truth_stores"]}
    assert len(manifest["database_sha256"]["truth_registry"]) == 64
    assert by_id[included.store_id]["backup_status"] == "included"
    assert by_id[unavailable_id]["backup_status"] == "unreachable"
    assert result["unreachable_truth_stores"] == [by_id[unavailable_id]]
    assert result["truth_store_errors"] == []

    with tarfile.open(result["tarball_path"], "r:gz") as archive:
        names = set(archive.getnames())
        profile_member = f"truth_stores/{included.store_id}/store.yaml"
        export_member = f"truth_stores/{included.store_id}/claims.jsonl"
        causality_member = (
            f"truth_stores/{included.store_id}/document-causality.json"
        )
        assert profile_member in names
        assert export_member in names
        assert causality_member in names
        assert f"truth_stores/{unavailable_id}/claims.jsonl" not in names
        export_bytes = archive.extractfile(export_member).read()
        causality_bytes = archive.extractfile(causality_member).read()
        profile_bytes = archive.extractfile(profile_member).read()
        assert profile_bytes

    assert hashlib.sha256(export_bytes).hexdigest() == by_id[included.store_id][
        "export_sha256"
    ]
    assert hashlib.sha256(profile_bytes).hexdigest() == by_id[included.store_id][
        "profile_sha256"
    ]
    assert json.loads(export_bytes.splitlines()[0])["store_info"]["store_id"] == (
        included.store_id
    )
    assert hashlib.sha256(causality_bytes).hexdigest() == by_id[included.store_id][
        "causality_sha256"
    ]
    causality = json.loads(causality_bytes)
    assert causality["store_id"] == included.store_id
    assert causality["payload_sha256"] == by_id[included.store_id][
        "causality_payload_sha256"
    ]


def test_backup_marks_reachable_store_without_causality_as_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from work_buddy.backups import local

    registry_path = tmp_path / "live" / "truth_registry.db"
    registry = TruthStoreRegistry(registry_path)
    incomplete = _store(tmp_path / "incomplete", with_causality=False)
    registry.register(incomplete)
    monkeypatch.setattr(
        local,
        "_resolve_vital_dbs",
        lambda: {"truth_registry": registry_path},
    )
    monkeypatch.setattr(local, "data_dir", lambda name="": tmp_path / name)

    result = local.run_backup(manual=True)
    row = result["truth_store_errors"][0]
    assert row["store_id"] == incomplete.store_id
    assert row["backup_status"] == "error"
    assert "causality" in row["error"]
    with tarfile.open(result["tarball_path"], "r:gz") as archive:
        assert not any(
            name.startswith(f"truth_stores/{incomplete.store_id}/")
            for name in archive.getnames()
        )


def _seed_document_with_snapshot(store: TruthStore) -> str:
    """Register a throwaway document carrying a content-addressed ydoc snapshot."""
    snapshot = b"opaque-ydoc-snapshot-for-backup"
    digest = sha256_bytes(snapshot)
    (store.paths.blobs / digest).write_bytes(snapshot)
    document_id = new_id()
    with store.write_transaction() as conn:
        conn.execute(
            "INSERT INTO documents "
            "(id, path, title, document_class, content_sha256, "
            "ydoc_snapshot_sha256, created_at, created_by_kind, created_by_ref, "
            "meta_json) VALUES (?, 'docs/backup.md', 'Backup doc', "
            "'co_authored', ?, ?, ?, 'human', 'user-1', NULL)",
            (document_id, sha256_bytes(b"materialized body"), digest, NOW),
        )
        store._insert_ledger_record_locked(conn, "document", document_id)
        # This fixture seeds below the public document API. Mirror the v11
        # compatibility policy that ordinary registration now provisions in
        # the same transaction so the portable export is structurally valid.
        from work_buddy.cowork.truth_activation import (
            backfill_legacy_document_policies,
        )

        assert backfill_legacy_document_policies(conn) == 1
    return digest


def test_backup_export_embeds_the_ydoc_snapshot_blob(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from work_buddy.backups import local

    registry_path = tmp_path / "live" / "truth_registry.db"
    registry = TruthStoreRegistry(registry_path)
    store = _store(tmp_path / "cowork")
    snapshot_digest = _seed_document_with_snapshot(store)
    registry.register(store)

    monkeypatch.setattr(
        local,
        "_resolve_vital_dbs",
        lambda: {"truth_registry": registry_path},
    )
    monkeypatch.setattr(local, "data_dir", lambda name="": tmp_path / name)

    result = local.run_backup(manual=True)
    export_member = f"truth_stores/{store.store_id}/claims.jsonl"
    with tarfile.open(result["tarball_path"], "r:gz") as archive:
        export_bytes = archive.extractfile(export_member).read()

    blob_digests = [
        json.loads(line)["content_sha256"]
        for line in export_bytes.splitlines()
        if json.loads(line).get("record_type") == "blob"
    ]
    assert snapshot_digest in blob_digests


def test_backup_generates_private_ephemeral_export_without_raw_store_db(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from work_buddy.backups import local

    registry_path = tmp_path / "live" / "truth_registry.db"
    registry = TruthStoreRegistry(registry_path)
    private = _store(tmp_path / "private", export_committed=False)
    registry.register(private)
    assert not private.paths.claims_export.exists()

    monkeypatch.setattr(
        local,
        "_resolve_vital_dbs",
        lambda: {"truth_registry": registry_path},
    )
    monkeypatch.setattr(local, "data_dir", lambda name="": tmp_path / name)

    result = local.run_backup(manual=True)
    with tarfile.open(result["tarball_path"], "r:gz") as archive:
        names = set(archive.getnames())
        assert f"truth_stores/{private.store_id}/claims.jsonl" in names
        assert f"truth_stores/{private.store_id}/store.db" not in names
    assert not private.paths.claims_export.exists()


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str) -> tuple[Path, ...]:
        return ()


def test_truth_import_publishes_matching_document_causality_companion(
    tmp_path: Path,
) -> None:
    source = _store(tmp_path / "source-recovery")
    _seed_document_with_snapshot(source)
    conn = source.connect()
    try:
        document_id = str(conn.execute("SELECT id FROM documents").fetchone()[0])
    finally:
        conn.close()
    causality = DocumentCausalityStore(source.paths.sidecar)
    binding = causality.ensure_binding(
        domain_namespace="journal",
        domain_kind="daily_note",
        domain_entity_id="1" * 32,
        domain_revision="revision-1",
        store_id=source.store_id,
        document_id=document_id,
        role="daily_note",
        created_by="service:test",
    )
    companion = (
        json.dumps(
            causality.export_recovery_bundle(store_id=source.store_id),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    ledger = export_store(source)
    target = tmp_path / "restored-recovery"
    target.mkdir()

    restored = import_store(
        ledger.path,
        target,
        registry=_EmptyRegistry(),
        causality_source=companion,
        causality_sha256=hashlib.sha256(companion).hexdigest(),
    ).store

    assert restored.store_id == source.store_id
    restored_binding = DocumentCausalityStore(restored.paths.sidecar).get_binding(
        binding.binding_id
    )
    assert restored_binding == binding


def test_truth_import_rejects_causality_for_another_store_before_publish(
    tmp_path: Path,
) -> None:
    source = _store(tmp_path / "source-wrong-identity")
    ledger = export_store(source)
    envelope = DocumentCausalityStore(source.paths.sidecar).export_recovery_bundle(
        store_id=source.store_id
    )
    envelope["store_id"] = "f" * 32
    companion = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    target = tmp_path / "rejected-recovery"
    target.mkdir()

    with pytest.raises(TruthImportError):
        import_store(
            ledger.path,
            target,
            registry=_EmptyRegistry(),
            causality_source=companion,
        )
    assert not (target / ".wbuddy" / "cowork" / "store.db").exists()
