from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from work_buddy.obsidian.tasks.migrations import TASK_MIGRATIONS as LEGACY_MIGRATIONS
from work_buddy.tasks import rollback_export as rollback_export_module
from work_buddy.tasks.migration import TaskMigrationLedger
from work_buddy.tasks.rollback_export import (
    DateConflictResolution,
    ROLLBACK_EXPORT_CONFIRMATION,
    ReverseLegacyTaskExportOperator,
    RollbackExportBlocked,
    RollbackExportVerificationError,
)
from work_buddy.tasks.runtime import (
    activation_authority_latch_path,
    arm_native_authority_latch,
)
from work_buddy.tasks.store import TaskStore


NOW = "2026-08-24T12:00:00+00:00"
NOTE_LIVE = "11111111-1111-4111-8111-111111111111"
NOTE_DELETED = "22222222-2222-4222-8222-222222222222"
NOTE_RECOVERED = "33333333-3333-4333-8333-333333333333"


def _canonical_sha(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_stop_receipt(
    path: Path,
    *,
    cohort_id: str = "cohort-native",
    process_generation: int = 7,
) -> Path:
    payload = {
        "schema": "wb.native-task-process-stop/v1",
        "cohort_id": cohort_id,
        "captured_at": NOW,
        "evidence": {
            "schema": "wb.native-task-live-stop-verification/v1",
            "process_generation": process_generation,
            "tracked_processes": [],
            "obsidian_pids": [],
            "untracked_work_buddy_processes": [],
            "producer_jobs": [],
            "pending_legacy_task_retries": [],
        },
    }
    payload["payload_sha256"] = _canonical_sha(payload)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _maintenance_verifier(state: dict | None = None):
    state = state if state is not None else {"valid": True}

    def verify(path, receipt, cohort_id, process_generation):
        if not state.get("valid", True):
            raise RuntimeError("process state changed")
        return {
            "continuously_revalidated": True,
            "process_generation": process_generation,
            "stop_payload_sha256": receipt["payload_sha256"],
            "cohort_id": cohort_id,
            "receipt_path": str(path),
        }

    return verify


def _seed_native_store(tmp_path: Path, *, distinct_dates: bool = False):
    database = tmp_path / "native-task.db"
    store = TaskStore(database)
    store.initialize()
    with store.transaction() as connection:
        connection.execute(
            "UPDATE task_system_state SET authority_epoch='native:4', "
            "cowork_task_store_id='task-cowork-store', process_generation=7, "
            "cutover_receipt_id='cutover-native', rollback_fence=0, "
            "updated_at=? WHERE id=1",
            (NOW,),
        )
        connection.execute(
            "UPDATE task_collection_state SET revision=31, updated_at=? WHERE id=1",
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO task_migration_cohorts (
                cohort_id, schema_version, state, inventory_sha256,
                manifest_sha256, source_file_count, source_tree_bytes,
                source_root_fingerprint, source_db_sha256,
                source_db_integrity, source_db_schema_version,
                previous_authority_epoch, target_authority_epoch,
                cowork_task_store_id, actor, retention_policy, counts_json,
                approved_exceptions_json, backup_receipts_json,
                created_at, updated_at, activated_at
            ) VALUES (
                'cohort-native', 1, 'active', 'inventory', 'manifest', 4, 100,
                'root', 'dbsha', 'ok', 11, 'legacy', 'native:4',
                'task-cowork-store', 'operator:test',
                'until_explicit_user_approval', '{}', '[]', '[]', ?, ?, ?
            )
            """,
            (NOW, NOW, NOW),
        )
        tasks = (
            (
                "t-aa11",
                "active",
                "high",
                "Ship native tasks",
                NOTE_LIVE,
                "2026-09-01",
                "2026-09-09" if distinct_dates else None,
                None,
                None,
                None,
            ),
            (
                "t-bb22",
                "done",
                "medium",
                "Archive completed task",
                None,
                "2026-10-02",
                "2026-10-02",
                "2026-10-03T14:30:00+00:00",
                "2026-10-04T00:00:00+00:00",
                None,
            ),
            (
                "t-cc33",
                "inbox",
                "low",
                None,
                NOTE_DELETED,
                None,
                "2026-11-03",
                None,
                None,
                "2026-08-25T00:00:00+00:00",
            ),
        )
        connection.executemany(
            """
            INSERT INTO task_metadata (
                task_id, state, urgency, description, note_uuid,
                due_date, deadline_date, has_deadline,
                completed_at, archived_at, deleted_at,
                created_at, updated_at, revision, summary_text,
                dependencies_json, restored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    task_id,
                    state,
                    urgency,
                    description,
                    note_uuid,
                    due,
                    deadline,
                    1 if deadline else 0,
                    completed,
                    archived,
                    deleted,
                    "2026-08-01T00:00:00+00:00",
                    NOW,
                    index + 2,
                    "Native summary" if index == 0 else None,
                    '["t-bb22"]' if index == 0 else None,
                    "2026-08-20T00:00:00+00:00" if index == 0 else None,
                )
                for index, (
                    task_id,
                    state,
                    urgency,
                    description,
                    note_uuid,
                    due,
                    deadline,
                    completed,
                    archived,
                    deleted,
                ) in enumerate(tasks)
            ],
        )
        connection.executemany(
            "INSERT INTO task_tags (task_id, tag, is_namespace) VALUES (?, ?, ?)",
            (
                ("t-aa11", "projects/work-buddy", 0),
                ("t-aa11", "contexts/codex", 1),
                ("t-cc33", "retained/deleted", 1),
            ),
        )
        connection.executemany(
            """
            INSERT INTO task_state_history (
                id, task_id, old_state, new_state, changed_at, reason,
                mutation, actor, session_id, receipt_id,
                task_revision, collection_revision, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    11,
                    "t-aa11",
                    "inbox",
                    "active",
                    NOW,
                    "started",
                    "state_change",
                    "dashboard:user",
                    "session-a",
                    "tmr_a",
                    2,
                    29,
                    '{"source":"dashboard"}',
                ),
                (
                    12,
                    "t-bb22",
                    "active",
                    "done",
                    NOW,
                    "finished",
                    "complete",
                    "dashboard:user",
                    "session-a",
                    "tmr_b",
                    3,
                    30,
                    "{}",
                ),
                (
                    13,
                    "t-cc33",
                    "inbox",
                    "inbox",
                    NOW,
                    "deleted",
                    "delete",
                    "dashboard:user",
                    "session-a",
                    "tmr_c",
                    4,
                    31,
                    "{}",
                ),
            ),
        )
        connection.execute(
            "INSERT INTO task_sessions (id, task_id, session_id, assigned_at) "
            "VALUES (21, 't-aa11', 'session-a', ?)",
            (NOW,),
        )
        connection.execute(
            """
            INSERT INTO task_action_items (
                id, task_id, sequence, description, state, authorship,
                created_at, updated_at
            ) VALUES (41, 't-aa11', 1, 'Run rollback rehearsal', 'pending',
                      'user', ?, ?)
            """,
            (NOW, NOW),
        )
        connection.execute(
            "UPDATE task_metadata SET current_action_item_id=41 WHERE task_id='t-aa11'"
        )
        connection.executemany(
            """
            INSERT INTO task_document_links (
                task_id, note_uuid, store_id, document_id, binding_id,
                lifecycle, created_at, updated_at, retired_at
            ) VALUES (?, ?, 'task-cowork-store', ?, ?, ?, ?, ?, ?)
            """,
            (
                ("t-aa11", NOTE_LIVE, "doc-live", "binding-live", "current", NOW, NOW, None),
                (
                    "t-cc33",
                    NOTE_DELETED,
                    "doc-deleted",
                    "binding-deleted",
                    "retired",
                    NOW,
                    NOW,
                    NOW,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO recovered_task_documents (
                recovery_id, note_uuid, store_id, document_id,
                source_receipt_id, classification, lifecycle, imported_at
            ) VALUES ('recovery-1', ?, 'task-cowork-store', 'doc-recovered',
                      'source-1', 'recovered_task_document', 'current', ?)
            """,
            (NOTE_RECOVERED, NOW),
        )
    arm_native_authority_latch(
        database,
        cohort_id="cohort-native",
        target_authority_epoch="native:4",
        cutover_receipt_id="cutover-native",
        armed_at=NOW,
    )
    documents = {
        "doc-live": "# Native details\n\nCurrent head.\n",
        "doc-deleted": "# Deleted task evidence\n\nPreserved.\n",
        "doc-recovered": "# Recovered note\n\nStill unattached.\n",
    }
    return database, store, documents


