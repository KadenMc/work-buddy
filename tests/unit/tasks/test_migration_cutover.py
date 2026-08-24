from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pytest

import work_buddy.tasks.import_legacy as legacy_import
from tests.unit.tasks.test_migration_inventory import (
    IDLESS_NOTE,
    LIVE_NOTE,
    _fixture,
    _manifest_csv,
)
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.sources import ActorRef, SourceStore
from work_buddy.tasks.documents import TaskDocumentService, TaskDocumentStoreManager
from work_buddy.tasks.import_legacy import (
    ACTIVATION_CONFIRMATION,
    LegacyTaskCutoverOperator,
    LegacyTaskDocumentImporter,
    main as import_legacy_main,
)
from work_buddy.tasks.migration import (
    CohortStateError,
    CutoverPreconditionError,
    LegacyInventoryError,
    LegacyManifestEntry,
    LegacyTaskInventoryBuilder,
    REQUIRED_ACTIVATION_GATES,
)
from work_buddy.tasks.store import TaskStore
from work_buddy.truth import documents
from work_buddy.truth.registry import TruthStoreRegistry


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
BACKUP_RECEIPTS = ({"snapshot_id": "fixture-backup", "verified": True},)


def _operator(tmp_path):
    source, db, manifest = _fixture(tmp_path)
    inventory = LegacyTaskInventoryBuilder(
        cohort_id="cohort-cutover-fixture",
        source_root=source,
        task_db_path=db,
        manifest=manifest,
    ).build().require_valid()
    task_store = TaskStore(db)
    sources = SourceStore.create(tmp_path / "sources")
    principal = ActorRef(
        sources.authority_id,
        "legacy-task-migration",
        "service",
        "task-migration-tenant",
    )
    stores = TaskDocumentStoreManager(
        root=tmp_path / "cowork-tasks",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    importer = LegacyTaskDocumentImporter(
        source_root=source,
        sources=sources,
        principal=principal,
        stores=stores,
    )
    operator = LegacyTaskCutoverOperator(
        inventory=inventory,
        source_root=source,
        task_store=task_store,
        document_importer=importer,
        actor="operator:test",
        session_id="session-test",
    )
    return operator, importer, task_store, sources, stores


def test_shadow_import_is_projection_free_idempotent_and_preserves_recovery(tmp_path):
    operator, importer, task_store, sources, stores = _operator(tmp_path)
    try:
        with pytest.raises(CutoverPreconditionError):
            operator.shadow_import(backup_receipts=())
        result = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        replay = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
    finally:
        importer.close()

    assert result["documents_imported"] == replay["documents_imported"] == 4
    assert result["recovered_documents"] == 1
    assert result["local_links_staged"] == 2
    assert not list((tmp_path / "cowork-tasks").rglob("*.md"))

    store = stores.open_existing()
    truth_conn = store.connect()
    try:
        assert truth_conn.execute(
            "SELECT COUNT(*) FROM document_provenance_attestations"
        ).fetchone()[0] == 4
    finally:
        truth_conn.close()
    causality = DocumentCausalityStore(store.paths.sidecar)
    live = causality.binding_for_domain(
        "tasks", "task_knowledge", "t-a1", "task_knowledge"
    )
    assert live is not None
    assert live.content_authority == "domain"
    assert live.projection_mode == "none"
    assert live.projection_path is None
    idless_task_id = next(
        line.imported_task_id for line in operator.inventory.task_lines if line.is_idless
    )
    idless = causality.binding_for_domain(
        "tasks", "task_knowledge", idless_task_id, "task_knowledge"
    )
    assert idless is not None and idless.content_authority == "domain"
    assert TaskDocumentService(stores=stores).get("t-a1") is not None

    conn = task_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM recovered_task_documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_document_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_migration_document_stage").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM task_migration_local_link_stage").fetchone()[0] == 2
        actions = {
            row[0]
            for row in conn.execute(
                "SELECT allowed_action FROM task_migration_local_link_stage"
            )
        }
        assert actions == {"open", "reveal"}
        staged = conn.execute(
            "SELECT document_id FROM task_migration_document_stage WHERE note_uuid=?",
            (IDLESS_NOTE,),
        ).fetchone()
        assert staged is not None
        document = documents.get_document(store, staged[0])
        body = store.resolve_blob_path(f"blobs/{document.content_sha256}").read_text(
            encoding="utf-8"
        )
        assert body.replace("\\_", "_").count("wb-local-file:lf_") == 2
        assert "private.ppk" not in body
        # Source exact bytes remain retained separately from the rewritten doc.
        assert conn.execute(
            "SELECT COUNT(*) FROM task_migration_inventory WHERE source_bytes IS NOT NULL"
        ).fetchone()[0] == 2
    finally:
        conn.close()
    source_conn = sources.connect()
    try:
        assert source_conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 4
    finally:
        source_conn.close()


