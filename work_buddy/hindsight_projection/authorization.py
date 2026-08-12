"""Durable, narrowly bound authorization for background Truth projection."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from work_buddy.hindsight_projection.contracts import (
    ProjectionValidationError,
    canonical_json,
    canonical_sha256,
    utc_now,
)
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore


PURPOSE = "truth_hindsight_projection"


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionValidationError("authorization timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ProjectionValidationError("authorization timestamp requires a timezone")
    return parsed.astimezone(timezone.utc)


def _required(value: str, label: str, *, maximum: int = 512) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ProjectionValidationError(f"{label} is invalid")
    return normalized


def _kinds(value: Sequence[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    result = tuple(sorted({_required(item, "eligible claim kind", maximum=128) for item in value}))
    return result


@dataclass(frozen=True, slots=True)
class ProjectionAuthorization:
    authorization_ref: str
    store_id: str
    policy_id: str
    recipient: str
    provider_id: str
    model_id: str
    eligible_claim_kinds: tuple[str, ...] | None
    projection_method: str
    granted_by_ref: str
    basis: str
    canonical_sha256: str
    granted_at: str
    expires_at: str
    revoked_at: str | None


def grant_projection_authorization(
    store: TruthHindsightProjectionStore,
    *,
    store_id: str,
    policy_id: str,
    recipient: str,
    provider_id: str,
    model_id: str,
    eligible_claim_kinds: Sequence[str] | None,
    projection_method: str,
    granted_by_ref: str,
    basis: str,
    expires_at: str,
    authorization_ref: str | None = None,
    granted_at: str | None = None,
) -> ProjectionAuthorization:
    """Mint one immutable authorization after an external high-consent gate."""

    now = granted_at or utc_now()
    start = _time(now)
    expiry = _time(expires_at)
    if expiry <= start or expiry > start + timedelta(days=365):
        raise ProjectionValidationError(
            "projection authorization expiry must be within one year"
        )
    ref = authorization_ref or f"hpa-{uuid.uuid4().hex}"
    normalized_kinds = _kinds(eligible_claim_kinds)
    payload = {
        "schema": "wb.truth-hindsight-projection-authorization/v1",
        "authorization_ref": _required(ref, "authorization_ref"),
        "store_id": _required(store_id, "store_id", maximum=256),
        "purpose": PURPOSE,
        "policy_id": _required(policy_id, "policy_id", maximum=256),
        "recipient": _required(recipient, "recipient"),
        "provider_id": _required(provider_id, "provider_id", maximum=256),
        "model_id": _required(model_id, "model_id", maximum=256),
        "eligible_claim_kinds": normalized_kinds,
        "projection_method": _required(
            projection_method, "projection_method", maximum=256
        ),
        "granted_by_ref": _required(granted_by_ref, "granted_by_ref"),
        "basis": _required(basis, "basis", maximum=256),
        "granted_at": now,
        "expires_at": expires_at,
    }
    digest = canonical_sha256(payload)
    with store.write_transaction() as conn:
        prior = conn.execute(
            "SELECT * FROM truth_hindsight_projection_authorizations "
            "WHERE authorization_ref=?",
            (payload["authorization_ref"],),
        ).fetchone()
        if prior is None:
            conn.execute(
                "INSERT INTO truth_hindsight_projection_authorizations "
                "(authorization_ref,store_id,purpose,policy_id,recipient,provider_id,"
                "model_id,eligible_claim_kinds_json,projection_method,granted_by_ref,"
                "basis,canonical_sha256,granted_at,expires_at,revoked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    payload["authorization_ref"],
                    payload["store_id"],
                    PURPOSE,
                    payload["policy_id"],
                    payload["recipient"],
                    payload["provider_id"],
                    payload["model_id"],
                    canonical_json(normalized_kinds),
                    payload["projection_method"],
                    payload["granted_by_ref"],
                    payload["basis"],
                    digest,
                    now,
                    expires_at,
                ),
            )
            prior = conn.execute(
                "SELECT * FROM truth_hindsight_projection_authorizations "
                "WHERE authorization_ref=?",
                (payload["authorization_ref"],),
            ).fetchone()
        elif str(prior["canonical_sha256"]) != digest:
            raise ProjectionValidationError(
                "projection authorization identity conflicts with another grant"
            )
    assert prior is not None
    return _from_row(prior)


def revoke_projection_authorization(
    store: TruthHindsightProjectionStore,
    authorization_ref: str,
    *,
    revoked_at: str | None = None,
) -> ProjectionAuthorization:
    at = revoked_at or utc_now()
    _time(at)
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE truth_hindsight_projection_authorizations SET revoked_at=? "
            "WHERE authorization_ref=? AND revoked_at IS NULL",
            (at, _required(authorization_ref, "authorization_ref")),
        )
        row = conn.execute(
            "SELECT * FROM truth_hindsight_projection_authorizations "
            "WHERE authorization_ref=?",
            (authorization_ref,),
        ).fetchone()
    if row is None:
        raise ProjectionValidationError("projection authorization does not exist")
    return _from_row(row)


def projection_authorization(
    store: TruthHindsightProjectionStore, authorization_ref: str
) -> ProjectionAuthorization | None:
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM truth_hindsight_projection_authorizations "
            "WHERE authorization_ref=?",
            (_required(authorization_ref, "authorization_ref"),),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _from_row(row)


def require_active_projection_authorization(
    store: TruthHindsightProjectionStore,
    *,
    authorization_ref: str,
    store_id: str,
    policy_id: str,
    recipient: str,
    provider_id: str,
    model_id: str,
    eligible_claim_kinds: Sequence[str] | None,
    projection_method: str,
    at: str | None = None,
) -> ProjectionAuthorization:
    authorization = projection_authorization(store, authorization_ref)
    if authorization is None:
        raise ProjectionValidationError("projection authorization is not recorded")
    moment = _time(at or utc_now())
    if authorization.revoked_at is not None or _time(authorization.expires_at) <= moment:
        raise ProjectionValidationError("projection authorization is expired or revoked")
    expected = (
        _required(store_id, "store_id", maximum=256),
        _required(policy_id, "policy_id", maximum=256),
        _required(recipient, "recipient"),
        _required(provider_id, "provider_id", maximum=256),
        _required(model_id, "model_id", maximum=256),
        _kinds(eligible_claim_kinds),
        _required(projection_method, "projection_method", maximum=256),
    )
    actual = (
        authorization.store_id,
        authorization.policy_id,
        authorization.recipient,
        authorization.provider_id,
        authorization.model_id,
        authorization.eligible_claim_kinds,
        authorization.projection_method,
    )
    if actual != expected:
        raise ProjectionValidationError(
            "projection authorization does not match this store, policy, or recipient"
        )
    return authorization


def _from_row(row) -> ProjectionAuthorization:
    raw_kinds = json.loads(str(row["eligible_claim_kinds_json"]))
    kinds = None if raw_kinds is None else tuple(str(value) for value in raw_kinds)
    return ProjectionAuthorization(
        authorization_ref=str(row["authorization_ref"]),
        store_id=str(row["store_id"]),
        policy_id=str(row["policy_id"]),
        recipient=str(row["recipient"]),
        provider_id=str(row["provider_id"]),
        model_id=str(row["model_id"]),
        eligible_claim_kinds=kinds,
        projection_method=str(row["projection_method"]),
        granted_by_ref=str(row["granted_by_ref"]),
        basis=str(row["basis"]),
        canonical_sha256=str(row["canonical_sha256"]),
        granted_at=str(row["granted_at"]),
        expires_at=str(row["expires_at"]),
        revoked_at=(None if row["revoked_at"] is None else str(row["revoked_at"])),
    )


__all__ = [
    "ProjectionAuthorization",
    "grant_projection_authorization",
    "projection_authorization",
    "require_active_projection_authorization",
    "revoke_projection_authorization",
]
