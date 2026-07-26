"""Focused invariants for readiness, bootstrap, structured heads, and Save."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from work_buddy.cowork import bootstrap, materialization
from work_buddy.cowork.readiness import classify_document
from work_buddy.truth import documents, ydoc_store
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
    bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
    )
    assert target.read_bytes() == source
    assert store.resolve_blob_path(f"blobs/{sha256_bytes(source)}").read_bytes() == source


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
    _, compacted_head, new_cursor = ydoc_store.compact_and_advance(
        store,
        document_id=record.id,
        snapshot=compacted,
        expected_snapshot_sha256=sha256_bytes(compacted),
        expected_structured_head_sha256=live,
        actor=HUMAN,
    )
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
