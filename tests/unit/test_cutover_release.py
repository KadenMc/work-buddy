from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from work_buddy.cutover_maintenance import CutoverMaintenanceError
from work_buddy.cutover_release import validate_configured_postseal_evidence
from work_buddy.index.cutover_checkpoint import checkpoint_search_cutover_databases
from work_buddy.utils.index_lock import is_locked
from work_buddy.vault_index.authority_exclusions import normalized_path


def _authority(path, table: str, column: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"CREATE TABLE {table} (singleton INTEGER PRIMARY KEY, {column} TEXT)"
        )
        connection.execute(
            f"INSERT INTO {table}(singleton,{column}) VALUES(1,?)", (value,)
        )


def _path_sha(path) -> str:
    return hashlib.sha256(
        normalized_path(path, real=True).encode("utf-8")
    ).hexdigest()


def _write(path, value) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def test_release_recertifies_configured_paths_and_exact_search_receipts(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    db = data / "db"
    roots = {
        "journal": vault / "journal",
        "projects": vault / "projects",
        "contracts": vault / "contracts",
        "personal": vault / "personal",
    }
    for root in roots.values():
        root.mkdir(parents=True)
    project_db = db / "projects.db"
    index_db = db / "index-consolidated.db"
    _authority(db / "journal_capture.db", "journal_authority_control", "mode", "database_only")
    _authority(project_db, "project_authority_state", "authority", "sqlite")
    _authority(db / "contracts.db", "contract_authority", "state", "native")
    _authority(
        db / "personal_knowledge.db",
        "personal_knowledge_authority",
        "authority",
        "sqlite",
    )
    with sqlite3.connect(index_db) as connection:
        connection.execute("CREATE TABLE marker (value INTEGER)")
    cfg = {
        "vault_root": str(vault),
        "paths": {"data_root": str(data)},
        "obsidian": {"journal_dir": str(roots["journal"])},
        "projects": {
            "markdown_dir": str(roots["projects"]),
            "db_path": str(project_db),
        },
        "contracts": {"vault_path": str(roots["contracts"])},
        "personal_knowledge": {"vault_path": str(roots["personal"])},
        "index": {"enabled": True, "db_path": str(index_db)},
    }
    search = {
        "schema": "wb.search-cutover-evidence/v1",
        "index_available": True,
        "build_in_progress": False,
        "partitions": {"projects": {"ready": True}},
        "ready": True,
    }
    detachment = {
        "schema": "wb.legacy-root-detachment-evidence/v1",
        "mode": "sustained",
        "roots": [],
        "ready": True,
    }
    checkpoint = checkpoint_search_cutover_databases(
        cfg=cfg,
        domains=("projects",),
        index_db_path=index_db,
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    search_path = tmp_path / "search.json"
    detachment_path = tmp_path / "detachment.json"
    _write(checkpoint_path, checkpoint)
    _write(search_path, search)
    _write(detachment_path, detachment)
    monkeypatch.setattr("work_buddy.cutover_release.load_config", lambda: cfg)
    build_gate = index_db.parent / f"{index_db.name}.build"

    def certify_while_locked(**_kwargs):
        assert is_locked(build_gate)
        return {"search": search, "detachment": detachment}

    monkeypatch.setattr(
        "work_buddy.cutover_release.certify_search_cutover",
        certify_while_locked,
    )

    evidence = validate_configured_postseal_evidence(
        domain="projects",
        authority_db_path=project_db,
        checkpoint_evidence_path=checkpoint_path,
        search_evidence_path=search_path,
        detachment_evidence_path=detachment_path,
    )
    assert set(evidence) == {
        "databaseCheckpoint",
        "search",
        "detachment",
        "authorityHead",
    }
    assert all(len(value) == 64 for value in evidence.values())

    forged = dict(checkpoint)
    forged.pop("database_heads")
    _write(checkpoint_path, forged)
    with pytest.raises(CutoverMaintenanceError, match="heads are unavailable"):
        validate_configured_postseal_evidence(
            domain="projects",
            authority_db_path=project_db,
            checkpoint_evidence_path=checkpoint_path,
            search_evidence_path=search_path,
            detachment_evidence_path=detachment_path,
        )
    _write(checkpoint_path, checkpoint)

    changed = dict(checkpoint)
    changed_heads = dict(checkpoint["database_heads"])
    changed_rows = [dict(row) for row in changed_heads["databases"]]
    changed_rows[0]["database_sha256"] = "0" * 64
    changed_heads["databases"] = changed_rows
    changed["database_heads"] = changed_heads
    _write(checkpoint_path, changed)
    with pytest.raises(CutoverMaintenanceError, match="do not match live state"):
        validate_configured_postseal_evidence(
            domain="projects",
            authority_db_path=project_db,
            checkpoint_evidence_path=checkpoint_path,
            search_evidence_path=search_path,
            detachment_evidence_path=detachment_path,
        )
    _write(checkpoint_path, checkpoint)

    _write(search_path, {**search, "ready": False})
    with pytest.raises(CutoverMaintenanceError, match="does not match live state"):
        validate_configured_postseal_evidence(
            domain="projects",
            authority_db_path=project_db,
            checkpoint_evidence_path=checkpoint_path,
            search_evidence_path=search_path,
            detachment_evidence_path=detachment_path,
        )

    _write(search_path, search)
    def mutate_index_during_certification(**_kwargs):
        assert is_locked(build_gate)
        with sqlite3.connect(index_db) as connection:
            connection.execute("INSERT INTO marker(value) VALUES(1)")
        return {"search": search, "detachment": detachment}

    monkeypatch.setattr(
        "work_buddy.cutover_release.certify_search_cutover",
        mutate_index_during_certification,
    )
    with pytest.raises(CutoverMaintenanceError, match="heads do not match live state"):
        validate_configured_postseal_evidence(
            domain="projects",
            authority_db_path=project_db,
            checkpoint_evidence_path=checkpoint_path,
            search_evidence_path=search_path,
            detachment_evidence_path=detachment_path,
        )

    # Recheckpoint the now-stable index before exercising the authority-store
    # race independently.
    checkpoint = checkpoint_search_cutover_databases(
        cfg=cfg,
        domains=("projects",),
        index_db_path=index_db,
    )
    _write(checkpoint_path, checkpoint)
    monkeypatch.setattr(
        "work_buddy.cutover_release.certify_search_cutover",
        certify_while_locked,
    )
    with sqlite3.connect(project_db) as connection:
        connection.execute("CREATE TABLE post_checkpoint_change (value INTEGER)")
    with pytest.raises(CutoverMaintenanceError, match="heads do not match live state"):
        validate_configured_postseal_evidence(
            domain="projects",
            authority_db_path=project_db,
            checkpoint_evidence_path=checkpoint_path,
            search_evidence_path=search_path,
            detachment_evidence_path=detachment_path,
        )


def test_release_rejects_checkpoint_path_lookalike(tmp_path, monkeypatch):
    project_db = tmp_path / "projects.db"
    index_db = tmp_path / "index.db"
    project_db.write_bytes(b"not a configured database")
    index_db.write_bytes(b"index")
    monkeypatch.setattr(
        "work_buddy.cutover_release.load_config",
        lambda: {
            "vault_root": str(tmp_path),
            "paths": {"data_root": str(tmp_path / "data")},
            "projects": {"db_path": str(tmp_path / "configured.db")},
            "index": {"db_path": str(index_db)},
        },
    )
    with pytest.raises(CutoverMaintenanceError, match="configured authority"):
        validate_configured_postseal_evidence(
            domain="projects",
            authority_db_path=project_db,
            checkpoint_evidence_path=tmp_path / "checkpoint.json",
            search_evidence_path=tmp_path / "search.json",
            detachment_evidence_path=tmp_path / "detachment.json",
        )