def _operator(tmp_path: Path, **kwargs):
    database, store, documents = _seed_native_store(
        tmp_path, distinct_dates=kwargs.pop("distinct_dates", False)
    )
    stop_receipt = _write_stop_receipt(tmp_path / "process-stop.json")
    operator = ReverseLegacyTaskExportOperator(
        source_db_path=database,
        staging_root=tmp_path / "rollback-stage",
        document_reader=lambda row: documents[str(row["document_id"])],
        document_head_reader=lambda row: hashlib.sha256(
            documents[str(row["document_id"])].encode("utf-8")
        ).hexdigest(),
        maintenance_verifier=kwargs.pop(
            "maintenance_verifier", _maintenance_verifier()
        ),
        clock=lambda: NOW,
        **kwargs,
    )
    operator._test_stop_receipt = stop_receipt
    return operator, database, store, documents


def _prepare(operator: ReverseLegacyTaskExportOperator, **kwargs):
    return operator.prepare(
        cohort_id="cohort-native",
        rollback_authority_epoch="rollback:5",
        maintenance_receipt=operator._test_stop_receipt,
        expected_process_generation=7,
        confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        **kwargs,
    )


def test_prepare_exports_tree_v11_database_and_native_supplement(tmp_path):
    operator, _database, _store, documents = _operator(tmp_path)
    receipt = _prepare(operator)

    assert receipt == operator.verify_staging()
    evidence = operator.rehearsal_evidence()
    assert evidence["cohort_id"] == "cohort-native"
    assert evidence["receipt_id"] == receipt["receipt_id"]
    assert evidence["source_snapshot_sha256"] == receipt["source_snapshot_sha256"]
    assert evidence["source_authority_latch_sha256"] == receipt[
        "source_authority_latch_sha256"
    ]
    assert receipt["legacy_database_schema_version"] == 11
    assert receipt["counts"] == {
        "task_rows": 3,
        "live_task_rows": 2,
        "tombstone_rows": 1,
        "archived_rows": 1,
        "tag_rows": 3,
        "history_rows": 3,
        "session_rows": 1,
        "action_item_rows": 1,
        "lww_rows": 0,
        "master_lines": 1,
        "archive_lines": 1,
        "note_files": 3,
        "recovered_note_files": 1,
        "tree_files": 5,
        "local_assets": 0,
    }
    tree = operator.staging_root / "legacy-tree"
    master = (tree / "master-task-list.md").read_text(encoding="utf-8")
    archive = (tree / "archive.md").read_text(encoding="utf-8")
    assert "- [ ] #todo Ship native tasks" in master
    assert f"[[{NOTE_LIVE}|📓]]" in master
    assert "#projects/work-buddy" in master
    assert "📅 2026-09-01" in master
    assert "⏫" in master
    assert "- [x] #todo Archive completed task" in archive
    assert "✅ 2026-10-03" in archive
    assert "t-cc33" not in master + archive
    assert (tree / "notes" / f"{NOTE_DELETED}.md").read_text(encoding="utf-8") == documents[
        "doc-deleted"
    ]

    legacy_db = operator.staging_root / "task_metadata.v11.db"
    connection = sqlite3.connect(legacy_db)
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(task_metadata)")
        }
        assert "due_date" not in columns
        assert "revision" not in columns
        rows = {
            row["task_id"]: dict(row)
            for row in connection.execute("SELECT * FROM task_metadata")
        }
        assert rows["t-aa11"]["deadline_date"] == "2026-09-01"
        assert rows["t-bb22"]["deadline_date"] == "2026-10-02"
        assert rows["t-cc33"]["deadline_date"] == "2026-11-03"
        assert rows["t-cc33"]["deleted_at"] is not None
        assert rows["t-cc33"]["description"] is None
        assert connection.execute("SELECT COUNT(*) FROM task_tags").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM task_state_history").fetchone()[0] == 3
        assert [
            row[0]
            for row in connection.execute(
                "SELECT id FROM task_state_history ORDER BY id"
            ).fetchall()
        ] == [11, 12, 13]
    finally:
        connection.close()

    supplement = json.loads(
        (operator.staging_root / "native-supplement.json").read_text(encoding="utf-8")
    )
    assert supplement["history_enrichment"][0]["receipt_id"] == "tmr_a"
    assert supplement["native_task_fields"][0]["summary_text"] == "Native summary"

    # An old build can open a copy without seeing a future-schema database.
    compatibility_db = tmp_path / "legacy-open-rehearsal.db"
    shutil.copy2(legacy_db, compatibility_db)
    old = sqlite3.connect(compatibility_db)
    try:
        LEGACY_MIGRATIONS.run(old)
        assert old.execute("PRAGMA user_version").fetchone()[0] == 11
    finally:
        old.close()


