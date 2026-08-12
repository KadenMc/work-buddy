from __future__ import annotations

import inspect

import pytest

from work_buddy.consent import ConsentRequired, per_invocation_authorization
from work_buddy.mcp_server.ops import source_maintenance_ops as ops
from work_buddy.sources import (
    ActorRef,
    ExportAuthorization,
    SourceStore,
    export_sources,
    source_export_recovery_scope,
    source_export_status,
)


def test_operator_has_no_actor_or_authorization_parameters() -> None:
    parameters = inspect.signature(ops.source_maintenance_operator).parameters
    assert "actor" not in parameters
    assert "authorization_fingerprint" not in parameters
    assert "principal" not in parameters


def test_export_requires_exact_approval_and_uses_server_actor(
    tmp_path, monkeypatch
) -> None:
    store = SourceStore.create(tmp_path / "sources")
    actor = ActorRef(store.authority_id, "local-human-0001", "human", "tenant-local-0001")
    item = store.capture_source(
        content="operator export",
        source_role="human_input",
        tenant_scope_id=actor.tenant_scope_id,
        originating_surface="test",
    )
    monkeypatch.setattr(ops, "_store", lambda: store)
    monkeypatch.setattr(ops, "_human", lambda: actor)
    destination = tmp_path / "sources.jsonl"
    kwargs = {
        "action": "export",
        "source_refs": [item.source_ref.uri],
        "destination": str(destination),
    }
    with pytest.raises(ConsentRequired):
        ops.source_maintenance_operator(**kwargs)
    assert not destination.exists()

    context = {
        "source_refs": [item.source_ref.uri],
        "destination": str(destination.resolve()),
        "source_path": None,
        "export_id": None,
        "include_content": True,
        "collision_policy": "quarantine",
        "reason_code": "user_requested",
    }
    prompt = ops._prompt("export", context)
    with per_invocation_authorization(
        "sources.maintenance",
        prompt.fingerprint,
        request_id="consent-request-1",
        response_surface="test",
        context=prompt.context,
    ):
        result = ops.source_maintenance_operator(**kwargs)
    assert result["itemCount"] == 1
    assert result["issuedCopyCount"] == 1
    assert destination.is_file()


def test_recovery_approval_binds_frozen_export_scope_and_is_audited(
    tmp_path, monkeypatch
) -> None:
    import work_buddy.sources.export as export_module

    store = SourceStore.create(tmp_path / "sources")
    actor = ActorRef(store.authority_id, "local-human-0001", "human", "tenant-local-0001")
    item = store.capture_source(
        content="interrupted exact export",
        source_role="human_input",
        tenant_scope_id=actor.tenant_scope_id,
        originating_surface="test",
    )
    store.grant_access(
        source_ref=item.source_ref,
        principal=actor,
        purpose="export",
        access_mode="content",
        authorization_fingerprint="a" * 64,
    )
    destination = tmp_path / "interrupted.jsonl"
    real_write = export_module.atomic_write_bytes
    monkeypatch.setattr(
        export_module,
        "atomic_write_bytes",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("stop before write")),
    )
    with pytest.raises(RuntimeError, match="stop before write"):
        export_sources(
            store,
            destination,
            authorization=ExportAuthorization(actor, "a" * 64),
            source_refs=[item.source_ref],
            idempotency_key="operator-recovery",
        )
    export_id = source_export_status(store)[0]["export_id"]
    scope = source_export_recovery_scope(store, export_id)
    assert scope == {
        "export_id": export_id,
        "request_sha256": scope["request_sha256"],
        "destination": str(destination.resolve()),
        "include_content": True,
        "source_refs": [item.source_ref.uri],
        "state": "prepared",
        "payload_sha256": scope["payload_sha256"],
        "item_count": 1,
    }
    monkeypatch.setattr(export_module, "atomic_write_bytes", real_write)
    monkeypatch.setattr(ops, "_store", lambda: store)
    monkeypatch.setattr(ops, "_human", lambda: actor)
    prompt = ops._prompt("recover_export", {"export_scope": scope})
    with per_invocation_authorization(
        "sources.maintenance",
        prompt.fingerprint,
        request_id="consent-recover-exact-scope",
        response_surface="test",
        context=prompt.context,
    ):
        result = ops.source_maintenance_operator(
            action="recover_export",
            export_id=export_id,
        )
    assert result["sha256"] == scope["payload_sha256"]
    conn = store.connect()
    try:
        receipt = conn.execute(
            "SELECT action,authorization_fingerprint,authorization_request_id "
            "FROM source_export_operator_authorizations WHERE export_id=?",
            (export_id,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(receipt) == (
        "recover_export",
        prompt.fingerprint,
        "consent-recover-exact-scope",
    )


def test_paused_effect_recovery_is_exact_high_consent_and_bounded(
    tmp_path, monkeypatch
) -> None:
    from work_buddy.sources.models import canonical_sha256

    store = SourceStore.create(tmp_path / "sources")
    actor = ActorRef(store.authority_id, "local-human-0001", "human", "tenant-local-0001")
    payload = {"usage_id": "usage-recovery-0001"}
    payload_sha = canonical_sha256(payload)
    with store.write_transaction() as conn:
        conn.execute(
            "INSERT INTO source_outbox "
            "(effect_id,command_id,target_domain,effect_type,payload_json,payload_sha256,"
            "authorization_fingerprint,status,error_code,created_at,updated_at) "
            "VALUES ('effect-recovery-0001',NULL,'journal','source.redaction',?,?,'a',"
            "'paused','imported_inert','2026-01-01','2026-01-01')",
            ('{"usage_id":"usage-recovery-0001"}', payload_sha),
        )
    monkeypatch.setattr(ops, "_store", lambda: store)
    monkeypatch.setattr(ops, "_human", lambda: actor)

    with pytest.raises(ConsentRequired):
        ops.source_maintenance_operator(
            action="recover_effect",
            effect_id="effect-recovery-0001",
        )
    context = {
        "effect_id": "effect-recovery-0001",
        "target_domain": "journal",
        "effect_type": "source.redaction",
        "payload_sha256": payload_sha,
        "status": "paused",
        "error_code": "imported_inert",
    }
    prompt = ops._prompt("recover_effect", context)
    with per_invocation_authorization(
        "sources.maintenance",
        prompt.fingerprint,
        request_id="consent-effect-recovery",
        response_surface="test",
        context=prompt.context,
    ):
        result = ops.source_maintenance_operator(
            action="recover_effect",
            effect_id="effect-recovery-0001",
        )
    assert result == {
        "schema": "wb.sources-effect-recovery-result/v1",
        "effectId": "effect-recovery-0001",
        "targetDomain": "journal",
        "effectType": "source.redaction",
        "payloadSha256": payload_sha,
        "status": "pending",
    }
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT authorization_fingerprint,authorization_expires_at,status,error_code "
            "FROM source_outbox WHERE effect_id='effect-recovery-0001'"
        ).fetchone()
    finally:
        conn.close()
    assert row["authorization_fingerprint"] == prompt.fingerprint
    assert row["authorization_expires_at"] is not None
    assert tuple(row[key] for key in ("status", "error_code")) == ("pending", None)
