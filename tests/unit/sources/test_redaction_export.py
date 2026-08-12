from __future__ import annotations

from pathlib import Path

import pytest
import work_buddy.sources.export as source_export_module

from work_buddy.sources import (
    ActorRef,
    ExportAuthorization,
    ImportAuthorization,
    SourceAccessDenied,
    SourceRedacted,
    SourceStore,
    SourceUsageConflict,
    abort_source_export,
    export_sources,
    import_sources,
    recover_source_export,
    redact_source,
    resolve_source,
)


def _capture(store: SourceStore, tenant_id: str, content: str):
    return store.capture_source(
        content=content,
        source_role="human_input",
        tenant_scope_id=tenant_id,
        originating_surface="test",
    )


def test_redaction_removes_readable_content_and_queues_managed_copies(
    source_store: SourceStore,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    auth_sha: str,
) -> None:
    secret = "private retained content that is large enough for blob storage"
    item = _capture(source_store, tenant_id, secret)
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
        consumer_domain="journal",
        consumer_id="entry-00000001",
        use_kind="exact_insertion",
        disclosure_kind="exact_readable_copy",
        redaction_policy="scrub",
    )
    source_store.acknowledge_usage(usage.usage_id)
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=auth_sha,
    )
    conn = source_store.connect()
    try:
        blob = source_store._representation_row(conn, item.source_ref)["blob_sha256"]
    finally:
        conn.close()

    result = redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=human,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    )
    assert result.managed_copy_state == "pending"
    assert len(result.pending_effect_ids) == 1
    assert not source_store.blobs.path_for(str(blob)).exists()
    with pytest.raises(SourceRedacted) as caught:
        resolve_source(
            source_store,
            source_ref=item.source_ref,
            principal=service,
            purpose="journal_effect",
        )
    assert secret not in str(caught.value)
    with pytest.raises(SourceUsageConflict):
        source_store.precommit_recheck_usage(usage.usage_id)
    conn = source_store.connect()
    try:
        representation = conn.execute(
            "SELECT inline_content, blob_sha256, redacted_at FROM source_representations "
            "WHERE representation_id = ?",
            (item.primary_representation_id,),
        ).fetchone()
        assert representation["inline_content"] is None
        assert representation["blob_sha256"] is None
        assert representation["redacted_at"] is not None
        usage_row = conn.execute(
            "SELECT maintenance_state FROM source_usage_intents WHERE usage_id = ?",
            (usage.usage_id,),
        ).fetchone()
        assert usage_row[0] == "pending_redaction"
        effect_payload = conn.execute(
            "SELECT payload_json FROM source_outbox WHERE effect_id = ?",
            (result.pending_effect_ids[0],),
        ).fetchone()[0]
        assert secret not in effect_payload
    finally:
        conn.close()
    assert redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=human,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    ) == result
    source_store.release_usage(usage.usage_id)
    completed = redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=human,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    )
    assert completed.managed_copy_state == "complete"


def test_export_import_retains_commandless_redaction_recovery_work(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    auth_sha: str,
) -> None:
    item = _capture(source_store, tenant_id, "portable redaction recovery")
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
        consumer_domain="journal",
        consumer_id="entry-portable-redaction",
        use_kind="exact_insertion",
        disclosure_kind="exact_readable_copy",
        redaction_policy="scrub",
    )
    source_store.acknowledge_usage(usage.usage_id)
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=auth_sha,
    )
    result = redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=human,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    )
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="export",
        access_mode="metadata",
        authorization_fingerprint=auth_sha,
    )
    archive = tmp_path / "redaction-recovery.jsonl"
    export_sources(
        source_store,
        archive,
        authorization=ExportAuthorization(human, auth_sha, include_content=False),
        source_refs=[item.source_ref],
    )

    target = SourceStore.create(
        tmp_path / "imported-redaction",
        authority_id=source_store.authority_id,
    )
    importer = ActorRef(
        target.authority_id,
        "importer-00000001",
        "human",
        tenant_id,
    )
    import_sources(
        target,
        archive,
        authorization=ImportAuthorization(importer, "b" * 64),
    )
    conn = target.connect()
    try:
        event = conn.execute(
            "SELECT managed_copy_state FROM source_redaction_events "
            "WHERE redaction_event_id=?",
            (result.redaction_event_id,),
        ).fetchone()
        effect = conn.execute(
            "SELECT command_id,status,error_code,payload_json FROM source_outbox "
            "WHERE effect_type='source.redaction'"
        ).fetchone()
    finally:
        conn.close()
    assert event[0] == "pending"
    assert effect[0] is None
    assert effect[1:3] == ("paused", "imported_inert")
    assert usage.usage_id in effect[3]


