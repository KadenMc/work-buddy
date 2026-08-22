from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from work_buddy.sources import (
    ActorRef,
    AttributionAssertion,
    SourceAccessDenied,
    SourceIntegrityFailure,
    SourceRef,
    SourceStore,
    SourceUsageConflict,
    redact_source,
    resolve_and_reserve_source,
    resolve_source,
)
from work_buddy.sources.errors import InvalidSourceRequest, SourceSchemaTooNew
from work_buddy.sources.migrations import (
    SOURCES_MIGRATIONS,
    _m001_sources_schema,
    _m002_recoverable_exports,
)
from work_buddy.storage.migrations import Migration, MigrationRunner


def _capture(store: SourceStore, tenant_id: str, content: str | bytes = "alpha"):
    return store.capture_source(
        content=content,
        source_role="human_input",
        tenant_scope_id=tenant_id,
        originating_surface="test",
    )


def test_store_identity_persists_and_future_schema_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "sources"
    store = SourceStore.create(root)
    assert SourceStore.open(root).authority_id == store.authority_id
    conn = sqlite3.connect(root / "store.db")
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    with pytest.raises(SourceSchemaTooNew):
        SourceStore.open(root)


def test_recoverable_export_migration_hash_remains_frozen_during_v3_upgrade(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(tmp_path / "sources-v2.db")
    v2_runner = MigrationRunner(
        "sources",
        [
            Migration(1, "retained source foundation", _m001_sources_schema),
            Migration(2, "recoverable issued-copy exports", _m002_recoverable_exports),
        ],
    )
    v2_runner.run(conn)

    frozen_hash = "0621fdb22cc3dddc78a8fee3b24ba70e9ba93d7827f5df78928f6158fa32e3f6"
    assert conn.execute(
        "SELECT code_hash FROM _migration_history WHERE version = 2"
    ).fetchone()[0] == frozen_hash

    SOURCES_MIGRATIONS.run(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute(
        "SELECT code_hash FROM _migration_history WHERE version = 2"
    ).fetchone()[0] == frozen_hash
    assert conn.execute(
        "SELECT COUNT(*) FROM _migration_history WHERE version = 3"
    ).fetchone()[0] == 1
    conn.close()


def test_exact_inline_and_blob_representations_are_immutable(
    source_store: SourceStore, tenant_id: str
) -> None:
    inline_text = "  α\r\nβ  "
    inline = _capture(source_store, tenant_id, inline_text)
    blob_bytes = b"x" * 128 + b"\r\n"
    first_blob = _capture(source_store, tenant_id, blob_bytes)
    second_blob = _capture(source_store, tenant_id, blob_bytes)
    assert first_blob.source_ref != second_blob.source_ref

    conn = source_store.connect()
    try:
        inline_row = source_store._representation_row(conn, inline.source_ref)
        assert bytes(inline_row["inline_content"]) == inline_text.encode("utf-8")
        blob_rows = conn.execute(
            "SELECT r.blob_sha256, b.ref_count FROM source_representations r "
            "JOIN source_blobs b ON b.content_sha256 = r.blob_sha256 "
            "WHERE r.authority_id = ? AND r.source_item_id IN (?, ?) ORDER BY r.source_item_id",
            (
                source_store.authority_id,
                first_blob.source_ref.item_id,
                second_blob.source_ref.item_id,
            ),
        ).fetchall()
        assert len(blob_rows) == 2
        assert blob_rows[0]["blob_sha256"] == blob_rows[1]["blob_sha256"]
        assert {row["ref_count"] for row in blob_rows} == {2}
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE source_items SET source_role = 'agent_output' "
                "WHERE authority_id = ? AND source_item_id = ?",
                (inline.source_ref.authority_id, inline.source_ref.item_id),
            )
    finally:
        conn.close()


def test_failed_source_transaction_removes_uncommitted_large_blob(
    source_store: SourceStore,
    tenant_id: str,
) -> None:
    content = b"uncommitted private bytes that exceed the inline threshold"

    with pytest.raises(InvalidSourceRequest):
        source_store.capture_source(
            content=content,
            source_role="not-a-source-role",
            tenant_scope_id=tenant_id,
            originating_surface="test",
        )

    assert source_store.blobs.digests() == set()


def test_open_recovers_blob_left_by_interrupted_precommit_stage(
    source_store: SourceStore,
) -> None:
    orphan = source_store.blobs.put(
        b"interrupted private bytes that were never committed"
    )
    assert source_store.blobs.path_for(orphan.sha256).is_file()

    reopened = SourceStore.open(
        source_store.paths.root,
        inline_content_bytes=source_store.inline_content_bytes,
        max_content_bytes=source_store.max_content_bytes,
    )

    assert reopened.blobs.path_for(orphan.sha256).exists() is False


def test_attribution_is_append_only_and_current_projection_uses_supersession(
    source_store: SourceStore,
    tenant_id: str,
    human: ActorRef,
    issuer: ActorRef,
) -> None:
    item = _capture(source_store, tenant_id)
    unknown_id = source_store.add_attribution(
        item.source_ref,
        AttributionAssertion(
            role="author",
            actor=None,
            state="unknown",
            asserted_by=issuer,
        ),
    )
    source_store.add_attribution(
        item.source_ref,
        AttributionAssertion(
            role="author",
            actor=human,
            basis="user_attestation",
            assurance="user_attested",
            asserted_by=human,
            supersedes_id=unknown_id,
        ),
    )
    conn = source_store.connect()
    try:
        current = source_store.current_attributions(conn, item.source_ref)
        assert len(current) == 1
        assert current[0].actor == human
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE source_attributions SET basis = 'rewritten' WHERE attribution_id = ?",
                (unknown_id,),
            )
    finally:
        conn.close()