def test_prepare_preserves_deleted_tombstone_without_document_link(tmp_path):
    operator, _database, store, _documents = _operator(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            "DELETE FROM task_document_links WHERE task_id = 't-cc33'"
        )

    _prepare(operator)

    legacy_db = operator.staging_root / "task_metadata.v11.db"
    connection = sqlite3.connect(legacy_db)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT description, note_uuid, deleted_at "
            "FROM task_metadata WHERE task_id = 't-cc33'"
        ).fetchone()
        assert dict(row) == {
            "description": None,
            "note_uuid": NOTE_DELETED,
            "deleted_at": "2026-08-25T00:00:00+00:00",
        }
    finally:
        connection.close()
    assert not (
        operator.staging_root / "legacy-tree" / "notes" / f"{NOTE_DELETED}.md"
    ).exists()


def test_distinct_due_deadline_blocks_until_explicit_per_task_resolution(tmp_path):
    operator, _database, _store, _documents = _operator(
        tmp_path, distinct_dates=True
    )
    with pytest.raises(RollbackExportBlocked) as raised:
        _prepare(operator)
    assert raised.value.details["date_conflicts"] == [
        {
            "task_id": "t-aa11",
            "due_date": "2026-09-01",
            "deadline_date": "2026-09-09",
        }
    ]
    assert not operator.staging_root.exists()

    receipt = _prepare(
        operator,
        date_resolutions={
            "t-aa11": DateConflictResolution(
                use="deadline_date", reason="Honor the contractual consequence date."
            )
        },
    )
    connection = sqlite3.connect(operator.staging_root / "task_metadata.v11.db")
    try:
        assert connection.execute(
            "SELECT deadline_date FROM task_metadata WHERE task_id='t-aa11'"
        ).fetchone()[0] == "2026-09-09"
    finally:
        connection.close()
    report = json.loads(
        (operator.staging_root / "rollback-exceptions.json").read_text(encoding="utf-8")
    )
    resolved = [
        item
        for item in report["semantic_downgrades"]
        if item["kind"] == "distinct_due_deadline_resolved"
    ]
    assert resolved[0]["selected_field"] == "deadline_date"
    assert receipt["semantic_downgrade_count"] == len(report["semantic_downgrades"])