def test_export_import_preserves_foreign_source_identity_but_not_access_grants(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    auth_sha: str,
) -> None:
    item = _capture(source_store, tenant_id, "portable exact source")
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="export",
        access_mode="content",
        authorization_fingerprint=auth_sha,
    )
    archive = tmp_path / "source-export.jsonl"
    exported = export_sources(
        source_store,
        archive,
        authorization=ExportAuthorization(human, auth_sha),
    )
    assert exported.item_count == 1
    assert len(exported.usage_ids) == 1
    assert archive.read_text(encoding="utf-8").count("portable exact source") == 0

    destination = SourceStore.create(tmp_path / "destination")
    destination_actor = ActorRef(
        destination.authority_id, "importer-00000001", "human", tenant_id
    )
    imported = import_sources(
        destination,
        archive,
        authorization=ImportAuthorization(destination_actor, "b" * 64),
    )
    assert imported.item_count == 1
    assert imported.mappings[f"{item.source_ref.authority_id}:{item.source_ref.item_id}"] == item.source_ref
    imported_item = destination.get_item(item.source_ref)
    assert imported_item is not None
    assert imported_item.custodian_authority_id == destination.authority_id
    conn = destination.connect()
    try:
        authority = conn.execute(
            "SELECT custody_kind FROM source_authorities WHERE authority_id = ?",
            (item.source_ref.authority_id,),
        ).fetchone()
        assert authority[0] == "foreign"
        assert conn.execute(
            "SELECT COUNT(*) FROM source_access_bindings WHERE authority_id = ? "
            "AND source_item_id = ? AND revoked_at IS NULL",
            (item.source_ref.authority_id, item.source_ref.item_id),
        ).fetchone()[0] == 0
    finally:
        conn.close()
    destination.grant_access(
        source_ref=item.source_ref,
        principal=destination_actor,
        purpose="review",
        access_mode="content",
        authorization_fingerprint="b" * 64,
    )
    assert resolve_source(
        destination,
        source_ref=item.source_ref,
        principal=destination_actor,
        purpose="review",
    ).content == b"portable exact source"
    repeated = import_sources(
        destination,
        archive,
        authorization=ImportAuthorization(destination_actor, "b" * 64),
    )
    assert repeated.import_id == imported.import_id