def test_shadow_fast_resume_skips_completed_documents_after_crash(
    tmp_path,
    monkeypatch,
):
    operator, importer, task_store, sources, stores = _operator(tmp_path)
    original_import = importer.import_note
    completed_before_crash: list[str] = []
    replay_importer = None

    def crash_on_second(item, **kwargs):
        if completed_before_crash:
            raise RuntimeError("fixture crash after first staged document")
        result = original_import(item, **kwargs)
        completed_before_crash.append(str(item.note_uuid))
        return result

    try:
        monkeypatch.setattr(importer, "import_note", crash_on_second)
        with pytest.raises(RuntimeError, match="fixture crash"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        conn = task_store.connect()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM task_migration_document_stage"
            ).fetchone()[0] == 1
        finally:
            conn.close()

        importer.close()
        principal = ActorRef(
            sources.authority_id,
            "legacy-task-migration",
            "service",
            "task-migration-tenant",
        )
        replay_importer = LegacyTaskDocumentImporter(
            source_root=operator.source_root,
            sources=sources,
            principal=principal,
            stores=stores,
        )
        replay_operator = LegacyTaskCutoverOperator(
            inventory=operator.inventory,
            source_root=operator.source_root,
            task_store=task_store,
            document_importer=replay_importer,
            actor="operator:restart",
            session_id="session-restart",
        )
        replayed_imports: list[str] = []

        def count_import(item, **kwargs):
            replayed_imports.append(str(item.note_uuid))
            return replay_importer_import(item, **kwargs)

        replay_importer_import = replay_importer.import_note
        monkeypatch.setattr(replay_importer, "import_note", count_import)
        replay = replay_operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        assert completed_before_crash[0] not in replayed_imports
        assert len(replayed_imports) == 3
        assert replay["documents_imported"] == 4
        assert replay["recovered_documents"] == 1
        assert replay["local_links_staged"] == 2

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("a complete stage must not re-enter import_note")

        monkeypatch.setattr(replay_importer, "import_note", unexpected_import)
        assert replay_operator.shadow_import(backup_receipts=BACKUP_RECEIPTS) == replay
    finally:
        if replay_importer is not None:
            replay_importer.close()
        else:
            importer.close()


def test_shadow_fast_resume_repairs_a_crash_between_local_link_rows(
    tmp_path,
    monkeypatch,
):
    operator, importer, task_store, _sources, _stores = _operator(tmp_path)
    original_import = importer.import_note
    original_stage_link = operator.ledger.stage_local_file_link
    crashed = False

    def crash_after_first_link(*args, **kwargs):
        nonlocal crashed
        result = original_stage_link(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("fixture crash after first local link")
        return result

    try:
        monkeypatch.setattr(
            operator.ledger, "stage_local_file_link", crash_after_first_link
        )
        with pytest.raises(RuntimeError, match="first local link"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        conn = task_store.connect()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM task_migration_local_link_stage "
                "WHERE note_uuid=?",
                (IDLESS_NOTE,),
            ).fetchone()[0] == 1
        finally:
            conn.close()

        monkeypatch.setattr(
            operator.ledger, "stage_local_file_link", original_stage_link
        )
        replayed_imports: list[str] = []

        def count_import(item, **kwargs):
            replayed_imports.append(str(item.note_uuid))
            return original_import(item, **kwargs)

        monkeypatch.setattr(importer, "import_note", count_import)
        replay = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        assert IDLESS_NOTE not in replayed_imports
        assert replay["documents_imported"] == 4
        assert replay["local_links_staged"] == 2
        conn = task_store.connect()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM task_migration_local_link_stage "
                "WHERE note_uuid=?",
                (IDLESS_NOTE,),
            ).fetchone()[0] == 2
        finally:
            conn.close()
    finally:
        importer.close()