def test_prepare_is_guarded_and_status_is_read_only(tmp_path):
    operator, _database, _store, _documents = _operator(tmp_path)
    status = operator.status(cohort_id="cohort-native")
    assert status["source"]["authority_epoch"] == "native:4"
    assert status["staging"] == {"state": "absent"}
    with pytest.raises(RollbackExportBlocked, match="confirmation phrase"):
        operator.prepare(
            cohort_id="cohort-native",
            rollback_authority_epoch="rollback:5",
            maintenance_receipt=operator._test_stop_receipt,
            expected_process_generation=7,
            confirmation="yes",
        )
    with pytest.raises(RollbackExportBlocked, match="process generation"):
        operator.prepare(
            cohort_id="cohort-native",
            rollback_authority_epoch="rollback:5",
            maintenance_receipt=operator._test_stop_receipt,
            expected_process_generation=8,
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )
    assert not operator.staging_root.exists()


def test_prepare_rejects_arbitrary_tampered_stale_and_mismatched_stop_receipts(
    tmp_path,
):
    operator, _database, _store, _documents = _operator(tmp_path)
    with pytest.raises(RollbackExportBlocked, match="receipt file"):
        operator.prepare(
            cohort_id="cohort-native",
            rollback_authority_epoch="rollback:5",
            maintenance_receipt="anything-nonempty",
            expected_process_generation=7,
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )

    payload = json.loads(operator._test_stop_receipt.read_text(encoding="utf-8"))
    payload["evidence"]["tracked_processes"] = [{"pid": 123}]
    operator._test_stop_receipt.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RollbackExportBlocked, match="modified"):
        _prepare(operator)

    mismatch_root = tmp_path / "mismatch"
    mismatch_root.mkdir()
    mismatch, *_ = _operator(mismatch_root)
    mismatch._test_stop_receipt = _write_stop_receipt(
        mismatch_root / "other-stop.json", cohort_id="another-cohort"
    )
    with pytest.raises(RollbackExportBlocked, match="another cohort"):
        _prepare(mismatch)

    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    state = {"valid": False}
    stale, *_ = _operator(
        stale_root, maintenance_verifier=_maintenance_verifier(state)
    )
    with pytest.raises(RollbackExportBlocked, match="revalidation failed"):
        _prepare(stale)


def test_maintenance_revalidation_ignores_only_invocation_specific_probe_details(
    tmp_path,
):
    calls = {"count": 0}

    def verifier(path, receipt, cohort_id, process_generation):
        calls["count"] += 1
        return {
            "continuously_revalidated": True,
            "process_generation": process_generation,
            "stop_payload_sha256": receipt["payload_sha256"],
            "cohort_id": cohort_id,
            "receipt_path": str(path),
            "operator_ancestor_chain": [{"pid": calls["count"]}],
        }

    operator, _database, _store, _documents = _operator(
        tmp_path,
        maintenance_verifier=verifier,
    )
    receipt = _prepare(operator)
    assert operator._revalidate_maintenance(receipt)["continuously_revalidated"]
    assert calls["count"] >= 2


def test_prepare_and_registration_require_the_exact_native_authority_latch(tmp_path):
    operator, _database, _store, _documents = _operator(tmp_path)
    latch = activation_authority_latch_path(operator.source_db_path)
    original = latch.read_bytes()
    latch.unlink()
    with pytest.raises(RollbackExportBlocked, match="authority latch"):
        _prepare(operator)

    latch.write_bytes(original)
    receipt = _prepare(operator)
    payload = json.loads(latch.read_text(encoding="utf-8"))
    payload["cutover_receipt_id"] = "another-cutover"
    latch.write_text(json.dumps(payload), encoding="utf-8")

    class FakeLedger:
        def __init__(self, store):
            self.store = store

        def prepare_rollback(self, cohort_id, **kwargs):
            return {"cohort_id": cohort_id, "state": "rollback_prepared"}

    with pytest.raises(RollbackExportBlocked, match="authority latch"):
        operator.register_prepared_rollback(
            ledger=FakeLedger(_store),
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )
    assert receipt["source_authority_latch"]["file_sha256"] == hashlib.sha256(
        original
    ).hexdigest()


