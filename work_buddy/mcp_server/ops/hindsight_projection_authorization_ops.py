"""Explicit operator boundary for durable Truth-to-Hindsight egress grants."""

from __future__ import annotations

from typing import Any

from work_buddy.consent import requires_consent
from work_buddy.mcp_server.op_registry import register_op


@requires_consent(
    "truth.hindsight_projection_authorization_change",
    "Grant or revoke bounded background disclosure from confirmed Truth into Hindsight.",
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def _authorize_change() -> None:
    return None


def truth_hindsight_projection_authorization(
    action: str,
    store_id: str,
    authorization_ref: str | None = None,
    policy_id: str = "confirmed_current_v1",
    recipient: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    eligible_claim_kinds: list[str] | None = None,
    projection_method: str = "hindsight_llm_retain_v1",
    expires_at: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Preview, grant, inspect, or revoke one exact background authorization."""

    from work_buddy.hindsight_projection.authorization import (
        grant_projection_authorization,
        projection_authorization,
        revoke_projection_authorization,
    )
    from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore
    from work_buddy.truth.registry import TruthStoreRegistry

    truth_store = TruthStoreRegistry().open_store(store_id)
    store = TruthHindsightProjectionStore(truth_store.paths.db)
    if action == "preview":
        return {
            "store_id": store_id,
            "purpose": "truth_hindsight_projection",
            "requires": [
                "recipient",
                "provider_id",
                "model_id",
                "expires_at_within_one_year",
            ],
            "does_not_authorize": [
                "Truth mutation",
                "arbitrary source content",
                "a different provider, model, recipient, or policy",
            ],
        }
    if action == "status":
        if authorization_ref is None:
            raise ValueError("status requires authorization_ref")
        record = projection_authorization(store, authorization_ref)
        return {"authorization": None if record is None else _view(record)}
    if action == "grant":
        _authorize_change()
        if None in {recipient, provider_id, model_id, expires_at}:
            raise ValueError(
                "grant requires recipient, provider_id, model_id, and expires_at"
            )
        record = grant_projection_authorization(
            store,
            store_id=store_id,
            policy_id=policy_id,
            recipient=str(recipient),
            provider_id=str(provider_id),
            model_id=str(model_id),
            eligible_claim_kinds=eligible_claim_kinds,
            projection_method=projection_method,
            granted_by_ref=f"work-buddy-consent:{agent_session_id or 'local-session'}",
            basis="high_consent_capability",
            expires_at=str(expires_at),
            authorization_ref=authorization_ref,
        )
        return {
            "authorization": _view(record),
            "config_binding": {
                "enabled": True,
                "authorization_ref": record.authorization_ref,
                "policy_id": record.policy_id,
                "recipient": record.recipient,
                "provider_id": record.provider_id,
                "model_id": record.model_id,
                "eligible_claim_kinds": record.eligible_claim_kinds,
                "projection_method": record.projection_method,
            },
        }
    if action == "revoke":
        _authorize_change()
        if authorization_ref is None:
            raise ValueError("revoke requires authorization_ref")
        return {"authorization": _view(revoke_projection_authorization(store, authorization_ref))}
    raise ValueError("action must be preview, status, grant, or revoke")


def _view(record) -> dict[str, Any]:
    return {
        "authorization_ref": record.authorization_ref,
        "store_id": record.store_id,
        "policy_id": record.policy_id,
        "recipient": record.recipient,
        "provider_id": record.provider_id,
        "model_id": record.model_id,
        "eligible_claim_kinds": record.eligible_claim_kinds,
        "projection_method": record.projection_method,
        "basis": record.basis,
        "granted_at": record.granted_at,
        "expires_at": record.expires_at,
        "revoked_at": record.revoked_at,
    }


register_op(
    "op.wb.truth_hindsight_projection_authorization",
    truth_hindsight_projection_authorization,
    replace=True,
)


__all__ = ["truth_hindsight_projection_authorization"]