def test_export_recovers_after_archive_write_before_usage_acknowledgement(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    auth_sha: str,
    monkeypatch,
) -> None:
    item = _capture(source_store, tenant_id, "recoverable export bytes")
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="export",
        access_mode="content",
        authorization_fingerprint=auth_sha,
    )
    archive = tmp_path / "recoverable-source-export.jsonl"
    original_acknowledge = source_store.acknowledge_usage
    calls = 0

    def interrupted(usage_id: str, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated process stop after archive write")
        return original_acknowledge(usage_id, **kwargs)

    monkeypatch.setattr(source_store, "acknowledge_usage", interrupted)
    with pytest.raises(RuntimeError, match="simulated process stop"):
        export_sources(
            source_store,
            archive,
            authorization=ExportAuthorization(human, auth_sha),
            source_refs=[item.source_ref],
            idempotency_key="portable-export-1",
        )
    assert archive.is_file()
    conn = source_store.connect()
    try:
        row = conn.execute(
            "SELECT export_id,state,payload_sha256 FROM source_export_operations"
        ).fetchone()
        assert row is not None and row["state"] == "written"
        export_id = str(row["export_id"])
        assert conn.execute(
            "SELECT COUNT(*) FROM source_usage_intents WHERE consumer_id=?",
            (export_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()

    monkeypatch.setattr(source_store, "acknowledge_usage", original_acknowledge)
    recovered = recover_source_export(source_store, export_id)
    assert recovered.path == archive
    assert recovered.sha256 == row["payload_sha256"]
    conn = source_store.connect()
    try:
        assert conn.execute(
            "SELECT state FROM source_export_operations WHERE export_id=?",
            (export_id,),
        ).fetchone()[0] == "completed"
        assert conn.execute(
            "SELECT COUNT(*) FROM source_usage_intents WHERE consumer_id=?",
            (export_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_export_adopts_matching_archive_written_before_state_commit(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    auth_sha: str,
    monkeypatch,
) -> None:
    item = _capture(source_store, tenant_id, "archive exists before state commit")
    for purpose, mode in (("export", "content"), ("redaction", "metadata")):
        source_store.grant_access(
            source_ref=item.source_ref,
            principal=human,
            purpose=purpose,
            access_mode=mode,
            authorization_fingerprint=auth_sha,
        )
    archive = tmp_path / "written-before-state.jsonl"
    real_write = source_export_module.atomic_write_bytes

    def stop_after_write(path, payload):
        real_write(path, payload)
        raise RuntimeError("simulated stop before written state")

    monkeypatch.setattr(source_export_module, "atomic_write_bytes", stop_after_write)
    with pytest.raises(RuntimeError, match="before written state"):
        export_sources(
            source_store,
            archive,
            authorization=ExportAuthorization(human, auth_sha),
            source_refs=[item.source_ref],
            idempotency_key="write-boundary",
        )
    conn = source_store.connect()
    try:
        row = conn.execute("SELECT * FROM source_export_operations").fetchone()
        assert row is not None and row["state"] == "prepared"
        export_id = str(row["export_id"])
        expected_sha = str(row["payload_sha256"])
    finally:
        conn.close()
    assert archive.is_file()

    # Redaction may race after the issued bytes exist. Recovery adopts those
    # exact bytes and leaves the issued-copy maintenance warning intact.
    redaction = redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=human,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    )
    monkeypatch.setattr(source_export_module, "atomic_write_bytes", real_write)
    recovered = recover_source_export(
        source_store,
        export_id,
        authorization=ExportAuthorization(human, auth_sha),
    )
    assert recovered.sha256 == expected_sha
    assert redaction.issued_copy_state == "uncontrolled_copies_possible"
    conn = source_store.connect()
    try:
        assert conn.execute(
            "SELECT state FROM source_export_operations WHERE export_id=?",
            (export_id,),
        ).fetchone()[0] == "completed"
    finally:
        conn.close()


def test_export_never_overwrites_mismatched_archive_during_recovery(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    auth_sha: str,
    monkeypatch,
) -> None:
    item = _capture(source_store, tenant_id, "archive mismatch during recovery")
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="export",
        access_mode="content",
        authorization_fingerprint=auth_sha,
    )
    archive = tmp_path / "mismatched-prepared-export.jsonl"
    real_write = source_export_module.atomic_write_bytes

    def stop_after_write(path, payload):
        real_write(path, payload)
        raise RuntimeError("simulated stop before written state")

    monkeypatch.setattr(source_export_module, "atomic_write_bytes", stop_after_write)
    with pytest.raises(RuntimeError, match="before written state"):
        export_sources(
            source_store,
            archive,
            authorization=ExportAuthorization(human, auth_sha),
            source_refs=[item.source_ref],
            idempotency_key="mismatched-write-boundary",
        )
    conn = source_store.connect()
    try:
        row = conn.execute("SELECT export_id,state FROM source_export_operations").fetchone()
        assert row is not None and row["state"] == "prepared"
        export_id = str(row["export_id"])
    finally:
        conn.close()

    archive.write_bytes(b"not the prepared archive\n")
    monkeypatch.setattr(source_export_module, "atomic_write_bytes", real_write)
    with pytest.raises(source_export_module.SourceImportInvalid):
        recover_source_export(
            source_store,
            export_id,
            authorization=ExportAuthorization(human, auth_sha),
        )
    assert archive.read_bytes() == b"not the prepared archive\n"
    conn = source_store.connect()
    try:
        assert conn.execute(
            "SELECT state FROM source_export_operations WHERE export_id=?",
            (export_id,),
        ).fetchone()[0] == "prepared"
    finally:
        conn.close()


def test_export_rebuilds_absent_prepared_archive_only_when_digest_matches(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    auth_sha: str,
    monkeypatch,
) -> None:
    item = _capture(source_store, tenant_id, "prepared deterministic export")
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="export",
        access_mode="content",
        authorization_fingerprint=auth_sha,
    )
    archive = tmp_path / "absent-prepared.jsonl"
    real_write = source_export_module.atomic_write_bytes
    monkeypatch.setattr(
        source_export_module,
        "atomic_write_bytes",
        lambda _path, _payload: (_ for _ in ()).throw(
            RuntimeError("simulated stop before archive write")
        ),
    )
    with pytest.raises(RuntimeError, match="before archive write"):
        export_sources(
            source_store,
            archive,
            authorization=ExportAuthorization(human, auth_sha),
            source_refs=[item.source_ref],
            idempotency_key="absent-boundary",
        )
    assert not archive.exists()
    monkeypatch.setattr(source_export_module, "atomic_write_bytes", real_write)
    recovered = export_sources(
        source_store,
        archive,
        authorization=ExportAuthorization(human, auth_sha),
        source_refs=[item.source_ref],
        idempotency_key="absent-boundary",
    )
    assert archive.is_file() and recovered.item_count == 1


def test_redaction_before_prepared_archive_write_requires_safe_abort(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    auth_sha: str,
    monkeypatch,
) -> None:
    item = _capture(source_store, tenant_id, "redact before archive write")
    for purpose, mode in (("export", "content"), ("redaction", "metadata")):
        source_store.grant_access(
            source_ref=item.source_ref,
            principal=human,
            purpose=purpose,
            access_mode=mode,
            authorization_fingerprint=auth_sha,
        )
    archive = tmp_path / "redacted-before-write.jsonl"
    real_write = source_export_module.atomic_write_bytes
    monkeypatch.setattr(
        source_export_module,
        "atomic_write_bytes",
        lambda _path, _payload: (_ for _ in ()).throw(RuntimeError("stop before write")),
    )
    with pytest.raises(RuntimeError, match="stop before write"):
        export_sources(
            source_store,
            archive,
            authorization=ExportAuthorization(human, auth_sha),
            source_refs=[item.source_ref],
            idempotency_key="redaction-before-write",
        )
    export_id = source_export_module.source_export_status(source_store)[0]["export_id"]
    redacted = redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=human,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    )
    assert redacted.managed_copy_state == "pending"
    monkeypatch.setattr(source_export_module, "atomic_write_bytes", real_write)
    with pytest.raises((source_export_module.SourceImportInvalid, SourceAccessDenied)):
        recover_source_export(
            source_store,
            str(export_id),
            authorization=ExportAuthorization(human, auth_sha),
        )
    aborted = abort_source_export(
        source_store,
        str(export_id),
        authorization=ExportAuthorization(human, auth_sha),
    )
    assert aborted["state"] == "failed"
    assert not archive.exists()
    completed = redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=human,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    )
    assert completed.managed_copy_state == "complete"