def test_verify_staging_detects_tree_tampering(tmp_path):
    operator, _database, _store, _documents = _operator(tmp_path)
    _prepare(operator)
    note = operator.staging_root / "legacy-tree" / "notes" / f"{NOTE_LIVE}.md"
    note.write_text(note.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    with pytest.raises(RollbackExportVerificationError, match="tree changed"):
        operator.verify_staging()


def test_register_prepared_rollback_reverifies_source_and_calls_ledger(tmp_path):
    operator, database, store, _documents = _operator(tmp_path)
    receipt = _prepare(operator)

    class FakeLedger:
        def __init__(self):
            self.store = store
            self.calls = []

        def prepare_rollback(self, cohort_id, **kwargs):
            self.calls.append((cohort_id, kwargs))
            return {"cohort_id": cohort_id, "state": "rollback_prepared"}

    ledger = FakeLedger()
    result = operator.register_prepared_rollback(
        ledger=ledger,
        actor="operator:test",
        session_id="session-test",
        confirmation=ROLLBACK_EXPORT_CONFIRMATION,
    )
    assert result["cohort"]["state"] == "rollback_prepared"
    assert ledger.calls[0][1]["reverse_export_receipt"] == receipt

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE task_collection_state SET revision=revision+1 WHERE id=1"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RollbackExportBlocked, match="no longer matches"):
        operator.register_prepared_rollback(
            ledger=ledger,
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )


def test_register_blocks_note_edit_and_unrevisioned_database_change(tmp_path):
    operator, database, store, documents = _operator(tmp_path)
    _prepare(operator)

    class FakeLedger:
        def __init__(self):
            self.store = store

        def prepare_rollback(self, cohort_id, **kwargs):
            return {"cohort_id": cohort_id, "state": "rollback_prepared"}

    documents["doc-live"] += "\nEdited without a task revision.\n"
    with pytest.raises(RollbackExportBlocked, match="Co-work heads"):
        operator.register_prepared_rollback(
            ledger=FakeLedger(),
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )

    documents["doc-live"] = "# Native details\n\nCurrent head.\n"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO task_sync_status "
            "(id, last_sync_created, last_sync_updated, last_sync_deleted, updated_at) "
            "VALUES (1, 1, 0, 0, ?) ON CONFLICT(id) DO UPDATE SET "
            "last_sync_created=task_sync_status.last_sync_created+1",
            (NOW,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RollbackExportBlocked, match="native task snapshot"):
        operator.register_prepared_rollback(
            ledger=FakeLedger(),
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )


def test_local_files_require_hash_verified_frozen_root_and_are_rehydrated(tmp_path):
    database, store, documents = _seed_native_store(tmp_path)
    payload = b"rollback attachment bytes\n"
    reveal_payload = b"private key evidence\n"
    relative = "notes/assets/spec.pdf"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_local_file_roots (
                root_id, label, manifest_sha256, policy_revision,
                status, created_at, updated_at
            ) VALUES ('frozen-root', 'Frozen tasks', 'manifest', 1,
                      'sealed', ?, ?)
            """,
            (NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO task_local_file_links (
                link_id, task_id, store_id, document_id, root_id,
                relative_path, display_name, suffix, media_type,
                byte_length, sha256, sensitivity, allowed_action,
                policy_revision, source_receipt_id, created_at
            ) VALUES (
                'lf_asset', 't-aa11', 'task-cowork-store', 'doc-live',
                'frozen-root', ?, 'spec.pdf', '.pdf', 'application/pdf',
                ?, ?, 'ordinary', 'open', 1, 'source-asset', ?
            )
            """,
            (relative, len(payload), hashlib.sha256(payload).hexdigest(), NOW),
        )
        connection.execute(
            """
            INSERT INTO task_local_file_links (
                link_id, task_id, store_id, document_id, root_id,
                relative_path, display_name, suffix, media_type,
                byte_length, sha256, sensitivity, allowed_action,
                policy_revision, source_receipt_id, created_at
            ) VALUES (
                'lf_key', 't-aa11', 'task-cowork-store', 'doc-live',
                'frozen-root', 'notes/assets/key.ppk', 'key.ppk', '.ppk',
                'application/octet-stream', ?, ?, 'credential_adjacent',
                'reveal', 1, 'source-key', ?
            )
            """,
            (
                len(reveal_payload),
                hashlib.sha256(reveal_payload).hexdigest(),
                NOW,
            ),
        )
    documents["doc-live"] += "\nLocal file (Spec): wb-local-file:lf\\_asset\n"
    documents["doc-live"] += "\nKey location: wb-local-file:lf\\_key\n"
    without_root = ReverseLegacyTaskExportOperator(
        source_db_path=database,
        staging_root=tmp_path / "rollback-stage",
        document_reader=lambda row: documents[str(row["document_id"])],
        document_head_reader=lambda row: hashlib.sha256(
            documents[str(row["document_id"])].encode("utf-8")
        ).hexdigest(),
        maintenance_verifier=_maintenance_verifier(),
        clock=lambda: NOW,
    )
    without_root._test_stop_receipt = _write_stop_receipt(
        tmp_path / "process-stop.json"
    )
    with pytest.raises(RollbackExportBlocked, match="frozen asset root"):
        _prepare(without_root)
    assert not without_root.staging_root.exists()

    frozen = tmp_path / "frozen"
    asset = frozen / "notes" / "assets" / "spec.pdf"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(payload)
    (frozen / "notes" / "assets" / "key.ppk").write_bytes(reveal_payload)
    operator = ReverseLegacyTaskExportOperator(
        source_db_path=database,
        staging_root=tmp_path / "rollback-stage",
        document_reader=lambda row: documents[str(row["document_id"])],
        document_head_reader=lambda row: hashlib.sha256(
            documents[str(row["document_id"])].encode("utf-8")
        ).hexdigest(),
        local_asset_root=frozen,
        maintenance_verifier=_maintenance_verifier(),
        clock=lambda: NOW,
    )
    operator._test_stop_receipt = tmp_path / "process-stop.json"
    receipt = _prepare(operator)
    staged = operator.staging_root / "legacy-tree"
    assert (staged / relative).read_bytes() == payload
    note = (staged / "notes" / f"{NOTE_LIVE}.md").read_text(encoding="utf-8")
    assert "[Spec](assets/spec.pdf)" in note
    assert "Key location: assets/key.ppk" in note
    assert "](assets/key.ppk)" not in note
    assert "wb-local-file:" not in note
    assert receipt["counts"]["local_assets"] == 2


