"""Backup coverage for the truth registry and registered stores."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from pathlib import Path

from work_buddy.truth.identity import new_id
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import TruthStore


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
) -> TruthStore:
    root.mkdir()
    store_id = new_id()
    return TruthStore.create(
        root,
        _profile(store_id, export_committed=export_committed),
    )


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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
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
    unavailable = _store(tmp_path / "unavailable")
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
    assert by_id[included.store_id]["backup_status"] == "included"
    assert by_id[unavailable_id]["backup_status"] == "unreachable"
    assert result["unreachable_truth_stores"] == [by_id[unavailable_id]]
    assert result["truth_store_errors"] == []

    with tarfile.open(result["tarball_path"], "r:gz") as archive:
        names = set(archive.getnames())
        profile_member = f"truth_stores/{included.store_id}/store.yaml"
        export_member = f"truth_stores/{included.store_id}/claims.jsonl"
        assert profile_member in names
        assert export_member in names
        assert f"truth_stores/{unavailable_id}/claims.jsonl" not in names
        export_bytes = archive.extractfile(export_member).read()
        assert archive.extractfile(profile_member).read()

    assert hashlib.sha256(export_bytes).hexdigest() == by_id[included.store_id][
        "export_sha256"
    ]
    assert json.loads(export_bytes.splitlines()[0])["store_info"]["store_id"] == (
        included.store_id
    )


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
