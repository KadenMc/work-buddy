"""Authorized operator boundary for Sources portability and redaction."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from work_buddy.consent import (
    ConsentPrompt,
    current_per_invocation_authorization,
    requires_consent,
)
from work_buddy.mcp_server.op_registry import register_op
from work_buddy.sources import (
    ExportAuthorization,
    ImportAuthorization,
    SourceRef,
    SourceOutbox,
    SourceStore,
    abort_source_export,
    export_sources,
    import_sources,
    record_source_export_operator_authorization,
    recover_source_export,
    redact_source,
    source_export_recovery_scope,
    source_export_status,
)
from work_buddy.sources.models import ActorRef, canonical_sha256


_MUTATIONS = frozenset(
    {"export", "recover_export", "abort_export", "import", "redact", "recover_effect"}
)


def _prompt(action: str, context: Mapping[str, Any]) -> ConsentPrompt:
    fingerprint = canonical_sha256(
        {
            "schema": "wb.sources-maintenance-authorization/v1",
            "action": action,
            "context": dict(context),
        }
    )
    descriptions = {
        "export": "Create the displayed offline Sources archive and record every issued copy.",
        "recover_export": "Resume the displayed interrupted Sources export without changing its scope.",
        "abort_export": "Cancel the displayed prepared export only if no destination copy exists.",
        "import": "Import the displayed Sources archive under foreign-source quarantine rules.",
        "redact": "Remove readable content for the displayed Source and notify managed copies.",
        "recover_effect": "Reauthorize the displayed paused Sources effect for domain delivery.",
    }
    return ConsentPrompt(
        body=descriptions.get(action, "Run the displayed Sources maintenance action."),
        fingerprint=fingerprint,
        context={"action": action, **dict(context)},
    )


@requires_consent(
    "sources.maintenance",
    "Export, import, recover, or redact the exact displayed Sources scope.",
    risk="high",
    consent_weight="high",
    grant_policy="per_invocation",
    request_factory=_prompt,
)
def _authorize(action: str, context: Mapping[str, Any]):
    authorization = current_per_invocation_authorization()
    if authorization is None:
        raise RuntimeError("sources_maintenance_authorization_missing")
    return authorization


def _store() -> SourceStore:
    from work_buddy.paths import resolve

    return SourceStore.create(resolve("stores/sources"))


def _human() -> ActorRef:
    from work_buddy.dashboard import local_identity_api

    return ActorRef.from_dict(local_identity_api._authority().enrolled_actor().to_dict())


def _refs(store: SourceStore, values: Sequence[str] | None) -> tuple[SourceRef, ...]:
    if values is not None:
        return tuple(SourceRef.parse(value) for value in values)
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT authority_id,source_item_id FROM source_items "
            "ORDER BY authority_id,source_item_id"
        ).fetchall()
    finally:
        conn.close()
    return tuple(SourceRef(str(row[0]), str(row[1])) for row in rows)


def _ensure_access(
    store: SourceStore,
    *,
    refs: Sequence[SourceRef],
    principal: ActorRef,
    purpose: str,
    access_mode: str,
    fingerprint: str,
    request_id: str,
) -> None:
    principal_json = canonical_sha256(principal.to_dict())
    for ref in refs:
        binding_id = hashlib.sha256(
            f"source-maintenance:{ref.uri}:{purpose}:{fingerprint}".encode("utf-8")
        ).hexdigest()[:32]
        conn = store.connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM source_access_bindings WHERE binding_id=? AND revoked_at IS NULL",
                (binding_id,),
            ).fetchone()
        finally:
            conn.close()
        if exists is not None:
            continue
        store.grant_access(
            source_ref=ref,
            principal=principal,
            purpose=purpose,
            access_mode=access_mode,
            authorization_fingerprint=fingerprint,
            scope={"source_ref": ref.uri, "principal_sha256": principal_json},
            trusted_service_id="work-buddy-sources-maintenance",
            gesture_receipt_id=request_id,
            binding_id=binding_id,
        )


def source_maintenance_operator(
    action: str,
    source_refs: Sequence[str] | None = None,
    destination: str | None = None,
    source_path: str | None = None,
    export_id: str | None = None,
    include_content: bool = True,
    collision_policy: str = "quarantine",
    reason_code: str = "user_requested",
    effect_id: str | None = None,
) -> dict[str, Any]:
    """Preview or execute one source maintenance operation without actor inputs."""

    store = _store()
    refs = (
        _refs(store, source_refs)
        if action in {"preview", "export", "redact"}
        else ()
    )
    if action == "preview":
        return {
            "schema": "wb.sources-maintenance-preview/v1",
            "sourceCount": len(refs),
            "sourceRefs": [ref.uri for ref in refs],
            "includeContent": bool(include_content),
            "destination": destination,
            "warning": "Content exports create issued offline copies that cannot be recalled automatically.",
        }
    if action == "status":
        return {
            "schema": "wb.sources-maintenance-status/v1",
            "exports": list(source_export_status(store, export_id)),
        }
    if action == "effects":
        effects = SourceOutbox(store).list(limit=1000)
        return {
            "schema": "wb.sources-effects-status/v1",
            "effects": [
                {
                    "effectId": effect.effect_id,
                    "targetDomain": effect.target_domain,
                    "effectType": effect.effect_type,
                    "payloadSha256": effect.payload_sha256,
                    "status": effect.status,
                    "errorCode": effect.error_code,
                }
                for effect in effects
            ],
        }
    if action not in _MUTATIONS:
        raise ValueError("unsupported Sources maintenance action")

    recovery_scope = None
    if action == "recover_effect":
        if not effect_id:
            raise ValueError("recover_effect requires effect_id")
        effect = SourceOutbox(store).get(effect_id)
        if effect is None:
            raise ValueError("Sources effect was not found")
        context = {
            "effect_id": effect.effect_id,
            "target_domain": effect.target_domain,
            "effect_type": effect.effect_type,
            "payload_sha256": effect.payload_sha256,
            "status": effect.status,
            "error_code": effect.error_code,
        }
    elif action in {"recover_export", "abort_export"}:
        if export_id is None:
            raise ValueError(f"{action} requires export_id")
        recovery_scope = source_export_recovery_scope(store, export_id)
        context = {"export_scope": recovery_scope}
    else:
        context = {
            "source_refs": [ref.uri for ref in refs],
            "destination": str(Path(destination).expanduser().resolve()) if destination else None,
            "source_path": str(Path(source_path).expanduser().resolve()) if source_path else None,
            "export_id": export_id,
            "include_content": bool(include_content),
            "collision_policy": collision_policy,
            "reason_code": reason_code,
        }
    approved = _authorize(action, context)
    human = _human()

    if action == "recover_effect":
        assert effect_id is not None
        recovered = SourceOutbox(store).reauthorize(
            effect_id,
            authorization_fingerprint=approved.fingerprint,
            authorization_expires_at=(
                datetime.now(UTC) + timedelta(minutes=15)
            ).isoformat(timespec="milliseconds"),
        )
        return {
            "schema": "wb.sources-effect-recovery-result/v1",
            "effectId": recovered.effect_id,
            "targetDomain": recovered.target_domain,
            "effectType": recovered.effect_type,
            "payloadSha256": recovered.payload_sha256,
            "status": recovered.status,
        }

    if action == "export":
        if destination is None:
            raise ValueError("export requires destination")
        _ensure_access(
            store,
            refs=refs,
            principal=human,
            purpose="export",
            access_mode="content" if include_content else "metadata",
            fingerprint=approved.fingerprint,
            request_id=approved.request_id,
        )
        result = export_sources(
            store,
            destination,
            authorization=ExportAuthorization(
                human,
                approved.fingerprint,
                include_content=bool(include_content),
            ),
            source_refs=refs,
            idempotency_key=approved.fingerprint,
        )
        return {
            "schema": "wb.sources-export-result/v1",
            "exportId": result.export_id,
            "path": str(result.path),
            "sha256": result.sha256,
            "itemCount": result.item_count,
            "issuedCopyCount": len(result.usage_ids),
        }
    if action == "recover_export":
        assert export_id is not None and recovery_scope is not None
        record_source_export_operator_authorization(
            store,
            export_id=export_id,
            action=action,
            authorization_fingerprint=approved.fingerprint,
            authorization_request_id=approved.request_id,
            approved_scope=recovery_scope,
        )
        result = recover_source_export(
            store,
            export_id,
            authorization=ExportAuthorization(human, approved.fingerprint),
        )
        return {
            "schema": "wb.sources-export-result/v1",
            "exportId": result.export_id,
            "path": str(result.path),
            "sha256": result.sha256,
            "itemCount": result.item_count,
            "issuedCopyCount": len(result.usage_ids),
        }
    if action == "abort_export":
        assert export_id is not None and recovery_scope is not None
        record_source_export_operator_authorization(
            store,
            export_id=export_id,
            action=action,
            authorization_fingerprint=approved.fingerprint,
            authorization_request_id=approved.request_id,
            approved_scope=recovery_scope,
        )
        return {
            "schema": "wb.sources-export-abort-result/v1",
            "export": abort_source_export(
                store,
                export_id,
                authorization=ExportAuthorization(human, approved.fingerprint),
            ),
        }
    if action == "import":
        if source_path is None:
            raise ValueError("import requires source_path")
        result = import_sources(
            store,
            source_path,
            authorization=ImportAuthorization(
                human,
                approved.fingerprint,
                collision_policy=collision_policy,
            ),
        )
        return {
            "schema": "wb.sources-import-result/v1",
            "importId": result.import_id,
            "itemCount": result.item_count,
            "reusedCount": result.reused_count,
            "remappedCount": result.remapped_count,
            "quarantinedCount": result.quarantined_count,
        }
    if len(refs) != 1:
        raise ValueError("redact requires exactly one source_ref")
    _ensure_access(
        store,
        refs=refs,
        principal=human,
        purpose="redaction",
        access_mode="metadata",
        fingerprint=approved.fingerprint,
        request_id=approved.request_id,
    )
    result = redact_source(
        store,
        source_ref=refs[0],
        actor=human,
        authorization_fingerprint=approved.fingerprint,
        reason_code=reason_code,
    )
    return {
        "schema": "wb.sources-redaction-result/v1",
        "sourceRef": result.source_ref.uri,
        "redactionEventId": result.redaction_event_id,
        "managedCopyState": result.managed_copy_state,
        "issuedCopyState": result.issued_copy_state,
        "pendingEffectCount": len(result.pending_effect_ids),
    }


register_op(
    "op.wb.source_maintenance_operator",
    source_maintenance_operator,
    replace=True,
)


__all__ = ["source_maintenance_operator"]