def test_frozen_asset_reparse_points_are_rejected(tmp_path, monkeypatch):
    operator, _database, _store, _documents = _operator(tmp_path)
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    asset = frozen / "asset.pdf"
    asset.write_bytes(b"bytes")
    operator.local_asset_root = frozen.resolve()
    monkeypatch.setattr(
        rollback_export_module,
        "_is_link_like",
        lambda path: Path(path).resolve() == frozen.resolve(),
    )
    with pytest.raises(RollbackExportBlocked, match="frozen asset root"):
        operator._verified_asset(
            {
                "link_id": "lf_test",
                "relative_path": "asset.pdf",
                "byte_length": 5,
                "sha256": hashlib.sha256(b"bytes").hexdigest(),
            }
        )


def test_local_asset_cannot_collide_with_generated_note_path(tmp_path):
    database, store, documents = _seed_native_store(tmp_path)
    payload = b"collision"
    relative = f"notes/{NOTE_LIVE}.md"
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO task_local_file_roots "
            "(root_id, label, manifest_sha256, policy_revision, status, created_at, updated_at) "
            "VALUES ('frozen-root', 'Frozen', 'manifest', 1, 'sealed', ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO task_local_file_links "
            "(link_id, task_id, store_id, document_id, root_id, relative_path, "
            "display_name, suffix, media_type, byte_length, sha256, sensitivity, "
            "allowed_action, policy_revision, source_receipt_id, created_at) "
            "VALUES ('lf_collision', 't-aa11', 'task-cowork-store', 'doc-live', "
            "'frozen-root', ?, 'collision.md', '.md', 'text/markdown', ?, ?, "
            "'ordinary', 'open', 1, 'source', ?)",
            (relative, len(payload), hashlib.sha256(payload).hexdigest(), NOW),
        )
    documents["doc-live"] += "\nwb-local-file:lf_collision\n"
    frozen = tmp_path / "frozen"
    collision = frozen / "notes" / f"{NOTE_LIVE}.md"
    collision.parent.mkdir(parents=True)
    collision.write_bytes(payload)
    operator = ReverseLegacyTaskExportOperator(
        source_db_path=database,
        staging_root=tmp_path / "rollback-stage",
        document_reader=lambda row: documents[str(row["document_id"])],
        document_head_reader=lambda row: hashlib.sha256(
            documents[str(row["document_id"])].encode("utf-8")
        ).hexdigest(),
        local_asset_root=frozen,
        maintenance_verifier=_maintenance_verifier(),
        clock=lambda: NOW,
    )
    operator._test_stop_receipt = _write_stop_receipt(tmp_path / "stop.json")
    with pytest.raises(RollbackExportBlocked, match="collides"):
        _prepare(operator)


@dataclass(frozen=True)
class _FakeBinding:
    binding_id: str
    store_id: str
    document_id: str
    lifecycle: str
    content_authority: str
    content_authority_epoch: int
    domain_revision: str = "native-revision"
    domain_namespace: str = "tasks"
    domain_kind: str = "task_knowledge"
    role: str = "task_knowledge"


class _FakeCausality:
    def __init__(self):
        self.bindings = {
            "binding-live": _FakeBinding(
                "binding-live",
                "task-cowork-store",
                "doc-live",
                "current",
                "co_work",
                4,
            ),
            "binding-deleted": _FakeBinding(
                "binding-deleted",
                "task-cowork-store",
                "doc-deleted",
                "retired",
                "co_work",
                4,
            ),
        }

    def list_all_bindings(self):
        return tuple(self.bindings.values())

    def list_bindings(self):
        return tuple(
            value for value in self.bindings.values() if value.lifecycle == "current"
        )

    def get_binding(self, binding_id):
        return self.bindings.get(binding_id)

    def rollback_to_domain(self, binding_id, *, domain_revision, expected_epoch):
        current = self.bindings[binding_id]
        if current.content_authority == "domain":
            return current
        assert current.content_authority_epoch == expected_epoch
        updated = replace(
            current,
            content_authority="domain",
            content_authority_epoch=expected_epoch + 1,
            domain_revision=domain_revision,
        )
        self.bindings[binding_id] = updated
        return updated