def test_shadow_fast_resume_fails_closed_on_stage_link_blob_and_receipt_drift(
    tmp_path,
    monkeypatch,
):
    operator, importer, task_store, _sources, stores = _operator(tmp_path)
    try:
        baseline = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("drift must fail rather than fall back to import_note")

        monkeypatch.setattr(importer, "import_note", unexpected_import)
        conn = task_store.connect()
        try:
            stage = conn.execute(
                "SELECT rewrite_manifest_json, source_receipt_id, document_id "
                "FROM task_migration_document_stage WHERE note_uuid=?",
                (IDLESS_NOTE,),
            ).fetchone()
            link = conn.execute(
                "SELECT link_id, display_name FROM task_migration_local_link_stage "
                "WHERE note_uuid=? ORDER BY link_id LIMIT 1",
                (IDLESS_NOTE,),
            ).fetchone()
            assert stage is not None and link is not None
            conn.execute(
                "UPDATE task_migration_document_stage SET rewrite_manifest_json='[]' "
                "WHERE note_uuid=?",
                (IDLESS_NOTE,),
            )
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        conn = task_store.connect()
        try:
            conn.execute(
                "UPDATE task_migration_document_stage SET rewrite_manifest_json=? "
                "WHERE note_uuid=?",
                (stage["rewrite_manifest_json"], IDLESS_NOTE),
            )
            conn.execute(
                "UPDATE task_migration_local_link_stage SET display_name='drifted.pdf' "
                "WHERE link_id=?",
                (link["link_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        conn = task_store.connect()
        try:
            conn.execute(
                "UPDATE task_migration_local_link_stage SET display_name=? "
                "WHERE link_id=?",
                (link["display_name"], link["link_id"]),
            )
            conn.execute(
                "UPDATE task_migration_document_stage SET source_receipt_id='missing-receipt' "
                "WHERE note_uuid=?",
                (IDLESS_NOTE,),
            )
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        conn = task_store.connect()
        try:
            conn.execute(
                "UPDATE task_migration_document_stage SET source_receipt_id=? "
                "WHERE note_uuid=?",
                (stage["source_receipt_id"], IDLESS_NOTE),
            )
            conn.commit()
        finally:
            conn.close()
        store = stores.open_existing()
        document = documents.get_document(store, str(stage["document_id"]))
        blob = store.resolve_blob_path(f"blobs/{document.content_sha256}")
        original_blob = blob.read_bytes()
        blob.write_bytes(original_blob + b"drift")
        try:
            with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
                operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        finally:
            blob.write_bytes(original_blob)

        assert operator.shadow_import(backup_receipts=BACKUP_RECEIPTS) == baseline
    finally:
        importer.close()


def test_shadow_fast_resume_derives_literal_count_from_document_metadata(
    tmp_path,
    monkeypatch,
):
    operator, importer, _task_store, _sources, _stores = _operator(tmp_path)
    try:
        monkeypatch.setattr(
            legacy_import,
            "_kernel_projection_equivalent",
            lambda _projection, _expected: False,
        )
        first = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        assert first["literal_fallback_documents"] == first["documents_imported"] == 4

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("literal replay must not re-enter import_note")

        monkeypatch.setattr(importer, "import_note", unexpected_import)
        replay = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        assert replay == first
    finally:
        importer.close()


def test_shadow_fast_resume_audits_retained_source_bytes_and_rejects_corruption(
    tmp_path,
    monkeypatch,
):
    operator, importer, _task_store, sources, _stores = _operator(tmp_path)
    original_resolve = legacy_import.resolve_source
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        conn = sources.connect()
        try:
            before = conn.execute("SELECT COUNT(*) FROM source_access_audit").fetchone()[0]
        finally:
            conn.close()

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("complete stages must not re-enter import_note")

        monkeypatch.setattr(importer, "import_note", unexpected_import)
        replay = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        assert replay["documents_imported"] == 4
        conn = sources.connect()
        try:
            after = conn.execute("SELECT COUNT(*) FROM source_access_audit").fetchone()[0]
        finally:
            conn.close()
        assert after - before == replay["documents_imported"]

        def corrupted_resolution(*args, **kwargs):
            resolved = original_resolve(*args, **kwargs)
            return replace(resolved, content=resolved.content + b"corrupt")

        monkeypatch.setattr(legacy_import, "resolve_source", corrupted_resolution)
        with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
    finally:
        importer.close()


def test_shadow_fast_resume_cas_backfills_null_receipt_without_upstream_import(
    tmp_path,
    monkeypatch,
):
    operator, importer, task_store, sources, stores = _operator(tmp_path)
    try:
        baseline = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        conn = task_store.connect()
        try:
            row = conn.execute(
                "SELECT source_receipt_id FROM task_migration_document_stage "
                "WHERE note_uuid=?",
                (LIVE_NOTE,),
            ).fetchone()
            assert row is not None
            expected_receipt = str(row[0])
            conn.execute(
                "UPDATE task_migration_document_stage SET source_receipt_id=NULL "
                "WHERE note_uuid=?",
                (LIVE_NOTE,),
            )
            conn.commit()
        finally:
            conn.close()
        source_conn = sources.connect()
        try:
            source_items_before = source_conn.execute(
                "SELECT COUNT(*) FROM source_items"
            ).fetchone()[0]
            usages_before = source_conn.execute(
                "SELECT COUNT(*) FROM source_usage_intents"
            ).fetchone()[0]
        finally:
            source_conn.close()
        store = stores.open_existing()
        truth_conn = store.connect()
        try:
            documents_before = truth_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            attestations_before = truth_conn.execute(
                "SELECT COUNT(*) FROM document_provenance_attestations"
            ).fetchone()[0]
        finally:
            truth_conn.close()

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("NULL receipt repair must not call import_note")

        monkeypatch.setattr(importer, "import_note", unexpected_import)
        assert operator.shadow_import(backup_receipts=BACKUP_RECEIPTS) == baseline
        conn = task_store.connect()
        try:
            assert conn.execute(
                "SELECT source_receipt_id FROM task_migration_document_stage "
                "WHERE note_uuid=?",
                (LIVE_NOTE,),
            ).fetchone()[0] == expected_receipt
        finally:
            conn.close()
        source_conn = sources.connect()
        try:
            assert source_conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == source_items_before
            assert source_conn.execute("SELECT COUNT(*) FROM source_usage_intents").fetchone()[0] == usages_before
        finally:
            source_conn.close()
        truth_conn = store.connect()
        try:
            assert truth_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == documents_before
            assert truth_conn.execute(
                "SELECT COUNT(*) FROM document_provenance_attestations"
            ).fetchone()[0] == attestations_before
        finally:
            truth_conn.close()
    finally:
        importer.close()


def test_shadow_fast_resume_rejects_document_meta_version_and_provenance_drift(
    tmp_path,
    monkeypatch,
):
    operator, importer, task_store, _sources, stores = _operator(tmp_path)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        conn = task_store.connect()
        try:
            document_id = str(
                conn.execute(
                    "SELECT document_id FROM task_migration_document_stage "
                    "WHERE note_uuid=?",
                    (LIVE_NOTE,),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        store = stores.open_existing()

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("Truth drift must not call import_note")

        monkeypatch.setattr(importer, "import_note", unexpected_import)
        original_get = documents.get_document
        with monkeypatch.context() as patch:
            def drifted_get(*args, **kwargs):
                record = original_get(*args, **kwargs)
                if record.id != document_id:
                    return record
                meta = json.loads(record.meta_json or "{}")
                meta["migration_read_only"] = False
                return replace(record, meta_json=json.dumps(meta))

            patch.setattr(documents, "get_document", drifted_get)
            with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
                operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        original_versions = documents.document_versions
        with monkeypatch.context() as patch:
            def extra_version(*args, **kwargs):
                versions = original_versions(*args, **kwargs)
                return (*versions, versions[-1])

            patch.setattr(documents, "document_versions", extra_version)
            with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
                operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        original_attestations = type(store).list_document_provenance_attestations
        with monkeypatch.context() as patch:
            def extra_attestation(self, requested_document_id, **kwargs):
                rows = original_attestations(
                    self, requested_document_id, **kwargs
                )
                return (*rows, rows[-1]) if requested_document_id == document_id else rows

            patch.setattr(
                type(store),
                "list_document_provenance_attestations",
                extra_attestation,
            )
            with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
                operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
    finally:
        importer.close()


def test_shadow_fast_resume_rejects_root_and_recovery_catalog_drift(
    tmp_path,
    monkeypatch,
):
    operator, importer, task_store, _sources, _stores = _operator(tmp_path)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("catalog drift must not call import_note")

        monkeypatch.setattr(importer, "import_note", unexpected_import)
        conn = task_store.connect()
        try:
            root = conn.execute("SELECT root_id, label FROM task_local_file_roots").fetchone()
            recovery = conn.execute(
                "SELECT note_uuid, lifecycle FROM recovered_task_documents"
            ).fetchone()
            assert root is not None and recovery is not None
            conn.execute(
                "UPDATE task_local_file_roots SET label='drifted root' WHERE root_id=?",
                (root["root_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        conn = task_store.connect()
        try:
            conn.execute(
                "UPDATE task_local_file_roots SET label=? WHERE root_id=?",
                (root["label"], root["root_id"]),
            )
            conn.execute(
                "UPDATE recovered_task_documents SET lifecycle='drifted' "
                "WHERE note_uuid=?",
                (recovery["note_uuid"],),
            )
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
    finally:
        importer.close()


def test_shadow_fast_resume_rejects_unexpected_recovery_binding(
    tmp_path,
    monkeypatch,
):
    operator, importer, task_store, _sources, stores = _operator(tmp_path)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        conn = task_store.connect()
        try:
            recovery = conn.execute(
                "SELECT note_uuid, store_id, document_id, source_content_sha256 "
                "FROM task_migration_document_stage "
                "WHERE classification='recovered_task_document'"
            ).fetchone()
            assert recovery is not None
        finally:
            conn.close()
        store = stores.open_existing()
        DocumentCausalityStore(store.paths.sidecar).ensure_binding(
            domain_namespace="tasks",
            domain_kind="task_knowledge",
            domain_entity_id="t-unexpected-recovery-binding",
            domain_revision=str(recovery["source_content_sha256"]),
            store_id=str(recovery["store_id"]),
            document_id=str(recovery["document_id"]),
            role="task_knowledge",
            created_by="fixture",
            projection_path=None,
            projection_mode="none",
            migration_origin="legacy-task-cohort/v1",
        )

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("unexpected binding must not call import_note")

        monkeypatch.setattr(importer, "import_note", unexpected_import)
        with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
    finally:
        importer.close()


def test_shadow_fast_resume_final_snapshot_catches_concurrent_stage_drift(
    tmp_path,
    monkeypatch,
):
    operator, importer, task_store, _sources, _stores = _operator(tmp_path)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)

        def unexpected_import(*_args, **_kwargs):
            raise AssertionError("complete stages must not call import_note")

        monkeypatch.setattr(importer, "import_note", unexpected_import)
        original_snapshot = operator.ledger.shadow_stage_snapshot
        calls = 0

        def racing_snapshot(cohort_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                conn = task_store.connect()
                try:
                    conn.execute(
                        "UPDATE task_migration_local_link_stage "
                        "SET display_name='concurrent-drift.pdf' "
                        "WHERE link_id=(SELECT link_id "
                        "FROM task_migration_local_link_stage ORDER BY link_id LIMIT 1)"
                    )
                    conn.commit()
                finally:
                    conn.close()
            return original_snapshot(cohort_id)

        monkeypatch.setattr(operator.ledger, "shadow_stage_snapshot", racing_snapshot)
        with pytest.raises(LegacyInventoryError, match="fast-resume verification"):
            operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        assert calls == 2
    finally:
        importer.close()


def test_shadow_cli_rebuild_is_replay_safe_but_rejects_changed_task_rows(
    tmp_path,
    capsys,
):
    source, db, manifest = _fixture(tmp_path)
    manifest_path = tmp_path / "manifest.csv"
    receipts_path = tmp_path / "backup-receipts.json"
    _manifest_csv(manifest_path, manifest)
    receipts_path.write_text(json.dumps(BACKUP_RECEIPTS), encoding="utf-8")
    args = [
        "--cohort-id",
        "cohort-cli-replay",
        "--source-root",
        str(source),
        "--task-db",
        str(db),
        "--manifest",
        str(manifest_path),
        "--apply-shadow",
        "--sources-root",
        str(tmp_path / "sources-cli"),
        "--cowork-store-root",
        str(tmp_path / "cowork-cli"),
        "--truth-registry",
        str(tmp_path / "truth-registry-cli.db"),
        "--backup-receipts-json",
        str(receipts_path),
    ]

    assert import_legacy_main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert import_legacy_main(args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["documents_imported"] == first["documents_imported"] == 4

    store = TaskStore(db)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE task_metadata SET description='changed after inventory' "
            "WHERE task_id='t-a1'"
        )
    with pytest.raises(CohortStateError, match="another inventory"):
        import_legacy_main(args)


def test_rebuilt_inventory_replay_binds_canonical_cohort_digest_for_prepare(tmp_path):
    operator, importer, task_store, sources, stores = _operator(tmp_path)
    replay_importer = None
    try:
        first = operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        checkpoint = task_store.connect()
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.close()
        manifest = tuple(
            LegacyManifestEntry(
                relative_path=str(item.relative_path),
                byte_length=int(item.byte_length),
                sha256=str(item.content_sha256),
            )
            for item in operator.inventory.items
            if item.item_kind == "source_file"
        )
        rebuilt = LegacyTaskInventoryBuilder(
            cohort_id=operator.inventory.cohort_id,
            source_root=operator.source_root,
            task_db_path=task_store.path,
            manifest=manifest,
        ).build().require_valid()
        assert rebuilt.inventory_sha256 != operator.inventory.inventory_sha256

        principal = ActorRef(
            sources.authority_id,
            "legacy-task-migration",
            "service",
            "task-migration-tenant",
        )
        replay_importer = LegacyTaskDocumentImporter(
            source_root=operator.source_root,
            sources=sources,
            principal=principal,
            stores=stores,
        )
        replay_operator = LegacyTaskCutoverOperator(
            inventory=rebuilt,
            source_root=operator.source_root,
            task_store=task_store,
            document_importer=replay_importer,
            actor="operator:replay",
            session_id="session-replay",
        )
        with pytest.raises(CohortStateError, match="accepted by shadow replay"):
            replay_operator.prepare(target_authority_epoch="native:1")

        replay = replay_operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        assert replay["inventory_sha256"] == first["inventory_sha256"]
        assert replay["inventory_sha256"] != rebuilt.inventory_sha256
        replay_operator.ledger.arm_mutation_fence(
            rebuilt.cohort_id,
            fence_receipt_id="fixture-replay-fence",
            expected_process_generation=0,
            actor="operator:replay",
            session_id="session-replay",
        )
        prepared = replay_operator.prepare(target_authority_epoch="native:1")
        assert prepared["inventory_sha256"] == first["inventory_sha256"]
        assert prepared["state"] == "prepared"
    finally:
        if replay_importer is not None:
            replay_importer.close()
        importer.close()


def test_document_and_local_link_stage_replays_compare_every_immutable_field(tmp_path):
    operator, importer, task_store, _sources, _stores = _operator(tmp_path)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        conn = task_store.connect()
        try:
            staged = conn.execute(
                "SELECT * FROM task_migration_document_stage WHERE note_uuid=?",
                (IDLESS_NOTE,),
            ).fetchone()
            local_link = conn.execute(
                "SELECT * FROM task_migration_local_link_stage WHERE note_uuid=? "
                "ORDER BY link_id LIMIT 1",
                (IDLESS_NOTE,),
            ).fetchone()
            assert staged is not None and local_link is not None
            source_receipt_id = str(local_link["source_receipt_id"])
            conn.execute(
                "UPDATE task_migration_document_stage SET source_receipt_id=NULL "
                "WHERE cohort_id=? AND note_uuid=?",
                (operator.inventory.cohort_id, IDLESS_NOTE),
            )
            conn.commit()
        finally:
            conn.close()

        base = {
            "note_uuid": IDLESS_NOTE,
            "task_id": staged["task_id"],
            "store_id": staged["store_id"],
            "document_id": staged["document_id"],
            "binding_id": staged["binding_id"],
            "source_ref": staged["source_ref"],
            "source_content_sha256": staged["source_content_sha256"],
            "normalized_content_sha256": staged["normalized_content_sha256"],
            "document_content_sha256": staged["document_content_sha256"],
            "document_head_sha256": staged["document_head_sha256"],
            "rewrite_manifest": json.loads(staged["rewrite_manifest_json"]),
            "lifecycle": staged["lifecycle"],
            "classification": staged["classification"],
            "byte_parity": bool(staged["byte_parity"]),
            "normalized_parity": bool(staged["normalized_parity"]),
            "source_receipt_id": source_receipt_id,
        }
        operator.ledger.record_document_stage(operator.inventory.cohort_id, **base)
        conn = task_store.connect()
        try:
            assert conn.execute(
                "SELECT source_receipt_id FROM task_migration_document_stage "
                "WHERE cohort_id=? AND note_uuid=?",
                (operator.inventory.cohort_id, IDLESS_NOTE),
            ).fetchone()[0] == source_receipt_id
        finally:
            conn.close()

        for changed in (
            {"normalized_content_sha256": "f" * 64},
            {"rewrite_manifest": [*base["rewrite_manifest"], {"changed": True}]},
            {"source_receipt_id": "different-source-receipt"},
        ):
            with pytest.raises(CohortStateError, match="changed on retry"):
                operator.ledger.record_document_stage(
                    operator.inventory.cohort_id,
                    **{**base, **changed},
                )

        link_args = {
            key: local_link[key]
            for key in (
                "link_id", "task_id", "note_uuid", "store_id", "document_id",
                "root_id", "relative_path", "display_name", "suffix", "media_type",
                "byte_length", "sha256", "sensitivity", "allowed_action",
                "source_receipt_id", "policy_revision",
            )
        }
        operator.ledger.stage_local_file_link(operator.inventory.cohort_id, **link_args)
        with pytest.raises(CohortStateError, match="changed on retry"):
            operator.ledger.stage_local_file_link(
                operator.inventory.cohort_id,
                **{**link_args, "allowed_action": "reveal" if link_args["allowed_action"] == "open" else "open"},
            )
    finally:
        importer.close()


def test_prepare_apply_verify_activate_is_fenced_and_atomic(tmp_path, monkeypatch):
    operator, importer, task_store, _sources, _stores = _operator(tmp_path)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        for gate in REQUIRED_ACTIVATION_GATES - {"binding_cohort_verified"}:
            operator.ledger.record_gate(
                operator.inventory.cohort_id,
                gate,
                passed=True,
                evidence={"fixture": True, "manifest": operator.inventory.manifest_sha256},
            )
        with pytest.raises(CutoverPreconditionError):
            operator.prepare(target_authority_epoch="rollback:1")
        with pytest.raises(CutoverPreconditionError):
            operator.prepare(target_authority_epoch="native:1")
        operator.ledger.arm_mutation_fence(
            operator.inventory.cohort_id,
            fence_receipt_id="fixture-process-stop-receipt",
            expected_process_generation=0,
            actor="operator:test",
            session_id="session-test",
        )
        prepared = operator.prepare(target_authority_epoch="native:1")
        assert prepared["state"] == "prepared"
        transitioned = operator.apply_and_verify_bindings()
        assert transitioned == {"applied": 2, "verified": 2}
        with pytest.raises(CutoverPreconditionError):
            operator.activate(
                confirmation="yes",
                sealed_tree_manifest_sha256=operator.inventory.manifest_sha256,
            )
        with pytest.raises(CutoverPreconditionError):
            operator.activate(
                confirmation=ACTIVATION_CONFIRMATION,
                sealed_tree_manifest_sha256="f" * 64,
            )
        activated = operator.activate(
            confirmation=ACTIVATION_CONFIRMATION,
            sealed_tree_manifest_sha256=operator.inventory.manifest_sha256,
        )
        replayed = operator.activate(
            confirmation=ACTIVATION_CONFIRMATION,
            sealed_tree_manifest_sha256=operator.inventory.manifest_sha256,
        )
        with pytest.raises(CohortStateError):
            operator.activate(
                confirmation=ACTIVATION_CONFIRMATION,
                sealed_tree_manifest_sha256="f" * 64,
            )
    finally:
        importer.close()

    assert activated["state"] == replayed["state"] == "active"
    assert activated["retention_policy"] == "until_explicit_user_approval"
    system = task_store.system_state()
    assert system.authority_epoch == "native:1"
    assert system.rollback_fence is False
    assert system.process_generation == 1
    conn = task_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM task_document_links").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM task_local_file_links").fetchone()[0] == 2
        existing = conn.execute(
            "SELECT due_date, deadline_date, revision FROM task_metadata WHERE task_id='t-a1'"
        ).fetchone()
        assert tuple(existing) == ("2026-09-01", None, 2)
        assert {
            row[0]
            for row in conn.execute("SELECT tag FROM task_tags WHERE task_id='t-a1'")
        } == {"projects/alpha"}
        assert conn.execute(
            "SELECT COUNT(*) FROM task_event_outbox WHERE mutation='legacy_import_activate'"
        ).fetchone()[0] == 2
        idless = next(
            line.imported_task_id for line in operator.inventory.task_lines if line.is_idless
        )
        row = conn.execute(
            "SELECT legacy_import_receipt_id, due_date FROM task_metadata WHERE task_id=?",
            (idless,),
        ).fetchone()
        assert row is not None and str(row[0]).startswith("legacy-line:")
        assert conn.execute(
            "SELECT COUNT(*) FROM task_migration_idless_stage WHERE activated_at IS NOT NULL"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # Activation's external authority latch survives loss of the SQLite file
    # and prevents every compatibility mutator from falling back to Obsidian.
    from work_buddy.obsidian.tasks import mutations
    from work_buddy.tasks import runtime
    from work_buddy.tasks import store as native_store
    from work_buddy.tasks.errors import TaskAuthorityUnavailable
    from work_buddy.work_item import task_adapter

    latch_path = runtime.activation_authority_latch_path(task_store.path)
    assert latch_path.is_file()
    # Treat the already-activated temporary database as the configured
    # installation without changing which durable latch vouches for it.
    monkeypatch.setattr(runtime, "_canonical_default_latch_path", lambda: latch_path)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: task_store.path)
    monkeypatch.setattr(native_store, "default_task_db_path", lambda: task_store.path)
    legacy_calls: list[str] = []

    def legacy_writer(name):
        def invoke(**_kwargs):
            legacy_calls.append(name)
            return {"success": True}

        return invoke

    for legacy_name in (
        "create_task",
        "toggle_task",
        "update_task",
        "update_task_description",
        "set_task_tags_on_line",
        "delete_task",
        "assign_task",
    ):
        monkeypatch.setattr(mutations, legacy_name, legacy_writer(legacy_name))
    compatibility_mutators = (
        lambda: task_adapter.create("Must not reach legacy"),
        lambda: task_adapter.toggle("t-missing"),
        lambda: task_adapter.update("t-missing", state="active"),
        lambda: task_adapter.set_description("t-missing", "Changed"),
        lambda: task_adapter.set_tags("t-missing", ["systems/tasks"]),
        lambda: task_adapter.delete("t-missing"),
        lambda: task_adapter.assign("t-missing"),
    )

    def assert_all_mutators_fail_closed():
        for mutate in compatibility_mutators:
            with pytest.raises(TaskAuthorityUnavailable):
                mutate()
        assert legacy_calls == []

    moved_path = task_store.path.with_name("moved-task-metadata.db")
    task_store.path.replace(moved_path)
    assert_all_mutators_fail_closed()

    moved_path.replace(task_store.path)
    for suffix in ("-wal", "-shm"):
        task_store.path.with_name(task_store.path.name + suffix).unlink(missing_ok=True)
    task_store.path.write_bytes(b"not a sqlite database")
    assert_all_mutators_fail_closed()


def test_partial_binding_cutover_can_abort(tmp_path):
    operator, importer, task_store, _sources, _stores = _operator(tmp_path)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        for gate in REQUIRED_ACTIVATION_GATES - {"binding_cohort_verified"}:
            operator.ledger.record_gate(
                operator.inventory.cohort_id,
                gate,
                passed=True,
                evidence={"fixture": True},
            )
        operator.ledger.arm_mutation_fence(
            operator.inventory.cohort_id,
            fence_receipt_id="fixture-fence",
            expected_process_generation=0,
            actor="operator:test",
            session_id=None,
        )
        operator.prepare(target_authority_epoch="native:1")
        operator.ledger.apply_bindings(
            operator.inventory.cohort_id,
            causality=operator.causality(),
        )
        # Simulate a process death after the latch fsync but before SQLite's
        # authority CAS. Abort must commit legacy state first, then remove the
        # pending latch; replay remains safe and idempotent.
        from work_buddy.tasks import runtime

        runtime.arm_native_authority_latch(
            task_store.path,
            cohort_id=operator.inventory.cohort_id,
            target_authority_epoch="native:1",
            cutover_receipt_id="pending-activation-crash",
            armed_at="2026-08-23T12:00:00+00:00",
        )
        assert runtime.activation_authority_latch_path(task_store.path).is_file()
        aborted = operator.abort_before_activation()
        replayed = operator.abort_before_activation()
        assert aborted["state"] == "aborted"
        assert replayed["state"] == "aborted"
        assert not runtime.activation_authority_latch_path(task_store.path).exists()
        assert runtime.authority_epoch(task_store.path) == "legacy"
        assert task_store.system_state().authority_epoch == "legacy"
        assert task_store.system_state().rollback_fence is False

        # A separate activated fixture exercises post-cutover rollback prep.
    finally:
        importer.close()


def test_activation_drift_is_atomic_and_rollback_epoch_must_advance(tmp_path):
    operator, importer, task_store, _sources, _stores = _operator(tmp_path)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        for gate in REQUIRED_ACTIVATION_GATES - {"binding_cohort_verified"}:
            operator.ledger.record_gate(
                operator.inventory.cohort_id,
                gate,
                passed=True,
                evidence={"fixture": True},
            )
        operator.ledger.arm_mutation_fence(
            operator.inventory.cohort_id,
            fence_receipt_id="fixture-fence",
            expected_process_generation=0,
            actor="operator:test",
            session_id=None,
        )
        operator.prepare(target_authority_epoch="native:1")
        operator.apply_and_verify_bindings()
        with task_store.transaction() as conn:
            conn.execute(
                "UPDATE task_metadata SET updated_at='changed-after-inventory' "
                "WHERE task_id='t-a1'"
            )
        with pytest.raises(CutoverPreconditionError):
            operator.activate(
                confirmation=ACTIVATION_CONFIRMATION,
                sealed_tree_manifest_sha256=operator.inventory.manifest_sha256,
            )
        conn = task_store.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM task_document_links").fetchone()[0] == 0
            assert task_store.system_state().authority_epoch == "legacy"
        finally:
            conn.close()
        assert operator.abort_before_activation()["state"] == "aborted"
    finally:
        importer.close()

    second = tmp_path / "second"
    operator, importer, task_store, _sources, _stores = _operator(second)
    try:
        operator.shadow_import(backup_receipts=BACKUP_RECEIPTS)
        for gate in REQUIRED_ACTIVATION_GATES - {"binding_cohort_verified"}:
            operator.ledger.record_gate(
                operator.inventory.cohort_id,
                gate,
                passed=True,
                evidence={"fixture": True},
            )
        operator.ledger.arm_mutation_fence(
            operator.inventory.cohort_id,
            fence_receipt_id="fixture-fence",
            expected_process_generation=0,
            actor="operator:test",
            session_id=None,
        )
        operator.prepare(target_authority_epoch="native:2")
        operator.apply_and_verify_bindings()
        operator.activate(
            confirmation=ACTIVATION_CONFIRMATION,
            sealed_tree_manifest_sha256=operator.inventory.manifest_sha256,
        )
        receipt = {
            "legacy_database_schema_version": 11,
            "staged_tree_sha256": "a" * 64,
        }
        with pytest.raises(CutoverPreconditionError):
            operator.ledger.prepare_rollback(
                operator.inventory.cohort_id,
                rollback_authority_epoch="rollback:2",
                reverse_export_receipt=receipt,
                actor="operator:test",
                session_id=None,
            )
        with pytest.raises(CutoverPreconditionError):
            operator.ledger.prepare_rollback(
                operator.inventory.cohort_id,
                rollback_authority_epoch="native:3",
                reverse_export_receipt=receipt,
                actor="operator:test",
                session_id=None,
            )
        prepared = operator.ledger.prepare_rollback(
            operator.inventory.cohort_id,
            rollback_authority_epoch="rollback:3",
            reverse_export_receipt=receipt,
            actor="operator:test",
            session_id=None,
        )
        assert prepared["state"] == "rollback_prepared"
        assert task_store.system_state().authority_epoch == "native:2"
        assert task_store.system_state().rollback_fence is True
    finally:
        importer.close()
