"""One-time authorization receipts for the private legacy-domain cutover.

Issuance is reachable only through the temporary high-consent MCP operator.
The private cutover runner consumes the resulting data-only receipt.  Actor
identity and the authorization fingerprint are never accepted from the MCP
caller or from the cutover runner command line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Mapping

from work_buddy.sources.cutover import (
    CUTOVER_IMPORT_PURPOSES,
    CutoverSourceAuthorization,
)
from work_buddy.sources.errors import InvalidSourceRequest
from work_buddy.sources.models import ActorRef, canonical_sha256


AUTHORIZATION_SCHEMA = "wb.private-cutover-authorization/v1"
SCOPE_SCHEMA = "wb.private-cutover-scope/v1"
RECEIPT_SCHEMA = "wb.private-cutover-authorization-receipt/v1"
DOMAINS = ("journal", "projects", "personal_knowledge", "contracts")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class CutoverAuthorizationReceiptError(RuntimeError):
    """The one-time cutover authorization is invalid or unavailable."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise CutoverAuthorizationReceiptError("authorization time must be timezone-aware")
    return current.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise CutoverAuthorizationReceiptError("authorization expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CutoverAuthorizationReceiptError("authorization expiry is invalid") from exc
    return _now(parsed)


def normalize_cutover_scope(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("schema") != SCOPE_SCHEMA:
        raise CutoverAuthorizationReceiptError("cutover scope schema is invalid")
    domains = value.get("domains")
    if not isinstance(domains, Mapping) or set(domains) != set(DOMAINS):
        raise CutoverAuthorizationReceiptError("cutover scope domains are invalid")
    normalized: dict[str, dict[str, str]] = {}
    for domain in DOMAINS:
        row = domains.get(domain)
        if not isinstance(row, Mapping):
            raise CutoverAuthorizationReceiptError("cutover domain scope is invalid")
        cohort_id = row.get("cohortId")
        inventory = row.get("inventorySha256")
        if (
            not isinstance(cohort_id, str)
            or _TOKEN.fullmatch(cohort_id) is None
            or not isinstance(inventory, str)
            or _SHA256.fullmatch(inventory) is None
        ):
            raise CutoverAuthorizationReceiptError("cutover domain scope is invalid")
        normalized[domain] = {
            "cohortId": cohort_id,
            "inventorySha256": inventory,
        }
    purposes = tuple(sorted((*CUTOVER_IMPORT_PURPOSES.values(), "export")))
    supplied = value.get("purposes")
    if supplied is not None and tuple(sorted(supplied)) != purposes:
        raise CutoverAuthorizationReceiptError("cutover purposes are invalid")
    return {
        "schema": SCOPE_SCHEMA,
        "domains": normalized,
        "purposes": list(purposes),
    }


def load_cutover_scope(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).expanduser().resolve().read_bytes()
        if len(raw) > 64_000:
            raise ValueError
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, Mapping):
            raise ValueError
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise CutoverAuthorizationReceiptError("cutover scope is unavailable") from exc
    return normalize_cutover_scope(parsed)


def default_cutover_authorization_root() -> Path:
    """Return the only production ledger root accepted by the live operator."""

    from work_buddy.paths import data_dir

    return data_dir("private_cutover_authorizations").expanduser().resolve()


def _connect(root: Path, *, create: bool = True) -> sqlite3.Connection:
    if create:
        root.mkdir(parents=True, exist_ok=True)
    database = (root / "receipts.db").resolve()
    if create:
        connection = sqlite3.connect(database, timeout=10.0)
    else:
        # ``mode=rw`` is deliberate: verification must never manufacture a
        # fresh adjacent ledger for a caller-selected manifest.
        connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    if create:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cutover_authorization_receipts (
                manifest_id TEXT PRIMARY KEY,
                scope_sha256 TEXT NOT NULL UNIQUE,
                manifest_sha256 TEXT NOT NULL,
                authorization_fingerprint TEXT NOT NULL,
                manifest_path TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('issued','consumed')),
                consumption_id TEXT UNIQUE,
                issued_at TEXT NOT NULL,
                consumed_at TEXT
            );
            """
        )
    return connection


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class IssuedCutoverAuthorizationReceipt:
    manifest_id: str
    manifest_path: Path
    manifest_sha256: str
    scope_sha256: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ConsumedCutoverAuthorizationReceipt:
    manifest_id: str
    manifest_sha256: str
    scope_sha256: str
    authorization: CutoverSourceAuthorization
    replayed: bool


def issue_cutover_authorization_receipt(
    *,
    root: str | Path,
    scope: Mapping[str, Any],
    enrolled_human: ActorRef,
    authorization_fingerprint: str,
    consent_request_id: str,
    expires_at: datetime,
    now: datetime | None = None,
) -> IssuedCutoverAuthorizationReceipt:
    """Persist one server-derived authorization after exact high consent."""

    normalized = normalize_cutover_scope(scope)
    current = _now(now)
    expiry = _now(expires_at)
    if expiry <= current or (expiry - current).total_seconds() > 3600:
        raise CutoverAuthorizationReceiptError("cutover authorization expiry is invalid")
    if enrolled_human.kind != "human":
        raise CutoverAuthorizationReceiptError("enrolled cutover inputter is invalid")
    if _SHA256.fullmatch(authorization_fingerprint) is None:
        raise CutoverAuthorizationReceiptError("authorization fingerprint is invalid")
    if not isinstance(consent_request_id, str) or not consent_request_id:
        raise CutoverAuthorizationReceiptError("consent request identity is invalid")

    issuer = ActorRef(
        enrolled_human.issuer_authority_id,
        "work-buddy-obsidian-cutover-issuer",
        "service",
        enrolled_human.tenant_scope_id,
    )
    principal = ActorRef(
        enrolled_human.issuer_authority_id,
        "work-buddy-obsidian-cutover",
        "service",
        enrolled_human.tenant_scope_id,
    )
    scope_sha = _sha(normalized)
    manifest_id = hashlib.sha256(
        f"cutover-authorization\0{scope_sha}\0{authorization_fingerprint}".encode()
    ).hexdigest()[:32]
    payload = {
        "schema": AUTHORIZATION_SCHEMA,
        "receiptSchema": RECEIPT_SCHEMA,
        "manifestId": manifest_id,
        "issuer": issuer.to_dict(),
        "inputter": enrolled_human.to_dict(),
        "principal": principal.to_dict(),
        "tenantScopeId": enrolled_human.tenant_scope_id,
        "authorizationFingerprint": authorization_fingerprint,
        "issuerVersion": "work-buddy-cutover/v1",
        "namespace": "legacy-domain-cutover",
        "sensitivityClass": "private",
        "retentionClass": "durable",
        "inputterAssurance": "enrolled_human_high_consent",
        "consentRequestIdSha256": hashlib.sha256(
            consent_request_id.encode("utf-8")
        ).hexdigest(),
        "scope": normalized,
        "scopeSha256": scope_sha,
        "issuedAt": _timestamp(current),
        "expiresAt": _timestamp(expiry),
    }
    manifest_sha = _sha(payload)
    payload["manifestSha256"] = manifest_sha
    target_root = Path(root).expanduser().resolve()
    manifest_path = target_root / f"authorization-{manifest_id}.json"
    connection = _connect(target_root)
    prior_manifest_path: Path | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM cutover_authorization_receipts WHERE scope_sha256=?",
            (scope_sha,),
        ).fetchone()
        if existing is not None:
            if existing["state"] == "consumed":
                raise CutoverAuthorizationReceiptError(
                    "this cutover scope was already consumed"
                )
            if _parse_time(existing["expires_at"]) > current:
                raise CutoverAuthorizationReceiptError(
                    "an unexpired authorization receipt already exists for this scope"
                )
            prior_manifest_path = Path(str(existing["manifest_path"]))
        _atomic_write(manifest_path, _canonical(payload) + b"\n")
        if existing is None:
            connection.execute(
                "INSERT INTO cutover_authorization_receipts VALUES "
                "(?,?,?,?,?,?,'issued',NULL,?,NULL)",
                (
                    manifest_id,
                    scope_sha,
                    manifest_sha,
                    authorization_fingerprint,
                    str(manifest_path),
                    _timestamp(expiry),
                    _timestamp(current),
                ),
            )
        else:
            connection.execute(
                "UPDATE cutover_authorization_receipts SET manifest_id=?,"
                "manifest_sha256=?,authorization_fingerprint=?,manifest_path=?,"
                "expires_at=?,state='issued',consumption_id=NULL,issued_at=?,"
                "consumed_at=NULL WHERE scope_sha256=? AND state='issued'",
                (
                    manifest_id,
                    manifest_sha,
                    authorization_fingerprint,
                    str(manifest_path),
                    _timestamp(expiry),
                    _timestamp(current),
                    scope_sha,
                ),
            )
        connection.commit()
        if prior_manifest_path is not None and prior_manifest_path != manifest_path:
            prior_manifest_path.unlink(missing_ok=True)
    except Exception:
        connection.rollback()
        manifest_path.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
    return IssuedCutoverAuthorizationReceipt(
        manifest_id=manifest_id,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        scope_sha256=scope_sha,
        expires_at=_timestamp(expiry),
    )


def consume_cutover_authorization_receipt(
    path: str | Path,
    *,
    authorization_root: str | Path,
    expected_scope: Mapping[str, Any],
    consumption_id: str,
    now: datetime | None = None,
) -> ConsumedCutoverAuthorizationReceipt:
    """Consume once; the same deterministic consumption ID may replay."""

    ledger_root = Path(authorization_root).expanduser().resolve()
    manifest_path = Path(path).expanduser().resolve()
    try:
        raw = manifest_path.read_bytes()
        if len(raw) > 64_000:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise CutoverAuthorizationReceiptError(
            "cutover authorization manifest is unavailable"
        ) from exc
    supplied_manifest_sha = payload.pop("manifestSha256", None)
    manifest_sha = _sha(payload)
    if supplied_manifest_sha != manifest_sha:
        raise CutoverAuthorizationReceiptError("cutover authorization manifest was changed")
    if payload.get("schema") != AUTHORIZATION_SCHEMA:
        raise CutoverAuthorizationReceiptError("cutover authorization schema is invalid")
    manifest_id = payload.get("manifestId")
    if not isinstance(manifest_id, str) or _TOKEN.fullmatch(manifest_id) is None:
        raise CutoverAuthorizationReceiptError("cutover authorization identity is invalid")
    if manifest_path != ledger_root / f"authorization-{manifest_id}.json":
        raise CutoverAuthorizationReceiptError(
            "cutover authorization is outside the trusted receipt ledger"
        )
    normalized_scope = normalize_cutover_scope(payload.get("scope") or {})
    expected = normalize_cutover_scope(expected_scope)
    scope_sha = _sha(normalized_scope)
    if normalized_scope != expected or payload.get("scopeSha256") != scope_sha:
        raise CutoverAuthorizationReceiptError("cutover authorization scope changed")
    current = _now(now)
    expiry = _parse_time(payload.get("expiresAt"))
    issued_at = _parse_time(payload.get("issuedAt"))
    if (
        issued_at > current
        or expiry <= issued_at
        or (expiry - issued_at).total_seconds() > 3600
    ):
        raise CutoverAuthorizationReceiptError("cutover authorization lifetime is invalid")
    if not isinstance(consumption_id, str) or _TOKEN.fullmatch(consumption_id) is None:
        raise CutoverAuthorizationReceiptError("cutover consumption identity is invalid")

    try:
        inputter = ActorRef.from_dict(payload["inputter"])
        issuer = ActorRef.from_dict(payload["issuer"])
        principal = ActorRef.from_dict(payload["principal"])
        authorization = CutoverSourceAuthorization(
            issuer=issuer,
            inputter=inputter,
            principal=principal,
            tenant_scope_id=str(payload["tenantScopeId"]),
            authorization_fingerprint=str(payload["authorizationFingerprint"]),
            issuer_version=str(payload["issuerVersion"]),
            namespace=str(payload["namespace"]),
            sensitivity_class=str(payload["sensitivityClass"]),
            retention_class=str(payload["retentionClass"]),
            inputter_assurance=str(payload["inputterAssurance"]),
        )
    except (KeyError, TypeError, ValueError, InvalidSourceRequest) as exc:
        raise CutoverAuthorizationReceiptError(
            "cutover authorization identity is invalid"
        ) from exc
    expected_issuer = ActorRef(
        inputter.issuer_authority_id,
        "work-buddy-obsidian-cutover-issuer",
        "service",
        inputter.tenant_scope_id,
    )
    expected_principal = ActorRef(
        inputter.issuer_authority_id,
        "work-buddy-obsidian-cutover",
        "service",
        inputter.tenant_scope_id,
    )
    if (
        payload.get("receiptSchema") != RECEIPT_SCHEMA
        or inputter.kind != "human"
        or issuer != expected_issuer
        or principal != expected_principal
        or authorization.issuer_version != "work-buddy-cutover/v1"
        or authorization.namespace != "legacy-domain-cutover"
        or authorization.sensitivity_class != "private"
        or authorization.retention_class != "durable"
        or authorization.inputter_assurance != "enrolled_human_high_consent"
        or _SHA256.fullmatch(str(payload.get("consentRequestIdSha256", ""))) is None
    ):
        raise CutoverAuthorizationReceiptError(
            "cutover authorization identity is invalid"
        )

    try:
        connection = _connect(ledger_root, create=False)
    except (OSError, sqlite3.Error) as exc:
        raise CutoverAuthorizationReceiptError(
            "cutover authorization ledger is unavailable"
        ) from exc
    replayed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM cutover_authorization_receipts WHERE manifest_id=?",
            (manifest_id,),
        ).fetchone()
        if (
            row is None
            or str(row["manifest_path"]) != str(manifest_path)
            or str(row["manifest_sha256"]) != manifest_sha
            or str(row["scope_sha256"]) != scope_sha
            or str(row["authorization_fingerprint"])
            != payload.get("authorizationFingerprint")
            or str(row["expires_at"]) != payload.get("expiresAt")
        ):
            raise CutoverAuthorizationReceiptError(
                "cutover authorization ledger does not match the manifest"
            )
        if row["state"] == "consumed":
            if row["consumption_id"] != consumption_id:
                raise CutoverAuthorizationReceiptError(
                    "cutover authorization was already consumed"
                )
            replayed = True
        elif row["state"] == "issued":
            if expiry <= current:
                raise CutoverAuthorizationReceiptError(
                    "cutover authorization expired"
                )
            cursor = connection.execute(
                "UPDATE cutover_authorization_receipts SET state='consumed',"
                "consumption_id=?,consumed_at=? WHERE manifest_id=? AND state='issued'",
                (consumption_id, _timestamp(current), payload["manifestId"]),
            )
            if cursor.rowcount != 1:
                raise CutoverAuthorizationReceiptError(
                    "cutover authorization changed concurrently"
                )
        else:
            raise CutoverAuthorizationReceiptError("cutover authorization state is invalid")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return ConsumedCutoverAuthorizationReceipt(
        manifest_id=str(payload["manifestId"]),
        manifest_sha256=manifest_sha,
        scope_sha256=scope_sha,
        authorization=authorization,
        replayed=replayed,
    )


__all__ = [
    "AUTHORIZATION_SCHEMA",
    "CutoverAuthorizationReceiptError",
    "ConsumedCutoverAuthorizationReceipt",
    "IssuedCutoverAuthorizationReceipt",
    "RECEIPT_SCHEMA",
    "SCOPE_SCHEMA",
    "default_cutover_authorization_root",
    "consume_cutover_authorization_receipt",
    "issue_cutover_authorization_receipt",
    "load_cutover_scope",
    "normalize_cutover_scope",
]