def test_restore_import_reconstitutes_same_authority_and_pauses_recovery_work(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    service: ActorRef,
    auth_sha: str,
) -> None:
    item = _capture(source_store, tenant_id, "portable managed copy")
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
        consumer_domain="journal",
        consumer_id="portable-entry",
        use_kind="exact_insertion",
        disclosure_kind="exact_readable_copy",
        redaction_policy="scrub",
    )
    source_store.acknowledge_usage(usage.usage_id)
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=auth_sha,
    )
    redacted = redact_source(
        source_store,
        source_ref=item.source_ref,
        actor=human,
        authorization_fingerprint=auth_sha,
        reason_code="user_requested",
    )
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="export",
        access_mode="metadata",
        authorization_fingerprint=auth_sha,
    )
    archive = tmp_path / "recovery.jsonl"
    export_sources(
        source_store,
        archive,
        authorization=ExportAuthorization(human, auth_sha, include_content=False),
        source_refs=[item.source_ref],
        idempotency_key="restore-import",
    )

    restored = SourceStore.create(
        tmp_path / "restored",
        authority_id=source_store.authority_id,
    )
    result = import_sources(
        restored,
        archive,
        authorization=ImportAuthorization(
            human,
            "b" * 64,
            allow_foreign_authorities=False,
            collision_policy="reject",
            restore_operational_state=True,
        ),
    )

    assert result.mappings[
        f"{item.source_ref.authority_id}:{item.source_ref.item_id}"
    ] == item.source_ref
    conn = restored.connect()
    try:
        recovered_usage = conn.execute(
            "SELECT status,maintenance_state FROM source_usage_intents WHERE usage_id=?",
            (usage.usage_id,),
        ).fetchone()
        effect = conn.execute(
            "SELECT command_id,status,error_code,payload_sha256 FROM source_outbox "
            "WHERE effect_id=?",
            (redacted.pending_effect_ids[0],),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(recovered_usage) == ("acknowledged", "pending_redaction")
    assert effect["command_id"] is None
    assert effect["status"] == "paused"
    assert effect["error_code"] == "imported_inert"
    assert len(effect["payload_sha256"]) == 64


def test_export_recovers_after_partial_usage_ack_batch(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    auth_sha: str,
    monkeypatch,
) -> None:
    items = [
        _capture(source_store, tenant_id, "first issued copy"),
        _capture(source_store, tenant_id, "second issued copy"),
    ]
    for item in items:
        source_store.grant_access(
            source_ref=item.source_ref,
            principal=human,
            purpose="export",
            access_mode="content",
            authorization_fingerprint=auth_sha,
        )
    original_acknowledge = source_store.acknowledge_usage
    calls = 0

    def fail_second(usage_id: str, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated stop during acknowledgement batch")
        return original_acknowledge(usage_id, **kwargs)

    monkeypatch.setattr(source_store, "acknowledge_usage", fail_second)
    archive = tmp_path / "partial-ack.jsonl"
    with pytest.raises(RuntimeError, match="acknowledgement batch"):
        export_sources(
            source_store,
            archive,
            authorization=ExportAuthorization(human, auth_sha),
            source_refs=[item.source_ref for item in items],
            idempotency_key="partial-ack-boundary",
        )
    conn = source_store.connect()
    try:
        export_id = str(
            conn.execute("SELECT export_id FROM source_export_operations").fetchone()[0]
        )
        statuses = {
            str(row[0])
            for row in conn.execute(
                "SELECT status FROM source_usage_intents WHERE consumer_id=?",
                (export_id,),
            ).fetchall()
        }
        assert statuses == {"reserved", "acknowledged"}
    finally:
        conn.close()
    monkeypatch.setattr(source_store, "acknowledge_usage", original_acknowledge)
    recovered = recover_source_export(source_store, export_id)
    assert len(recovered.usage_ids) == 2
    conn = source_store.connect()
    try:
        assert conn.execute(
            "SELECT state FROM source_export_operations WHERE export_id=?",
            (export_id,),
        ).fetchone()[0] == "completed"
        assert conn.execute(
            "SELECT COUNT(*) FROM source_usage_intents WHERE consumer_id=?",
            (export_id,),
        ).fetchone()[0] == 2
    finally:
        conn.close()


def _conflicting_target(
    source: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    source_item_id: str,
) -> SourceStore:
    target = SourceStore.create(
        tmp_path / f"conflict-{source_item_id[-8:]}", authority_id=source.authority_id
    )
    content = b"conflicting content"
    with target.write_transaction() as conn:
        staged = target._stage_if_needed(content, conn=conn)
        target._capture_source(
            conn,
            content=content,
            staged_blob=staged,
            source_role="human_input",
            tenant_scope_id=tenant_id,
            originating_surface="test",
            media_type="text/plain",
            representation_kind="decoded_text",
            encoding="utf-8",
            schema_type=None,
            origin_ref=None,
            native_revision=None,
            fidelity="exact",
            namespace=None,
            sensitivity_class="private",
            retention_class="durable",
            occurred_at=None,
            provider_observed_at=None,
            received_at="2026-08-09T12:00:00.000+00:00",
            attributions=(),
            producer=None,
            source_item_id=source_item_id,
        )
    return target


@pytest.mark.parametrize("policy, expected_quarantine, expected_remap", [("quarantine", 1, 0), ("remap", 0, 1)])
def test_import_collision_is_explicitly_quarantined_or_remapped(
    source_store: SourceStore,
    tmp_path: Path,
    tenant_id: str,
    human: ActorRef,
    auth_sha: str,
    policy: str,
    expected_quarantine: int,
    expected_remap: int,
) -> None:
    item = _capture(source_store, tenant_id, "original export content")
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=human,
        purpose="export",
        access_mode="content",
        authorization_fingerprint=auth_sha,
    )
    archive = tmp_path / f"collision-{policy}.jsonl"
    export_sources(
        source_store,
        archive,
        authorization=ExportAuthorization(human, auth_sha),
        source_refs=[item.source_ref],
    )
    target = _conflicting_target(
        source_store, tmp_path / policy, tenant_id, item.source_ref.item_id
    )
    importer = ActorRef(target.authority_id, "importer-00000002", "human", tenant_id)
    result = import_sources(
        target,
        archive,
        authorization=ImportAuthorization(
            importer,
            "c" * 64,
            collision_policy=policy,
        ),
    )
    assert result.quarantined_count == expected_quarantine
    assert result.remapped_count == expected_remap
    conn = target.connect()
    try:
        expected_items = 2 if policy == "remap" else 1
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == expected_items
    finally:
        conn.close()
