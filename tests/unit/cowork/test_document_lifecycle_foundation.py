"""Focused invariants for readiness, bootstrap, structured heads, and Save."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from work_buddy.cowork import bootstrap, materialization, provenance, retirement
from work_buddy.cowork.file_importers import (
    FileImporter,
    FileImporterRegistry,
    MARKDOWN_FILE_IMPORTER,
    MARKDOWN_MAX_SOURCE_BYTES,
)
from work_buddy.cowork.lifecycle_state import inspect_lifecycle_state
from work_buddy.cowork.readiness import classify_document
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.export import export_store
from work_buddy.truth.identity import sha256_bytes

from .conftest import HUMAN


def _create_ready(
    store_ctx,
    *,
    path: str = "docs/ready.md",
    source: bytes = b"# Ready\n",
    key: str = "create-ready-0001",
):
    store = store_ctx["store"]
    intent, created = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": path,
            "title": "Ready",
            "initial_source_sha256": sha256_bytes(source),
            "idempotency_key": key,
        },
        source=source,
        actor=HUMAN,
    )
    assert created is True
    snapshot = b"YDOC-INITIALIZED:" + sha256_bytes(source).encode("ascii")
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
    )
    return documents.get_document(store, receipt["document_id"]), receipt, snapshot


def test_missing_snapshot_is_bootstrap_required_without_fabrication(
    store_ctx,
):
    store = store_ctx["store"]
    body = b"# Uninitialized\n"
    target = store_ctx["root"] / "docs" / "uninitialized.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    record = documents.register_document(
        store,
        path="docs/uninitialized.md",
        title="Uninitialized",
        document_class="co_authored",
        content_sha256=sha256_bytes(body),
        actor=HUMAN,
    )
    readiness = classify_document(store, record)
    assert readiness.initialization_state == "bootstrap_required"
    assert readiness.snapshot_sha256 is None
    assert readiness.structured_head_sha256 is None
    assert readiness.permissions["open"] is False
    assert readiness.permissions["repair"] is True


def test_store_write_holds_external_migration_lock_through_post_commit(
    store_ctx, monkeypatch
):
    store = store_ctx["store"]
    events: list[str] = []

    @contextmanager
    def fake_lock(folder, store_id, *, data_root=None, timeout=30.0):
        events.append("lock_acquired")
        yield
        events.append("lock_released")

    monkeypatch.setattr("work_buddy.truth.locks.migration_store_lock", fake_lock)
    monkeypatch.setattr(store, "_run_on_commit", lambda **_: events.append("post_commit"))
    with store.write_transaction() as conn:
        conn.execute("SELECT 1")
        events.append("transaction_body")
    assert events == [
        "lock_acquired",
        "transaction_body",
        "post_commit",
        "lock_released",
    ]


def test_bootstrap_create_commits_one_ready_version_and_receipt(store_ctx):
    record, receipt, snapshot = _create_ready(store_ctx)
    store = store_ctx["store"]
    assert (store_ctx["root"] / record.path).read_bytes() == b"# Ready\n"
    readiness = classify_document(store, record)
    assert readiness.initialization_state == "ready"
    assert readiness.structured_head_sha256 == ydoc_store.structured_head_from_segments(
        snapshot, ()
    )
    versions = documents.document_versions(store, record.id)
    assert len(versions) == 1
    assert versions[0].kind == "initial_import"
    assert versions[0].id == receipt["document_version_id"]
    with store.connect() as conn:
        intent = conn.execute(
            "SELECT state, receipt_json FROM cowork_bootstrap_intents"
        ).fetchone()
        path_key = conn.execute(
            "SELECT path_key FROM document_path_keys WHERE document_id = ?",
            (record.id,),
        ).fetchone()[0]
    assert intent["state"] == "committed"
    assert intent["receipt_json"]
    assert path_key == documents.document_path_key(record.path)


def test_bootstrap_create_rejects_a_projection_that_differs_from_source(
    store_ctx,
):
    store = store_ctx["store"]
    source = b"# Exact create\n"
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": "docs/exact-create.md",
            "initial_source_sha256": sha256_bytes(source),
            "idempotency_key": "create-projection-mismatch-0001",
        },
        source=source,
        actor=HUMAN,
    )
    snapshot = b"opaque-create-snapshot"
    projection = b"# Different managed projection\n"

    with pytest.raises(bootstrap.BootstrapError) as rejected:
        bootstrap.commit_bootstrap(
            store,
            bootstrap_id=intent.id,
            snapshot=snapshot,
            source_sha256=intent.source_sha256,
            snapshot_sha256=sha256_bytes(snapshot),
            ydoc_schema=bootstrap.YDOC_SCHEMA,
            actor=HUMAN,
            projection=projection,
            projection_sha256=sha256_bytes(projection),
        )

    assert rejected.value.code == "projection_not_lossless"
    assert not (store_ctx["root"] / "docs" / "exact-create.md").exists()
    assert bootstrap.get_intent(store, intent.id).state == "prepared"


def test_bootstrap_import_preserves_bom_and_crlf_bytes(store_ctx):
    store = store_ctx["store"]
    source = b"\xef\xbb\xbf# Exact\r\n\r\nBody\r\n"
    target = store_ctx["root"] / "notes" / "exact.markdown"
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "notes/exact.markdown",
            "title": "Exact",
            "expected_file_sha256": sha256_bytes(source),
            "idempotency_key": "import-exact-0001",
        },
        source=None,
        actor=HUMAN,
    )
    staged_intent, staged = bootstrap.read_staged_source(
        store, bootstrap_id=intent.id, actor=HUMAN
    )
    assert staged_intent.source_sha256 == sha256_bytes(source)
    assert staged == source
    snapshot = b"opaque-import-snapshot"
    projection = b"# Exact\n\nBody\n"
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
        projection=projection,
        projection_sha256=sha256_bytes(projection),
    )
    assert target.read_bytes() == source
    assert store.resolve_blob_path(f"blobs/{sha256_bytes(source)}").read_bytes() == source
    assert (
        store.resolve_blob_path(f"blobs/{sha256_bytes(projection)}").read_bytes()
        == projection
    )
    document = documents.get_document(store, intent.document_id)
    assert document.content_sha256 == sha256_bytes(projection)
    assert receipt["projection_sha256"] == sha256_bytes(projection)
    assert receipt["source_file_sha256"] == sha256_bytes(source)
    assert isinstance(receipt["authorship_attestation_id"], str)
    assert documents.source_writeback_policy(document) == "never"
    assert json.loads(document.meta_json)["source"]["kind"] == "file_import"
    assert receipt["source_writeback"] == "never"
    assert receipt["permissions"]["materialize"] is False
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    with pytest.raises(materialization.MaterializationError) as blocked:
        materialization.publish_projection(
            store,
            document_id=document.id,
            rendered_markdown="# Coerced\n",
            rendered_sha256=sha256_bytes(b"# Coerced\n"),
            expected_file_sha256=sha256_bytes(source),
            expected_structured_head_sha256=head,
            snapshot_sha256=document.ydoc_snapshot_sha256,
            actor=HUMAN,
        )
    assert blocked.value.code == "source_writeback_forbidden"
    assert target.read_bytes() == source

    replacement = b"opaque-import-snapshot-after-review"
    managed = materialization.commit_managed_projection(
        store,
        document_id=document.id,
        rendered_markdown="# Coerced\n",
        rendered_sha256=sha256_bytes(b"# Coerced\n"),
        expected_structured_head_sha256=head,
        snapshot_sha256=document.ydoc_snapshot_sha256,
        actor=HUMAN,
        replacement_snapshot=replacement,
        replacement_snapshot_sha256=sha256_bytes(replacement),
        version_detail="managed_projection:test",
    )
    refreshed = documents.get_document(store, document.id)
    assert managed["source_writeback"] == "never"
    assert managed["materialization_intent_id"] is None
    assert refreshed.content_sha256 == sha256_bytes(b"# Coerced\n")
    assert refreshed.ydoc_snapshot_sha256 == sha256_bytes(replacement)
    assert target.read_bytes() == source

    retire_intent, created = retirement.prepare_retirement(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="retire-detached-import-0001",
    )
    assert created is True
    retired = retirement.commit_retirement(
        store,
        document_id=document.id,
        intent_id=retire_intent.id,
        actor=HUMAN,
    )
    assert retired["file_retained"] is True
    assert target.read_bytes() == source


def test_bootstrap_recovery_never_unbounded_reads_a_grown_import_source(store_ctx):
    store = store_ctx["store"]
    source = b"# Prepared import\n"
    target = store_ctx["root"] / "imports" / "recovery-grown.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "imports/recovery-grown.md",
            "expected_file_sha256": sha256_bytes(source),
            "idempotency_key": "recovery-grown-import-0001",
        },
        source=None,
        actor=HUMAN,
    )
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_bootstrap_intents SET state = 'publishing' WHERE id = ?",
            (intent.id,),
        )
    with target.open("wb") as stream:
        stream.truncate(MARKDOWN_MAX_SOURCE_BYTES + 1)

    recovered = bootstrap.recover_bootstrap_intent(store, intent.id)

    assert recovered.state == "failed"
    assert recovered.recovery_detail == "recovery_required:external_state"
    assert target.stat().st_size == MARKDOWN_MAX_SOURCE_BYTES + 1


def test_bootstrap_and_lifecycle_are_source_format_neutral(
    store_ctx,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = store_ctx["store"]
    synthetic = FileImporter(
        "fixture/v1",
        (".wbtest",),
        "application/x-wbtest",
        4096,
        display_name="Fixture document",
        source_format="fixture",
    )
    monkeypatch.setattr(
        bootstrap,
        "DEFAULT_FILE_IMPORTERS",
        FileImporterRegistry((MARKDOWN_FILE_IMPORTER, synthetic)),
    )
    source = b"\x00future binary source\xff"
    projection = b"# Canonical projection\n"
    target = store_ctx["root"] / "imports" / "source.wbtest"
    target.parent.mkdir()
    target.write_bytes(source)

    intent, created = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "imports/source.wbtest",
            "title": "Source",
            "expected_file_sha256": sha256_bytes(source),
            "importer_id": synthetic.importer_id,
            # This browser value is only an assertion; the registry is
            # authoritative.
            "source_media_type": synthetic.media_type,
            "idempotency_key": "synthetic-import-0001",
        },
        source=None,
        actor=HUMAN,
    )
    assert created is True
    snapshot = b"opaque-synthetic-import-snapshot"
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
        projection=projection,
        projection_sha256=sha256_bytes(projection),
    )

    document = documents.get_document(store, receipt["document_id"])
    metadata = json.loads(document.meta_json)
    assert metadata["source"] == {
        "kind": "file_import",
        "path": "imports/source.wbtest",
        "sha256": sha256_bytes(source),
        "writeback_policy": "never",
        "importer_id": "fixture/v1",
        "format": "fixture",
        "media_type": "application/x-wbtest",
    }
    assert receipt["source_importer"] == synthetic.descriptor()
    assert inspect_lifecycle_state(store, document).file_path == target.resolve()
    assert target.read_bytes() == source

    retirement_intent, _ = retirement.prepare_retirement(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="synthetic-retire-0001",
    )
    retirement.commit_retirement(
        store,
        document_id=document.id,
        intent_id=retirement_intent.id,
        actor=HUMAN,
    )
    export_store(store, tmp_path / "synthetic-export.jsonl")
    assert target.read_bytes() == source


def test_bootstrap_enforces_the_authoritative_importer_binding(
    store_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = store_ctx["store"]
    synthetic = FileImporter(
        "fixture/v1",
        (".wbtest",),
        "application/x-wbtest",
        4,
    )
    monkeypatch.setattr(
        bootstrap,
        "DEFAULT_FILE_IMPORTERS",
        FileImporterRegistry((MARKDOWN_FILE_IMPORTER, synthetic)),
    )
    markdown = store_ctx["root"] / "wrong.md"
    markdown.write_bytes(b"ok")
    with pytest.raises(bootstrap.BootstrapError) as wrong_suffix:
        bootstrap.prepare_bootstrap(
            store,
            metadata={
                "mode": "import",
                "path": "wrong.md",
                "importer_id": "fixture/v1",
                "idempotency_key": "fixture-wrong-suffix-0001",
            },
            source=None,
            actor=HUMAN,
        )
    assert wrong_suffix.value.code == "importer_path_mismatch"

    source = store_ctx["root"] / "source.wbtest"
    source.write_bytes(b"ok")
    with pytest.raises(bootstrap.BootstrapError) as wrong_media:
        bootstrap.prepare_bootstrap(
            store,
            metadata={
                "mode": "import",
                "path": "source.wbtest",
                "importer_id": "fixture/v1",
                "source_media_type": "text/plain",
                "idempotency_key": "fixture-wrong-media-0001",
            },
            source=None,
            actor=HUMAN,
        )
    assert wrong_media.value.code == "importer_media_type_mismatch"

    source.write_bytes(b"12345")
    with pytest.raises(bootstrap.BootstrapError) as too_large:
        bootstrap.prepare_bootstrap(
            store,
            metadata={
                "mode": "import",
                "path": "source.wbtest",
                "importer_id": "fixture/v1",
                "idempotency_key": "fixture-too-large-0001",
            },
            source=None,
            actor=HUMAN,
        )
    assert too_large.value.code == "source_too_large"
    assert too_large.value.details == {
        "max_source_bytes": 4,
        "source_byte_length": 5,
    }


def test_detached_import_compacts_before_exportable_retirement(store_ctx):
    store = store_ctx["store"]
    source = b"# Detached source\n"
    target = store_ctx["root"] / "notes" / "detached-with-edits.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "notes/detached-with-edits.md",
            "expected_file_sha256": sha256_bytes(source),
            "idempotency_key": "import-detached-edits-0001",
        },
        source=None,
        actor=HUMAN,
    )
    snapshot = b"opaque-detached-edit-snapshot"
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
        projection=source,
        projection_sha256=sha256_bytes(source),
    )
    document = documents.get_document(store, receipt["document_id"])
    ydoc_store.append_update(
        store,
        document_id=document.id,
        update=b"durable-direct-edit",
    )
    edited = inspect_lifecycle_state(store, document)
    assert edited.unmaterialized_structured_edits is True

    with pytest.raises(retirement.RetirementError) as blocked:
        retirement.prepare_retirement(
            store,
            document_id=document.id,
            actor=HUMAN,
            idempotency_key="retire-detached-edits-0001",
        )
    assert blocked.value.code == "retirement_compaction_required"
    assert blocked.value.status == 409
    assert blocked.value.retryable is True
    assert blocked.value.details == {
        "document_id": document.id,
        "recovery_action": "compact_current_structured_head",
        "retry_after_compaction": True,
    }
    assert documents.current_lifecycle(store, document.id) == "active"
    assert target.read_bytes() == source

    # This is the same lossless, client-owned operation performed by the live
    # editor after its idle debounce. Python never interprets or combines Yjs
    # updates itself.
    compacted_snapshot = b"opaque-detached-edit-compacted-snapshot"
    assert edited.structured_head_sha256 is not None
    ydoc_store.compact_and_advance(
        store,
        document_id=document.id,
        snapshot=compacted_snapshot,
        expected_snapshot_sha256=sha256_bytes(compacted_snapshot),
        expected_structured_head_sha256=edited.structured_head_sha256,
        actor=HUMAN,
    )
    assert not ydoc_store.update_tail_present(store, document_id=document.id)

    retire_intent, created = retirement.prepare_retirement(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key="retire-detached-edits-after-compact-0001",
    )
    assert created is True
    retired = retirement.commit_retirement(
        store,
        document_id=document.id,
        intent_id=retire_intent.id,
        actor=HUMAN,
    )

    assert retired["history_retained"] is True
    assert documents.current_lifecycle(store, document.id) == "retired"
    assert not ydoc_store.update_tail_present(store, document_id=document.id)
    assert target.read_bytes() == source
    assert export_store(store).path.is_file()


def test_normalized_detached_import_does_not_offer_impossible_repair(
    store_ctx,
):
    store = store_ctx["store"]
    source = b"# Normalized\r\n\r\nImported source.\r\n"
    projection = b"# Normalized\n\nImported source.\n"
    target = store_ctx["root"] / "notes" / "normalized-repair.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "notes/normalized-repair.md",
            "expected_file_sha256": sha256_bytes(source),
            "idempotency_key": "normalized-repair-import-0001",
        },
        source=None,
        actor=HUMAN,
    )
    snapshot = b"opaque-normalized-repair-snapshot"
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
        projection=projection,
        projection_sha256=sha256_bytes(projection),
    )
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE documents SET ydoc_snapshot_sha256 = NULL WHERE id = ?",
            (receipt["document_id"],),
        )
    document = documents.get_document(store, receipt["document_id"])
    readiness = classify_document(store, document)
    assert readiness.initialization_state == "bootstrap_required"
    assert readiness.permissions["repair"] is False

    with pytest.raises(bootstrap.BootstrapError) as rejected:
        bootstrap.prepare_bootstrap(
            store,
            metadata={
                "mode": "repair",
                "path": document.path,
                "document_id": document.id,
                "expected_file_sha256": sha256_bytes(source),
                "idempotency_key": "normalized-repair-attempt-0001",
            },
            source=None,
            actor=HUMAN,
        )
    assert rejected.value.code == "repair_not_supported"
    assert rejected.value.status == 409
    assert target.read_bytes() == source


def test_import_intent_binds_importer_and_staged_provenance(store_ctx):
    store = store_ctx["store"]
    source = b"# Bound import\n"
    target = store_ctx["root"] / "imports" / "bound.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    attestation = {
        "schema": provenance.INPUT_ATTESTATION_SCHEMA,
        "authorship": {
            "kind": "ai",
            "contributors": [],
        },
        "human_review": {
            "status": "not_reviewed",
            "reviewers": [],
        },
    }
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "imports/bound.md",
            "expected_file_sha256": sha256_bytes(source),
            "importer_id": "markdown/v1",
            "source_media_type": "text/markdown",
            "authorship_attestation": attestation,
            "idempotency_key": "bound-import-provenance-0001",
        },
        source=None,
        actor=HUMAN,
    )

    assert intent.importer_id == "markdown/v1"
    assert intent.source_media_type == "text/markdown"
    assert intent.import_attestation_sha256 is not None
    staged_attestation = bootstrap._attestation_stage_path(store, intent.id)
    staged_attestation.write_text(
        '{"schema":"cowork-authorship-attestation/v1",'
        '"authorship":{"kind":"human","contributors":[{"kind":"current_user"}]},'
        '"human_review":{"status":"not_applicable","reviewers":[]}}',
        encoding="utf-8",
    )
    snapshot = b"opaque-import-integrity-snapshot"
    with pytest.raises(bootstrap.BootstrapError) as corrupt:
        bootstrap.commit_bootstrap(
            store,
            bootstrap_id=intent.id,
            snapshot=snapshot,
            source_sha256=intent.source_sha256,
            snapshot_sha256=sha256_bytes(snapshot),
            ydoc_schema=bootstrap.YDOC_SCHEMA,
            actor=HUMAN,
            projection=source,
            projection_sha256=sha256_bytes(source),
        )
    assert corrupt.value.code == "staged_attestation_corrupt"


def test_new_import_does_not_downgrade_a_missing_attestation_to_unknown(
    store_ctx,
):
    store = store_ctx["store"]
    source = b"# Missing determination\n"
    target = store_ctx["root"] / "imports" / "missing-determination.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(source)
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "import",
            "path": "imports/missing-determination.md",
            "expected_file_sha256": sha256_bytes(source),
            "idempotency_key": "missing-import-provenance-0001",
        },
        source=None,
        actor=HUMAN,
    )
    bootstrap._attestation_stage_path(store, intent.id).unlink()
    snapshot = b"opaque-missing-attestation-snapshot"

    with pytest.raises(bootstrap.BootstrapError) as missing:
        bootstrap.commit_bootstrap(
            store,
            bootstrap_id=intent.id,
            snapshot=snapshot,
            source_sha256=intent.source_sha256,
            snapshot_sha256=sha256_bytes(snapshot),
            ydoc_schema=bootstrap.YDOC_SCHEMA,
            actor=HUMAN,
            projection=source,
            projection_sha256=sha256_bytes(source),
        )

    assert missing.value.code == "staged_attestation_missing"


def test_bootstrap_idempotency_conflict_does_not_publish(store_ctx):
    store = store_ctx["store"]
    first, created = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": "docs/one.md",
            "idempotency_key": "same-key-0001",
        },
        source=b"one",
        actor=HUMAN,
    )
    assert created is True
    repeated, created = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": "docs/one.md",
            "idempotency_key": "same-key-0001",
        },
        source=b"one",
        actor=HUMAN,
    )
    assert created is False and repeated.id == first.id
    with pytest.raises(bootstrap.BootstrapError) as conflict:
        bootstrap.prepare_bootstrap(
            store,
            metadata={
                "mode": "create",
                "path": "docs/two.md",
                "idempotency_key": "same-key-0001",
            },
            source=b"two",
            actor=HUMAN,
        )
    assert conflict.value.code == "idempotency_conflict"
    assert not (store_ctx["root"] / "docs" / "one.md").exists()
    assert not (store_ctx["root"] / "docs" / "two.md").exists()


def test_structured_head_vectors_and_epoch_reset(store_ctx):
    assert ydoc_store.structured_head_from_segments(b"", ()) == (
        "28ca3277b470f732c7f9087e532be8ffca53d81bfc88f107ed5353102f7765ac"
    )
    snapshot = bytes.fromhex("000102ff")
    assert ydoc_store.structured_head_from_segments(snapshot, ()) == (
        "5a91acc38df34d983731b7e7e458df87d3f1dc58de4b785f744f9d2ea6fb43df"
    )
    assert ydoc_store.structured_head_from_segments(
        snapshot, (b"alpha", bytes.fromhex("00ff10"))
    ) == "bfe6c9a0388e277d92e2095b77e176ee5327a6735a7cb05ebb6bbb9694bbb8bd"

    record, _, initial = _create_ready(
        store_ctx, path="docs/cursor.md", key="cursor-ready-0001"
    )
    store = store_ctx["store"]
    base = ydoc_store.structured_head_from_segments(initial, ())
    cursor, live = ydoc_store.append_update_cas(
        store,
        document_id=record.id,
        snapshot_sha256=record.ydoc_snapshot_sha256,
        update=b"human-update",
        expected_structured_head_sha256=base,
    )
    assert cursor.startswith("cowork-cursor-v1:0:")
    with pytest.raises(ydoc_store.StructuredHeadConflict):
        ydoc_store.append_update_cas(
            store,
            document_id=record.id,
            snapshot_sha256=record.ydoc_snapshot_sha256,
            update=b"stale",
            expected_structured_head_sha256=base,
        )
    compacted = b"client-compacted-state"
    _, compacted_head, new_cursor, projection_receipt = (
        ydoc_store.compact_and_advance(
            store,
            document_id=record.id,
            snapshot=compacted,
            expected_snapshot_sha256=sha256_bytes(compacted),
            expected_structured_head_sha256=live,
            actor=HUMAN,
        )
    )
    assert projection_receipt is None
    assert compacted_head == ydoc_store.structured_head_from_segments(compacted, ())
    assert new_cursor != cursor
    updates, _, reset = ydoc_store.read_epoch_updates(
        store, document_id=record.id, since_cursor=cursor
    )
    assert updates == ()
    assert reset is True


def test_materialization_retains_history_and_rejects_stale_file(store_ctx):
    record, receipt, _ = _create_ready(
        store_ctx, path="docs/save.md", key="save-ready-0001"
    )
    store = store_ctx["store"]
    rendered = "# Saved\n\nNew body.\n"
    result = materialization.publish_projection(
        store,
        document_id=record.id,
        rendered_markdown=rendered,
        rendered_sha256=sha256_bytes(rendered.encode()),
        expected_file_sha256=record.content_sha256,
        expected_structured_head_sha256=receipt["structured_head_sha256"],
        snapshot_sha256=receipt["snapshot_sha256"],
        actor=HUMAN,
        idempotency_key="save-materialize-0001",
    )
    assert result["drift_state"] == "clean"
    assert (store_ctx["root"] / record.path).read_text(encoding="utf-8") == rendered
    versions = documents.document_versions(store, record.id)
    assert [item.kind for item in versions] == ["initial_import", "materialized"]
    assert store.resolve_blob_path(f"blobs/{record.content_sha256}").is_file()
    assert store.resolve_blob_path(f"blobs/{result['new_file_sha256']}").is_file()

    external = b"# External\n"
    (store_ctx["root"] / record.path).write_bytes(external)
    with pytest.raises(materialization.MaterializationError) as stale:
        materialization.publish_projection(
            store,
            document_id=record.id,
            rendered_markdown="# Again\n",
            rendered_sha256=sha256_bytes(b"# Again\n"),
            expected_file_sha256=result["new_file_sha256"],
            expected_structured_head_sha256=receipt["structured_head_sha256"],
            snapshot_sha256=receipt["snapshot_sha256"],
            actor=HUMAN,
        )
    assert stale.value.code == "stale_file"
    assert (store_ctx["root"] / record.path).read_bytes() == external


def test_materialization_external_gap_race_retains_both_byte_sets(
    store_ctx, monkeypatch
):
    record, receipt, _ = _create_ready(
        store_ctx, path="docs/race.md", key="race-ready-0001"
    )
    store = store_ctx["store"]
    original = materialization._exclusive_publish

    def race(path: Path, payload: bytes) -> None:
        path.write_bytes(b"external-gap-write")
        original(path, payload)

    monkeypatch.setattr(materialization, "_exclusive_publish", race)
    with pytest.raises(materialization.MaterializationError) as conflict:
        materialization.publish_projection(
            store,
            document_id=record.id,
            rendered_markdown="# Proposed\n",
            rendered_sha256=sha256_bytes(b"# Proposed\n"),
            expected_file_sha256=record.content_sha256,
            expected_structured_head_sha256=receipt["structured_head_sha256"],
            snapshot_sha256=receipt["snapshot_sha256"],
            actor=HUMAN,
        )
    assert conflict.value.code == "external_write_race"
    assert (store_ctx["root"] / record.path).read_bytes() == b"external-gap-write"
    quarantines = list((store_ctx["root"] / "docs").glob(".*.previous"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"# Ready\n"