def _registered_operator(tmp_path: Path, **kwargs):
    operator, database, store, documents = _operator(tmp_path, **kwargs)
    receipt = _prepare(operator)
    ledger = TaskMigrationLedger(store, clock=lambda: NOW)
    operator.register_prepared_rollback(
        ledger=ledger,
        actor="operator:test",
        session_id="session-test",
        confirmation=ROLLBACK_EXPORT_CONFIRMATION,
    )
    return operator, database, store, documents, ledger, receipt


def test_completion_installs_targets_resumes_after_crash_and_commits_epoch_last(
    tmp_path,
):
    operator, database, _store, _documents, ledger, receipt = _registered_operator(
        tmp_path
    )
    causality = _FakeCausality()
    tree_target = tmp_path / "installed" / "tasks"
    tree_target.mkdir(parents=True)
    (tree_target / "old.md").write_text("old", encoding="utf-8")
    database_target = tmp_path / "installed" / "legacy.db"
    database_target.write_bytes(b"old database")
    tripped = {"value": False}

    def crash(name):
        if name == "database_backup_moved" and not tripped["value"]:
            tripped["value"] = True
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=causality,
            legacy_tree_target=tree_target,
            legacy_database_target=database_target,
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
            failpoint=crash,
        )
    assert not database_target.exists()
    assert list(database_target.parent.glob(".legacy.db.pre-rollback.*"))

    completed = operator.complete_prepared_rollback(
        ledger=ledger,
        causality=causality,
        legacy_tree_target=tree_target,
        legacy_database_target=database_target,
        actor="operator:test",
        session_id="session-test",
        confirmation=ROLLBACK_EXPORT_CONFIRMATION,
    )
    assert completed["authority_committed"]["authority_epoch"] == "rollback:5"
    assert completed["authority_latch_cleared"]["cleared"] is True
    assert not activation_authority_latch_path(database).exists()
    assert operator._tree_manifest(tree_target)["tree_sha256"] == receipt[
        "staged_tree_sha256"
    ]
    assert hashlib.sha256(database_target.read_bytes()).hexdigest() == receipt[
        "legacy_database_sha256"
    ]
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        system = connection.execute(
            "SELECT * FROM task_system_state WHERE id=1"
        ).fetchone()
        cohort = connection.execute(
            "SELECT * FROM task_migration_cohorts WHERE cohort_id='cohort-native'"
        ).fetchone()
        assert system["authority_epoch"] == "rollback:5"
        assert system["process_generation"] == 8
        assert system["rollback_fence"] == 0
        assert cohort["state"] == "rolled_back"
        transitions = connection.execute(
            "SELECT direction, before_authority, before_epoch, after_authority, "
            "after_epoch, domain_revision, result "
            "FROM task_migration_binding_transitions "
            "WHERE cohort_id='cohort-native' AND direction='rollback_to_domain'"
        ).fetchall()
        assert [tuple(row) for row in transitions] == [
            (
                "rollback_to_domain",
                "co_work",
                4,
                "domain",
                5,
                "rollback:5:" + receipt["staged_tree_sha256"],
                "applied",
            )
        ]
    finally:
        connection.close()
    assert causality.get_binding("binding-live").content_authority == "domain"
    assert causality.get_binding("binding-live").content_authority_epoch == 5

    assert operator.complete_prepared_rollback(
        ledger=ledger,
        causality=causality,
        legacy_tree_target=tree_target,
        legacy_database_target=database_target,
        actor="operator:test",
        session_id="session-test",
        confirmation=ROLLBACK_EXPORT_CONFIRMATION,
    )["authority_committed"] == completed["authority_committed"]


def test_completion_recovers_crash_after_control_commit_and_detects_tamper(tmp_path):
    operator, _database, _store, _documents, ledger, _receipt = _registered_operator(
        tmp_path
    )
    causality = _FakeCausality()
    tree_target = tmp_path / "installed" / "tasks"
    database_target = tmp_path / "installed" / "legacy.db"

    def crash(name):
        if name == "authority_db_committed":
            raise RuntimeError("lost response after commit")

    with pytest.raises(RuntimeError, match="lost response"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=causality,
            legacy_tree_target=tree_target,
            legacy_database_target=database_target,
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
            failpoint=crash,
        )
    recovered = operator.complete_prepared_rollback(
        ledger=ledger,
        causality=causality,
        legacy_tree_target=tree_target,
        legacy_database_target=database_target,
        actor="operator:test",
        session_id="session-test",
        confirmation=ROLLBACK_EXPORT_CONFIRMATION,
    )
    assert recovered["authority_committed"]["authority_epoch"] == "rollback:5"

    (tree_target / "master-task-list.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(RollbackExportVerificationError, match="tree changed"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=causality,
            legacy_tree_target=tree_target,
            legacy_database_target=database_target,
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )


