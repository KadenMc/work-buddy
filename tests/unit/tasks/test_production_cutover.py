from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.unit.tasks.test_migration_cutover import BACKUP_RECEIPTS, _operator
from tests.unit.tasks.test_migration_inventory import _manifest_csv
from work_buddy.sources import ActorRef
from work_buddy.cowork.local_files import LocalFileLinkError, LocalFileLinkRegistry
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.obsidian.tasks.migrations import TASK_MIGRATIONS as LEGACY_MIGRATIONS
from work_buddy.tasks.documents import TaskDocumentStoreManager
from work_buddy.tasks.import_legacy import (
    ACTIVATION_CONFIRMATION,
    LegacyTaskCutoverOperator,
    LegacyTaskDocumentImporter,
)
from work_buddy.tasks.migration import (
    CutoverPreconditionError,
    LegacyManifestEntry,
    canonical_sha256,
)
from work_buddy.tasks.production_cutover import (
    CANCEL_RETRIES_CONFIRMATION,
    RESTORE_RECEIPT_SCHEMA,
    ROLLBACK_REHEARSAL_SCHEMA,
    CutoverPaths,
    ProductionTaskCutover,
    load_accepted_inventory,
)
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.export import export_store


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
NOW = datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)


def _sha(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _write_rollback_rehearsal(cutover: ProductionTaskCutover) -> Path:
    stage = cutover._rollback_stage_snapshot()
    staging = cutover.paths.rollback_rehearsal.parent / "rollback-stage"
    tree = staging / "legacy-tree"
    notes = tree / "notes"
    notes.mkdir(parents=True)

    source_documents = []
    for row in stage["documents"]:
        content = f"# Rollback rehearsal {row['note_uuid']}\n"
        note = notes / f"{row['note_uuid']}.md"
        note.write_text(content, encoding="utf-8")
        encoded = content.encode("utf-8")
        source_documents.append(
            {
                **row,
                "ydoc_snapshot_sha256": row["structured_head_sha256"],
                "projection_sha256": hashlib.sha256(encoded).hexdigest(),
                "projection_byte_length": len(encoded),
            }
        )

    database = staging / "task_metadata.v11.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    LEGACY_MIGRATIONS.run(connection)
    current = sqlite3.connect(cutover.task_db_path)
    current.row_factory = sqlite3.Row
    try:
        idless = {
            str(row["task_id"]): json.loads(str(row["fields_json"]))
            for row in current.execute(
                "SELECT task_id, fields_json FROM task_migration_idless_stage "
                "WHERE cohort_id=?",
                (cutover.inventory.cohort_id,),
            )
        }
        live_rows = {
            str(row["task_id"]): dict(row)
            for row in current.execute("SELECT * FROM task_metadata")
        }
        legacy_columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(task_metadata)")
        )
        placeholders = ",".join("?" for _ in legacy_columns)
        for task_id in stage["task_ids"]:
            fields = dict(live_rows.get(task_id) or idless[task_id])
            fields.update(
                {
                    "task_id": task_id,
                    "state": fields.get("state") or "pending",
                    "urgency": fields.get("urgency") or "medium",
                    "description": fields.get("description") or f"Task {task_id}",
                    "created_at": fields.get("created_at") or NOW.isoformat(),
                    "updated_at": fields.get("updated_at") or NOW.isoformat(),
                    "current_action_item_id": None,
                    "task_kind": fields.get("task_kind") or "task",
                    "density": fields.get("density") or "sparse",
                    "creation_effort": fields.get("creation_effort") or "developed",
                    "user_involvement": fields.get("user_involvement") or "high",
                    "creation_provenance": fields.get("creation_provenance") or "manual",
                    "has_deadline": int(bool(fields.get("deadline_date"))),
                    "has_dependency": int(bool(fields.get("dependency_hint"))),
                }
            )
            connection.execute(
                f"INSERT INTO task_metadata ({','.join(legacy_columns)}) "
                f"VALUES ({placeholders})",
                tuple(fields.get(column) for column in legacy_columns),
            )
    finally:
        current.close()
    connection.execute("PRAGMA user_version=11")
    connection.commit()
    connection.close()

    verify = ProductionTaskCutover._verify_v11_rollback_database(database)
    legacy = sqlite3.connect(database)
    try:
        live_ids = [
            str(row[0])
            for row in legacy.execute(
                "SELECT task_id FROM task_metadata WHERE deleted_at IS NULL ORDER BY task_id"
            )
        ]
    finally:
        legacy.close()
    (tree / "master-task-list.md").write_text(
        "".join(f"- [ ] #todo Rehearsed 🆔 {task_id}\n" for task_id in live_ids),
        encoding="utf-8",
    )
    (tree / "archive.md").write_text("", encoding="utf-8")

    for asset in stage["local_file_links"]:
        source = cutover.paths.frozen_tree / asset["relative_path"]
        target = tree / asset["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    tree_files = []
    for path in sorted(tree.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_file():
            tree_files.append(
                {
                    "relative_path": path.relative_to(tree).as_posix(),
                    "byte_length": path.stat().st_size,
                    "sha256": _sha(path),
                }
            )
    manifest = {
        "schema": "wb.task-rollback-tree-manifest/v1",
        "files": tree_files,
        "tree_sha256": canonical_sha256(tree_files),
    }
    manifest_path = staging / "legacy-tree-manifest.json"
    exception_path = staging / "rollback-exceptions.json"
    supplement_path = staging / "native-supplement.json"
    _write_json(manifest_path, manifest)
    _write_json(exception_path, {"schema": "wb.task-rollback-exceptions/v1"})
    _write_json(supplement_path, {"schema": "wb.task-rollback-native-supplement/v1"})

    catalog = {
        "roots": stage["local_file_roots"],
        "links": [
            {**row, "created_at": NOW.isoformat()} for row in stage["local_file_links"]
        ],
        "verified_assets": [
            {
                "link_id": row["link_id"],
                "relative_path": row["relative_path"],
                "byte_length": row["byte_length"],
                "sha256": row["sha256"],
            }
            for row in stage["local_file_links"]
        ],
    }
    counts = {
        **verify["counts"],
        "master_lines": len(live_ids),
        "archive_lines": 0,
        "note_files": len(stage["documents"]),
        "recovered_note_files": sum(row["task_id"] is None for row in stage["documents"]),
        "tree_files": len(tree_files),
        "local_assets": len(stage["local_file_links"]),
    }
    receipt = {
        "schema": "wb.task-rollback-export/v1",
        "cohort_id": stage["cohort_id"],
        "rollback_authority_epoch": "rollback:2",
        "maintenance_stop_receipt": {"verified": True},
        "created_at": cutover.clock().isoformat(),
        "source_schema_version": 19,
        "source_authority_epoch": stage["target_authority_epoch"],
        "source_collection_revision": 1,
        "source_process_generation": stage["target_process_generation"],
        "source_inventory_sha256": stage["inventory_sha256"],
        "source_manifest_sha256": stage["manifest_sha256"],
        "source_root_fingerprint": stage["source_root_fingerprint"],
        "source_cowork_task_store_id": stage["cowork_task_store_id"],
        "source_database_snapshot_sha256": canonical_sha256({"clone": stage["stage_sha256"]}),
        "source_content_snapshot_sha256": canonical_sha256({"tasks": stage["task_ids"]}),
        "source_snapshot_sha256": canonical_sha256(
            {"stage": stage["stage_sha256"], "documents": source_documents, "catalog": catalog}
        ),
        "legacy_database_schema_version": 11,
        "legacy_database_file": database.name,
        "legacy_database_byte_length": database.stat().st_size,
        "legacy_database_sha256": _sha(database),
        "legacy_database_semantic_sha256": verify["semantic_sha256"],
        "legacy_tree_directory": "legacy-tree",
        "staged_tree_sha256": manifest["tree_sha256"],
        "tree_manifest_file": manifest_path.name,
        "tree_manifest_byte_length": manifest_path.stat().st_size,
        "tree_manifest_sha256": _sha(manifest_path),
        "exception_report_file": exception_path.name,
        "exception_report_byte_length": exception_path.stat().st_size,
        "exception_report_sha256": _sha(exception_path),
        "native_supplement_file": supplement_path.name,
        "native_supplement_byte_length": supplement_path.stat().st_size,
        "native_supplement_sha256": _sha(supplement_path),
        "source_documents": source_documents,
        "document_heads_sha256": canonical_sha256(source_documents),
        "source_local_file_catalog": catalog,
        "source_local_file_catalog_sha256": canonical_sha256(catalog),
        "counts": counts,
        "semantic_downgrade_count": 0,
        "verified": True,
    }
    receipt["receipt_id"] = "trr_" + canonical_sha256(receipt)[:32]
    receipt_path = staging / "rollback-export-receipt.json"
    _write_json(receipt_path, receipt)
    compact = {
        "schema": "wb.task-rollback-rehearsal-evidence/v1",
        "receipt_file": str(receipt_path),
        "receipt_byte_length": receipt_path.stat().st_size,
        "receipt_sha256": _sha(receipt_path),
        "receipt_id": receipt["receipt_id"],
        **{key: receipt[key] for key in (
            "cohort_id", "source_inventory_sha256", "source_manifest_sha256",
            "source_cowork_task_store_id", "source_snapshot_sha256",
            "source_database_snapshot_sha256", "document_heads_sha256",
            "source_local_file_catalog_sha256", "staged_tree_sha256",
            "legacy_database_sha256", "legacy_database_semantic_sha256", "counts",
        )},
    }
    backup = cutover._backup_evidence()["artifacts"]["work_buddy_data_snapshot"]
    companion = {
        "schema": ROLLBACK_REHEARSAL_SCHEMA,
        "completed_at": cutover.clock().isoformat(),
        "cohort_id": stage["cohort_id"],
        "inventory_sha256": stage["inventory_sha256"],
        "manifest_sha256": stage["manifest_sha256"],
        "production_stage_sha256": stage["stage_sha256"],
        "backup_snapshot_sha256": backup["sha256"],
        "restore_receipt_sha256": _sha(cutover.paths.restore_rehearsal),
        "export_evidence": compact,
    }
    companion["payload_sha256"] = canonical_sha256(companion)
    _write_json(cutover.paths.rollback_rehearsal, companion)
    return database


def _cutover_fixture(tmp_path: Path):
    shadow_operator, shadow_importer, task_store, sources, stores = _operator(tmp_path)
    try:
        shadow_operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
    finally:
        shadow_importer.close()

    connection = task_store.connect()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()

    source = shadow_operator.source_root
    manifest = tuple(
        LegacyManifestEntry(
            path.relative_to(source).as_posix(),
            path.stat().st_size,
            _sha(path),
        )
        for path in sorted(candidate for candidate in source.rglob("*") if candidate.is_file())
    )
    manifest_path = tmp_path / "receipts" / "legacy-files.csv"
    manifest_path.parent.mkdir(parents=True)
    _manifest_csv(manifest_path, manifest)

    legacy_tree = source
    frozen_tree = (
        tmp_path / "vault" / "_frozen" / "work-buddy-task-tree-cutover-1" / "tasks"
    )
    frozen_tree.parent.mkdir(parents=True)
    source.rename(frozen_tree)

    inventory = load_accepted_inventory(
        task_store.path,
        cohort_id=shadow_operator.inventory.cohort_id,
    )
    assert inventory.inventory_sha256 == shadow_operator.inventory.inventory_sha256

    tree_zip = tmp_path / "receipts" / "legacy-tree.zip"
    with zipfile.ZipFile(tree_zip, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for entry in manifest:
            bundle.write(frozen_tree / entry.relative_path, f"tasks/{entry.relative_path}")

    snapshot = tmp_path / "receipts" / "fresh-snapshot.tar.gz"
    snapshot_manifest = tmp_path / "snapshot-MANIFEST.json"
    snapshot_manifest.write_text(
        json.dumps(
            {
                "snapshot_ts": "2026-08-24T14-59-00Z",
                "row_counts": {
                    "tasks": {
                        "task_metadata": inventory.counts["database_tasks"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with tarfile.open(snapshot, "w:gz") as bundle:
        bundle.add(task_store.path, arcname="task_metadata.db")
        bundle.add(snapshot_manifest, arcname="MANIFEST.json")

    backup_receipts = tmp_path / "receipts" / "backups.json"
    _write_json(
        backup_receipts,
        [
            {
                "kind": "work_buddy_data_snapshot",
                "path": str(snapshot),
                "sha256": _sha(snapshot),
                "size_bytes": snapshot.stat().st_size,
                "created_at": "2026-08-24T14:59:00+00:00",
            },
            {
                "kind": "exact_legacy_task_tree",
                "path": str(tree_zip),
                "sha256": _sha(tree_zip),
                "entry_count": len(manifest),
            },
            {
                "kind": "legacy_task_tree_manifest",
                "path": str(manifest_path),
                "sha256": _sha(manifest_path),
                "row_count": len(manifest),
            },
        ],
    )

    restored_dir = tmp_path / "restored"
    restored_dir.mkdir(parents=True)
    cowork_export = tmp_path / "restored" / "cowork-export.bin"
    causality_export = tmp_path / "restored" / "causality-export.bin"
    cowork_store = stores.open_existing()
    export_store(cowork_store, cowork_export)
    causality_export.write_text(
        json.dumps(
            DocumentCausalityStore(cowork_store.paths.sidecar).export_recovery_bundle(
                store_id=cowork_store.store_id
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    restore_receipt = tmp_path / "receipts" / "restore.json"
    _write_json(
        restore_receipt,
        {
            "schema": RESTORE_RECEIPT_SCHEMA,
            "cohort_id": inventory.cohort_id,
            "inventory_sha256": inventory.inventory_sha256,
            "manifest_sha256": inventory.manifest_sha256,
            "completed_at": "2026-08-24T14:59:30+00:00",
            "restored_task_db_sha256": _sha(task_store.path),
            "restored_from_sha256": _sha(snapshot),
            "portable_artifacts": [
                {
                    "kind": "cowork_task_store",
                    "path": str(cowork_export),
                    "sha256": _sha(cowork_export),
                },
                {
                    "kind": "task_causality",
                    "path": str(causality_export),
                    "sha256": _sha(causality_export),
                },
            ],
        },
    )

    state = tmp_path / "runtime" / "sidecar_state.json"
    _write_json(
        state,
        {
            "pid": 900001,
            "services": {
                "mcp_gateway": {"pid": 900002},
                "dashboard": {"pid": 900003},
            },
        },
    )
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "task-sync.md").write_text(
        "---\nenabled: false\ntype: capability\ncapability: task_sync\nparams: {}\n---\n",
        encoding="utf-8",
    )
    (jobs / "task-note-index.md").write_text(
        "---\nenabled: true\ntype: capability\ncapability: ir_index\nparams:\n  source: task_note\n---\n",
        encoding="utf-8",
    )
    operations = tmp_path / "operations"
    operations.mkdir()
    stop_receipt = tmp_path / "receipts" / "process-stop.json"
    cutover_paths = CutoverPaths(
        manifest=manifest_path,
        frozen_tree=frozen_tree,
        legacy_tree=legacy_tree,
        backup_receipts=backup_receipts,
        restore_rehearsal=restore_receipt,
        rollback_rehearsal=tmp_path / "receipts" / "rollback-rehearsal.json",
        process_stop_receipt=stop_receipt,
        receipts=tmp_path / "operator-receipts",
        root_bindings=tmp_path / "runtime" / "local-roots.db",
        operations=operations,
        sidecar_state=state,
        sidecar_pid=tmp_path / "runtime" / "sidecar.pid",
        tray_pid=tmp_path / "runtime" / "tray.pid",
        job_roots=(jobs,),
    )
    common = {
        "inventory": inventory,
        "task_db_path": task_store.path,
        "cutover_paths": cutover_paths,
        "clock": lambda: NOW,
        "process_alive": lambda _pid: False,
        "process_lister": lambda: (),
        "acl_probe": lambda _root: {
            "verified": True,
            "filesystem_type": "NTFS",
            "root_acl_protected": True,
            "acl_tree_sha256": "a" * 64,
        },
    }
    cutover = ProductionTaskCutover(**common)
    cutover.capture_process_stop_receipt()

    principal = ActorRef(
        sources.authority_id,
        "production-task-cutover-test",
        "service",
        "task-migration-tenant",
    )
    resumed_stores = TaskDocumentStoreManager(
        root=tmp_path / "cowork-tasks",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    importer = LegacyTaskDocumentImporter(
        source_root=frozen_tree,
        sources=sources,
        principal=principal,
        stores=resumed_stores,
    )
    operator = LegacyTaskCutoverOperator(
        inventory=inventory,
        source_root=frozen_tree,
        task_store=task_store,
        document_importer=importer,
        actor="operator:production-test",
        session_id="session-production-test",
    )
    return ProductionTaskCutover(**common, operator=operator), importer, operations, task_store


def test_status_is_read_only_and_prepare_records_independently_verified_gates(tmp_path):
    cutover, importer, _operations, task_store = _cutover_fixture(tmp_path)
    try:
        before = task_store.path.read_bytes()
        status = cutover.status()
        after = task_store.path.read_bytes()
        assert status["read_only"] is True
        assert status["ready_for_prepare"] is True, json.dumps(status["checks"], indent=2)
        assert status["ready_for_activate"] is False
        assert before == after
        frozen = status["checks"]["frozen_tree_sealed"]["evidence"]
        assert frozen["acl_scope"] == "dedicated_wrapper_and_descendants"
        assert Path(frozen["tree"]["acl_wrapper"]) == cutover.paths.frozen_tree.parent

        result = cutover.prepare(target_authority_epoch="native:1")
        assert result["cohort"]["state"] == "prepared"
        assert Path(result["operator_receipt"]).is_file()
        # A stop receipt is a continuously revalidated generation identity,
        # not a 15-minute lease that can strand a long binding pass.
        cutover.clock = lambda: NOW + timedelta(minutes=45)
        bindings = cutover.apply_and_verify_bindings()
        assert bindings["applied"] == bindings["verified"] == 2
        assert Path(bindings["operator_receipt"]).is_file()
        _write_rollback_rehearsal(cutover)
        activated = cutover.activate(confirmation=ACTIVATION_CONFIRMATION)
        assert activated["cohort"]["state"] == "active"
        assert Path(activated["operator_receipt"]).is_file()
        active_status = cutover.status()
        assert active_status["ready_for_activate"] is True, json.dumps(
            active_status["checks"], indent=2
        )
        replay = cutover.activate(confirmation=ACTIVATION_CONFIRMATION)
        assert replay["cohort"]["cutover_receipt_id"] == activated["cohort"]["cutover_receipt_id"]

        registry = LocalFileLinkRegistry(task_store.path, cutover.paths.root_bindings)
        conn = task_store.connect()
        try:
            linked = conn.execute(
                "SELECT store_id, document_id, link_id, root_id "
                "FROM task_local_file_links ORDER BY link_id LIMIT 1"
            ).fetchone()
            root_status = conn.execute(
                "SELECT status FROM task_local_file_roots WHERE root_id=?",
                (linked["root_id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        assert root_status == "active"
        assert registry.get_root(linked["root_id"]).root == cutover.paths.frozen_tree.resolve()
        link = registry.get_document_link(
            store_id=linked["store_id"],
            document_id=linked["document_id"],
            link_id=linked["link_id"],
        )
        assert registry.verified_path(link).is_file()
    finally:
        importer.close()

    with task_store.connect() as conn:
        gates = {
            row["gate_name"]: bool(row["passed"])
            for row in conn.execute(
                "SELECT gate_name, passed FROM task_migration_gates "
                "WHERE cohort_id=?",
                (cutover.inventory.cohort_id,),
            )
        }
        system = conn.execute(
            "SELECT authority_epoch, rollback_fence FROM task_system_state WHERE id=1"
        ).fetchone()
    assert gates["legacy_mutation_fenced"] is True
    assert gates["frozen_tree_sealed"] is True
    assert gates["backup_restore_rehearsal"] is True
    assert gates["binding_cohort_verified"] is True
    assert system["authority_epoch"] == "native:1"
    assert bool(system["rollback_fence"]) is False


def test_activate_requires_explicit_binding_and_replays_recovery_checkpoint(tmp_path):
    cutover, importer, _operations, task_store = _cutover_fixture(tmp_path)
    try:
        cutover.prepare(target_authority_epoch="native:1")
        with pytest.raises(CutoverPreconditionError, match="explicit bindings action"):
            cutover.activate(confirmation=ACTIVATION_CONFIRMATION)
        with task_store.connect() as connection:
            state = connection.execute(
                "SELECT state FROM task_migration_cohorts WHERE cohort_id=?",
                (cutover.inventory.cohort_id,),
            ).fetchone()[0]
            transitions = connection.execute(
                "SELECT COUNT(*) FROM task_migration_binding_transitions "
                "WHERE cohort_id=?",
                (cutover.inventory.cohort_id,),
            ).fetchone()[0]
        assert state == "prepared"
        assert transitions == 0

        cutover.apply_and_verify_bindings()
        stage_before = cutover._rollback_stage_snapshot()["stage_sha256"]
        with task_store.transaction() as connection:
            connection.execute(
                "UPDATE task_migration_binding_transitions SET applied_at=? "
                "WHERE cohort_id=?",
                ((NOW + timedelta(seconds=30)).isoformat(), cutover.inventory.cohort_id),
            )
        assert cutover._rollback_stage_snapshot()["stage_sha256"] == stage_before
        recovery = cutover.status()["checks"]["backup_restore_rehearsal"]
        assert recovery["passed"] is True, recovery["problems"]
        assert recovery["evidence"]["restore"]["portable_restore"][
            "binding_transition_replay_count"
        ] == 2
        missing_rollback = cutover.status()["checks"]["rollback_rehearsal_verified"]
        assert missing_rollback["passed"] is False
        with pytest.raises(CutoverPreconditionError, match="preflight failed"):
            cutover.activate(confirmation=ACTIVATION_CONFIRMATION)
        _write_rollback_rehearsal(cutover)
        stage = cutover._rollback_stage_snapshot()
        recovered = [row for row in stage["documents"] if row["task_id"] is None]
        assert recovered and {row["lifecycle"] for row in recovered} == {"recovery"}
        companion = json.loads(
            cutover.paths.rollback_rehearsal.read_text(encoding="utf-8")
        )
        export_receipt = json.loads(
            Path(companion["export_evidence"]["receipt_file"]).read_text(encoding="utf-8")
        )
        assert {
            row["lifecycle"]
            for row in export_receipt["source_documents"]
            if row["task_id"] is None
        } == {"recovery"}
        assert cutover.status()["checks"]["rollback_rehearsal_verified"]["passed"] is True
        assert cutover.activate(confirmation=ACTIVATION_CONFIRMATION)["cohort"]["state"] == "active"
    finally:
        importer.close()


def test_tampered_rollback_v11_artifact_blocks_activation(tmp_path):
    cutover, importer, _operations, _task_store = _cutover_fixture(tmp_path)
    try:
        cutover.prepare(target_authority_epoch="native:1")
        cutover.apply_and_verify_bindings()
        database = _write_rollback_rehearsal(cutover)
        assert cutover.status()["checks"]["rollback_rehearsal_verified"]["passed"] is True
        with database.open("ab") as stream:
            stream.write(b"tampered")
        failed = cutover.status()["checks"]["rollback_rehearsal_verified"]
        assert failed["passed"] is False
        assert "database changed" in failed["problems"][0]
        with pytest.raises(CutoverPreconditionError, match="preflight failed"):
            cutover.activate(confirmation=ACTIVATION_CONFIRMATION)
    finally:
        importer.close()


def test_capture_stop_after_bindings_returns_bound_receipt_without_overwrite(tmp_path):
    cutover, importer, _operations, task_store = _cutover_fixture(tmp_path)
    try:
        cutover.prepare(target_authority_epoch="native:1")
        cutover.apply_and_verify_bindings()
        before = cutover.paths.process_stop_receipt.read_bytes()
        original = json.loads(before)
        with task_store.connect() as connection:
            bound_fence = connection.execute(
                "SELECT fence_receipt_id FROM task_migration_cohorts WHERE cohort_id=?",
                (cutover.inventory.cohort_id,),
            ).fetchone()[0]

        replay = cutover.capture_process_stop_receipt()
        assert replay["replayed"] is True
        assert replay["payload_sha256"] == original["payload_sha256"]
        assert bound_fence == "fence_" + original["payload_sha256"][:32]
        assert cutover.paths.process_stop_receipt.read_bytes() == before

        sync_job = cutover.paths.job_roots[0] / "task-sync.md"
        sync_job.write_text(
            "---\nenabled: true\ntype: capability\n"
            "capability: task_sync\nparams: {}\n---\n",
            encoding="utf-8",
        )
        with pytest.raises(CutoverPreconditionError, match="Legacy task producers"):
            cutover.capture_process_stop_receipt()
        assert cutover.paths.process_stop_receipt.read_bytes() == before
    finally:
        importer.close()


def test_wrapped_queued_legacy_task_retry_invalidates_stop_receipt(tmp_path):
    cutover, importer, operations, _task_store = _cutover_fixture(tmp_path)
    try:
        _write_json(
            operations / "op_inner.json",
            {
                "operation_id": "op_inner",
                "name": "task_create",
                "params": {"task_text": "stale"},
                "task_authority_epoch": "legacy",
            },
        )
        _write_json(
            operations / "op_wrapper.json",
            {
                "operation_id": "op_wrapper",
                "name": "obsidian_retry",
                "params": {"operation_id": "op_inner"},
                "queued": True,
                "status": "failed",
            },
        )
        status = cutover.status()
    finally:
        importer.close()

    process_gate = status["checks"]["process_generations_stopped"]
    assert process_gate["passed"] is False
    assert "Queued legacy task mutations" in process_gate["problems"][0]


def test_native_task_note_indexer_is_allowed_but_legacy_sync_is_not(tmp_path):
    cutover, importer, _operations, _task_store = _cutover_fixture(tmp_path)
    try:
        initial = cutover.status()
        stop_evidence = initial["checks"]["process_generations_stopped"]["evidence"]
        assert initial["checks"]["process_generations_stopped"]["passed"] is True
        assert [row["path"] for row in stop_evidence["producer_jobs"]] == [
            "task-sync.md"
        ]

        cutover.process_lister = lambda: (
            {
                "pid": 880001,
                "name": "python.exe",
                "command_line": (
                    f"python {Path.cwd() / 'work_buddy' / 'sidecar' / 'daemon.py'}"
                ),
                "executable_path": str(Path.cwd() / ".venv" / "Scripts" / "python.exe"),
            },
        )
        untracked = cutover.status()
        assert untracked["checks"]["process_generations_stopped"]["passed"] is False
        assert "Untracked Work Buddy-capable" in untracked["checks"][
            "process_generations_stopped"
        ]["problems"][0]
        cutover.process_lister = lambda: ()

        sync_job = cutover.paths.job_roots[0] / "task-sync.md"
        sync_job.write_text(
            "---\nenabled: true\ntype: capability\ncapability: task_sync\nparams: {}\n---\n",
            encoding="utf-8",
        )
        blocked = cutover.status()
    finally:
        importer.close()

    process_gate = blocked["checks"]["process_generations_stopped"]
    assert process_gate["passed"] is False
    assert "Legacy task producers are still enabled" in process_gate["problems"][0]


def test_only_the_verified_operator_ancestor_chain_is_exempt(tmp_path):
    cutover, importer, _operations, _task_store = _cutover_fixture(tmp_path)
    try:
        parent_pid = os.getppid()
        grandparent_pid = parent_pid + 100_000
        sibling_pid = parent_pid + 200_000
        ancestor_rows = (
            {
                "pid": parent_pid,
                "parent_pid": grandparent_pid,
                "name": "uv.exe",
                "command_line": f"uv run --no-sync python {Path.cwd() / 'work_buddy'}",
                "executable_path": "C:/tools/uv.exe",
            },
            {
                "pid": grandparent_pid,
                "parent_pid": 0,
                "name": "pwsh.exe",
                "command_line": "pwsh",
                "executable_path": "C:/Program Files/PowerShell/7/pwsh.exe",
            },
        )
        cutover.process_lister = lambda: ancestor_rows
        evidence = cutover._stopped_process_evidence()
        assert any(
            row["pid"] == parent_pid and row["name"] == "uv.exe"
            for row in evidence["operator_ancestor_chain"]
        )
        assert len(evidence["operator_ancestor_chain_sha256"]) == 64

        cutover.process_lister = lambda: (
            *ancestor_rows,
            {
                "pid": sibling_pid,
                "parent_pid": grandparent_pid,
                "name": "uv.exe",
                "command_line": f"uv run python {Path.cwd() / 'work_buddy' / 'tasks'}",
                "executable_path": "C:/tools/uv.exe",
            },
        )
        with pytest.raises(CutoverPreconditionError, match="Untracked Work Buddy-capable"):
            cutover._stopped_process_evidence()
    finally:
        importer.close()


def test_stop_receipt_accepts_only_the_exact_completed_rollback_generation(tmp_path):
    cutover, importer, _operations, task_store = _cutover_fixture(tmp_path)
    try:
        with task_store.transaction() as connection:
            connection.execute(
                "UPDATE task_migration_cohorts SET state='rolled_back', "
                "rollback_authority_epoch='rollback:2', expected_process_generation=0 "
                "WHERE cohort_id=?",
                (cutover.inventory.cohort_id,),
            )
            connection.execute(
                "UPDATE task_system_state SET authority_epoch='rollback:2', "
                "process_generation=1 WHERE id=1"
            )

        evidence = cutover._stop_receipt_evidence()
        assert evidence["process_generation"] == 0
        assert evidence["observed_process_generation"] == 1
        assert evidence["generation_advanced_by_activation"] is False
        assert evidence["generation_advanced_by_rollback"] is True

        with task_store.transaction() as connection:
            connection.execute(
                "UPDATE task_system_state SET authority_epoch='rollback:3' WHERE id=1"
            )
        with pytest.raises(CutoverPreconditionError, match="process generation changed"):
            cutover._stop_receipt_evidence()
    finally:
        importer.close()


def test_retry_cancellation_is_guarded_selective_receipted_and_idempotent(tmp_path):
    cutover, importer, operations, _task_store = _cutover_fixture(tmp_path)
    try:
        records = {
            "op_direct": {
                "operation_id": "op_direct",
                "name": "task_update_description",
                "params": {"task_id": "legacy-task"},
                "task_authority_epoch": "legacy",
                "queued": False,
                "queued_for_retry": True,
                "status": "failed",
                "retry_at": "2026-08-24T15:01:00+00:00",
            },
            "op_inner": {
                "operation_id": "op_inner",
                "name": "task_create",
                "params": {"task_text": "legacy wrapped"},
                "task_authority_epoch": "legacy",
                "status": "failed",
            },
            "op_wrapper": {
                "operation_id": "op_wrapper",
                "name": "obsidian_retry",
                "params": {"operation_id": "op_inner"},
                "queued": True,
                "status": "failed",
            },
            "op_native": {
                "operation_id": "op_native",
                "name": "task_delete",
                "params": {"task_id": "native-task"},
                "task_authority_epoch": "native:9",
                "queued": True,
                "status": "failed",
            },
            "op_other": {
                "operation_id": "op_other",
                "name": "calendar_create",
                "params": {"summary": "leave me alone"},
                "queued": True,
                "status": "failed",
            },
        }
        for operation_id, record in records.items():
            _write_json(operations / f"{operation_id}.json", record)
        before = {
            operation_id: (operations / f"{operation_id}.json").read_bytes()
            for operation_id in records
        }

        with pytest.raises(Exception, match="exact operator confirmation"):
            cutover.cancel_legacy_retries(confirmation="yes")
        assert {
            operation_id: (operations / f"{operation_id}.json").read_bytes()
            for operation_id in records
        } == before

        result = cutover.cancel_legacy_retries(
            confirmation=CANCEL_RETRIES_CONFIRMATION
        )
        assert result["cancelled"] == 2
        assert result["replayed"] is False
        assert result["pending_legacy_task_retries"] == []
        assert len(result["receipts"]) == 1

        for operation_id in ("op_direct", "op_wrapper"):
            cancelled = json.loads(
                (operations / f"{operation_id}.json").read_text(encoding="utf-8")
            )
            assert cancelled["status"] == "cancelled"
            assert cancelled["queued"] is False
            assert cancelled["queued_for_retry"] is False
            assert cancelled["cancelled_for_cohort_id"] == cutover.inventory.cohort_id
            assert cancelled["cancelled_reason"].startswith("native_task_cutover_")
        for operation_id in ("op_inner", "op_native", "op_other"):
            assert (operations / f"{operation_id}.json").read_bytes() == before[operation_id]

        receipt = json.loads(Path(result["receipts"][0]).read_text(encoding="utf-8"))
        originals = {
            item["operation_id"]: base64.b64decode(item["original_bytes_base64"])
            for item in receipt["records"]
        }
        assert originals == {
            "op_direct": before["op_direct"],
            "op_wrapper": before["op_wrapper"],
        }

        after_first = {
            operation_id: (operations / f"{operation_id}.json").read_bytes()
            for operation_id in records
        }
        replay = cutover.cancel_legacy_retries(
            confirmation=CANCEL_RETRIES_CONFIRMATION
        )
        assert replay["cancelled"] == 0
        assert replay["replayed"] is True
        assert {
            operation_id: (operations / f"{operation_id}.json").read_bytes()
            for operation_id in records
        } == after_first
        assert cutover.status()["checks"]["process_generations_stopped"]["passed"] is True
    finally:
        importer.close()


def test_tree_path_and_portable_restore_cannot_be_waived_by_receipt(tmp_path):
    cutover, importer, _operations, _task_store = _cutover_fixture(tmp_path)
    try:
        surprise = cutover.paths.frozen_tree / "notes" / "surprise.md"
        surprise.write_text("not in manifest", encoding="utf-8")
        status = cutover.status()
        surprise.unlink()

        accepted_paths = cutover.paths
        cutover.paths = replace(
            accepted_paths,
            legacy_tree=tmp_path / "arbitrary-absent-legacy-tree",
        )
        wrong_legacy_path = cutover.status()
        cutover.paths = accepted_paths

        restore_receipt = json.loads(
            cutover.paths.restore_rehearsal.read_text(encoding="utf-8")
        )
        cowork = next(
            item
            for item in restore_receipt["portable_artifacts"]
            if item["kind"] == "cowork_task_store"
        )
        cowork_path = Path(cowork["path"])
        cowork_path.write_bytes(b"not a portable Co-work export")
        cowork["sha256"] = _sha(cowork_path)
        _write_json(cutover.paths.restore_rehearsal, restore_receipt)
        bogus_portable = cutover.status()
    finally:
        importer.close()

    inventory_gate = status["checks"]["inventory_parity"]
    frozen_gate = status["checks"]["frozen_tree_sealed"]
    assert inventory_gate["passed"] is False
    assert frozen_gate["passed"] is False
    assert "membership differs" in inventory_gate["problems"][0]
    assert wrong_legacy_path["checks"]["inventory_parity"]["passed"] is False
    assert "not the accepted inventory source path" in wrong_legacy_path["checks"][
        "inventory_parity"
    ]["problems"][0]
    assert bogus_portable["checks"]["backup_restore_rehearsal"]["passed"] is False
    assert "cannot be restored" in bogus_portable["checks"][
        "backup_restore_rehearsal"
    ]["problems"][0]


def test_local_link_collision_aborts_activation_and_root_is_disabled(tmp_path):
    cutover, importer, _operations, task_store = _cutover_fixture(tmp_path)
    try:
        cutover.prepare(target_authority_epoch="native:1")
        cutover.apply_and_verify_bindings()
        _write_rollback_rehearsal(cutover)
        conn = task_store.connect()
        try:
            staged = conn.execute(
                "SELECT * FROM task_migration_local_link_stage ORDER BY link_id LIMIT 1"
            ).fetchone()
            conn.execute(
                "INSERT INTO task_local_file_links ("
                "link_id,task_id,store_id,document_id,root_id,relative_path,"
                "display_name,suffix,media_type,byte_length,sha256,sensitivity,"
                "allowed_action,policy_revision,source_receipt_id,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    staged["link_id"],
                    None,
                    staged["store_id"],
                    staged["document_id"],
                    staged["root_id"],
                    "wrong/collision.pdf",
                    staged["display_name"],
                    staged["suffix"],
                    staged["media_type"],
                    staged["byte_length"],
                    staged["sha256"],
                    staged["sensitivity"],
                    staged["allowed_action"],
                    staged["policy_revision"],
                    staged["source_receipt_id"],
                    NOW.isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(CutoverPreconditionError, match="collides"):
            cutover.activate(confirmation=ACTIVATION_CONFIRMATION)
        aborted = cutover.abort_before_activation()
        assert aborted["cohort"]["state"] == "aborted"

        conn = task_store.connect()
        try:
            status = conn.execute(
                "SELECT status FROM task_local_file_roots WHERE root_id=?",
                (staged["root_id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        assert status == "aborted"
        registry = LocalFileLinkRegistry(task_store.path, cutover.paths.root_bindings)
        with pytest.raises(LocalFileLinkError):
            registry.get_root(staged["root_id"])
    finally:
        importer.close()


@pytest.mark.skipif(sys.platform != "win32", reason="WinPS smoke test is Windows-only")
def test_windows_acl_probe_script_is_compatible_with_windows_powershell_51(tmp_path):
    root = tmp_path / "acl-smoke"
    root.mkdir()
    (root / "child.txt").write_text("smoke", encoding="utf-8")
    with pytest.raises(CutoverPreconditionError) as raised:
        ProductionTaskCutover._probe_windows_acl(root)
    assert "Could not verify" not in str(raised.value)