def test_possession_is_not_access_and_resolution_checks_integrity(
    source_store: SourceStore,
    tenant_id: str,
    service: ActorRef,
    auth_sha: str,
) -> None:
    content = "exact source that is deliberately stored as a blob"
    item = _capture(source_store, tenant_id, content)
    with pytest.raises(SourceAccessDenied):
        resolve_source(
            source_store,
            source_ref=item.source_ref,
            principal=service,
            purpose="truth_evidence",
        )
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=service,
        purpose="truth_evidence",
        access_mode="content",
        authorization_fingerprint=auth_sha,
        content_boundary={
            "representation_id": item.primary_representation_id,
            "max_bytes": 100,
        },
    )
    resolved = resolve_source(
        source_store,
        source_ref=item.source_ref,
        principal=service,
        purpose="truth_evidence",
        expected_digest=source_store.get_representation(
            item.primary_representation_id
        ).content_sha256,
    )
    assert resolved.content == content.encode("utf-8")
    assert resolved.source_ref == item.source_ref
    assert resolved.authorization_context_sha256

    conn = source_store.connect()
    try:
        row = source_store._representation_row(conn, item.source_ref)
        blob_sha = str(row["blob_sha256"])
    finally:
        conn.close()
    source_store.blobs.path_for(blob_sha).write_bytes(b"tampered")
    with pytest.raises(SourceIntegrityFailure):
        resolve_source(
            source_store,
            source_ref=item.source_ref,
            principal=service,
            purpose="truth_evidence",
        )


def test_usage_reserve_recheck_ack_release_and_conflict(
    source_store: SourceStore,
    tenant_id: str,
    service: ActorRef,
    auth_sha: str,
) -> None:
    item = _capture(source_store, tenant_id, "usage source")
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=service,
        purpose="journal_effect",
        access_mode="content",
        authorization_fingerprint=auth_sha,
    )
    reserved = resolve_and_reserve_source(
        source_store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        principal=service,
        purpose="journal_effect",
        consumer_domain="journal",
        consumer_id="entry-00000001",
        use_kind="exact_insertion",
        disclosure_kind="exact_readable_copy",
        redaction_policy="scrub",
    )
    assert reserved.resolved.content == b"usage source"
    assert source_store.precommit_recheck_usage(
        reserved.reservation.usage_id
    ).status == "reserved"
    assert source_store.acknowledge_usage(reserved.reservation.usage_id).status == "acknowledged"
    assert source_store.acknowledge_usage(reserved.reservation.usage_id).status == "acknowledged"
    assert source_store.release_usage(reserved.reservation.usage_id).status == "released"
    with pytest.raises(SourceUsageConflict):
        source_store.reserve_usage(
            source_ref=item.source_ref,
            representation_id=item.primary_representation_id,
            principal=service,
            purpose="journal_effect",
            consumer_domain="journal",
            consumer_id="entry-00000001",
            use_kind="exact_insertion",
            disclosure_kind="semantic_derivative",
            redaction_policy="review",
        )


def test_conditional_usage_release_never_overtakes_redaction(
    source_store: SourceStore,
    tenant_id: str,
    service: ActorRef,
    auth_sha: str,
) -> None:
    item = _capture(source_store, tenant_id, "managed source")
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=service,
        purpose="journal_effect",
        access_mode="content",
        authorization_fingerprint=auth_sha,
    )
    usage = source_store.reserve_usage(
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        principal=service,
        purpose="journal_effect",
        consumer_domain="cowork_document",
        consumer_id="document-00000001",
        use_kind="exact_insertion",
        disclosure_kind="exact_readable_copy",
        redaction_policy="scrub",
    )
    source_store.acknowledge_usage(usage.usage_id)
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=service,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=auth_sha,
    )
    redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=service,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    )

    assert source_store.release_usage_if_source_active(usage.usage_id) is None
    conn = source_store.connect()
    try:
        row = conn.execute(
            "SELECT status,maintenance_state FROM source_usage_intents WHERE usage_id=?",
            (usage.usage_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert tuple(row) == ("acknowledged", "pending_redaction")