def test_postcommit_resume_does_not_require_precommit_maintenance_state(tmp_path):
    maintenance = {"valid": True}
    operator, _database, _store, _documents, ledger, _receipt = _registered_operator(
        tmp_path,
        maintenance_verifier=_maintenance_verifier(maintenance),
    )
    causality = _FakeCausality()
    tree_target = tmp_path / "installed" / "tasks"
    database_target = tmp_path / "installed" / "legacy.db"

    def crash(name):
        if name == "authority_db_committed":
            maintenance["valid"] = False
            raise RuntimeError("lost response after authority commit")

    with pytest.raises(RuntimeError, match="lost response after authority commit"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=causality,
            legacy_tree_target=tree_target,
            legacy_database_target=database_target,
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
            failpoint=crash,
        )

    # A rolled-back authority epoch no longer satisfies the pre-commit stop
    # verifier. Recovery must discover the committed CAS from the native
    # ledger/journal first and finish the latch/journal idempotently.
    completed = operator.complete_prepared_rollback(
        ledger=ledger,
        causality=causality,
        legacy_tree_target=tree_target,
        legacy_database_target=database_target,
        actor="operator:test",
        session_id="session-test",
        confirmation=ROLLBACK_EXPORT_CONFIRMATION,
    )
    assert maintenance["valid"] is False
    assert completed["authority_committed"]["authority_epoch"] == "rollback:5"
    assert completed["authority_latch_cleared"]["cleared"] is True


def test_completion_rechecks_targets_immediately_before_authority_commit(tmp_path):
    operator, database, _store, _documents, ledger, _receipt = _registered_operator(
        tmp_path
    )
    causality = _FakeCausality()
    tree_target = tmp_path / "installed" / "tasks"
    database_target = tmp_path / "installed" / "legacy.db"

    def tamper(name):
        if name == "before_final_target_verification":
            (tree_target / "master-task-list.md").write_text(
                "tampered before CAS", encoding="utf-8"
            )

    with pytest.raises(RollbackExportVerificationError, match="tree changed"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=causality,
            legacy_tree_target=tree_target,
            legacy_database_target=database_target,
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
            failpoint=tamper,
        )
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT authority_epoch FROM task_system_state WHERE id=1"
        ).fetchone()[0] == "native:4"
    finally:
        connection.close()


def test_completion_rejects_install_target_overlap_before_any_mutation(tmp_path):
    operator, database, _store, _documents, ledger, _receipt = _registered_operator(
        tmp_path / "source"
    )
    operator.staging_root.rename(tmp_path / "rollback-stage")
    operator.staging_root = (tmp_path / "rollback-stage").resolve()
    causality = _FakeCausality()

    nested_tree = tmp_path / "installed" / "tasks"
    with pytest.raises(RollbackExportBlocked, match="inside the legacy tree"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=causality,
            legacy_tree_target=nested_tree,
            legacy_database_target=nested_tree / "legacy.db",
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )
    with pytest.raises(RollbackExportBlocked, match="native control database"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=causality,
            legacy_tree_target=database.parent,
            legacy_database_target=tmp_path / "installed" / "legacy.db",
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )
    assert causality.get_binding("binding-live").content_authority == "co_work"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT authority_epoch FROM task_system_state WHERE id=1"
        ).fetchone()[0] == "native:4"
    finally:
        connection.close()


def test_completed_rollback_detects_binding_transition_receipt_tampering(tmp_path):
    operator, database, _store, _documents, ledger, _receipt = _registered_operator(
        tmp_path
    )
    causality = _FakeCausality()
    tree_target = tmp_path / "installed" / "tasks"
    database_target = tmp_path / "installed" / "legacy.db"
    operator.complete_prepared_rollback(
        ledger=ledger,
        causality=causality,
        legacy_tree_target=tree_target,
        legacy_database_target=database_target,
        actor="operator:test",
        session_id="session-test",
        confirmation=ROLLBACK_EXPORT_CONFIRMATION,
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE task_migration_binding_transitions SET result='tampered' "
            "WHERE cohort_id='cohort-native' AND direction='rollback_to_domain'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RollbackExportBlocked, match="transition receipt was modified"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=causality,
            legacy_tree_target=tree_target,
            legacy_database_target=database_target,
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )


def test_completion_refuses_to_install_over_frozen_evidence(tmp_path):
    operator, _database, _store, _documents, ledger, _receipt = _registered_operator(
        tmp_path
    )
    frozen = tmp_path / "immutable-frozen"
    frozen.mkdir()
    operator.local_asset_root = frozen.resolve()
    with pytest.raises(RollbackExportBlocked, match="immutable frozen evidence"):
        operator.complete_prepared_rollback(
            ledger=ledger,
            causality=_FakeCausality(),
            legacy_tree_target=frozen / "tasks",
            legacy_database_target=tmp_path / "installed" / "legacy.db",
            actor="operator:test",
            session_id="session-test",
            confirmation=ROLLBACK_EXPORT_CONFIRMATION,
        )
