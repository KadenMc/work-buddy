"""Leased, restart-safe delivery for the source-owned outbox."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from work_buddy.sources.errors import (
    InvalidSourceRequest,
    SourceLeaseConflict,
    SourceNotFound,
)
from work_buddy.sources.models import (
    EffectReceipt,
    OutboxEffect,
    canonical_json,
    new_id,
    utc_now,
    validate_sha256,
)
from work_buddy.sources.store import SourceStore


_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")


def _lease_until(now: str, seconds: int) -> str:
    if seconds < 1 or seconds > 3600:
        raise InvalidSourceRequest()
    try:
        parsed = datetime.fromisoformat(now)
    except ValueError as exc:
        raise InvalidSourceRequest() from exc
    if parsed.tzinfo is None:
        raise InvalidSourceRequest()
    return (parsed.astimezone(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )


def _effect_from_row(row: Any) -> OutboxEffect:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise InvalidSourceRequest()
    return OutboxEffect(
        effect_id=str(row["effect_id"]),
        command_id=row["command_id"],
        target_domain=str(row["target_domain"]),
        effect_type=str(row["effect_type"]),
        payload=payload,
        payload_sha256=str(row["payload_sha256"]),
        authorization_fingerprint=str(row["authorization_fingerprint"]),
        authorization_expires_at=row["authorization_expires_at"],
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        lease_owner=row["lease_owner"],
        lease_until=row["lease_until"],
        result_ref=row["result_ref"],
        error_code=row["error_code"],
    )


class SourceOutbox:
    def __init__(self, store: SourceStore) -> None:
        self.store = store

    def get(self, effect_id: str) -> OutboxEffect | None:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            return _effect_from_row(row) if row else None
        finally:
            conn.close()

    def lease(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 60,
        at: str | None = None,
        target_domain: str | None = None,
        effect_type: str | None = None,
    ) -> tuple[OutboxEffect, ...]:
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 256:
            raise InvalidSourceRequest()
        if not (1 <= limit <= 100):
            raise InvalidSourceRequest()
        now = at or utc_now()
        until = _lease_until(now, lease_seconds)
        with self.store.write_transaction() as conn:
            conn.execute(
                "UPDATE source_outbox SET status = 'retryable', lease_owner = NULL, "
                "lease_until = NULL, updated_at = ? "
                "WHERE status = 'leased' AND lease_until <= ?",
                (now, now),
            )
            conn.execute(
                "UPDATE source_outbox SET status = 'paused', error_code = "
                "'authorization_expired', updated_at = ? "
                "WHERE status IN ('pending','retryable') "
                "AND authorization_expires_at IS NOT NULL AND authorization_expires_at <= ?",
                (now, now),
            )
            clauses = ["status IN ('pending','retryable')"]
            params: list[object] = []
            if target_domain is not None:
                clauses.append("target_domain = ?")
                params.append(target_domain)
            if effect_type is not None:
                clauses.append("effect_type = ?")
                params.append(effect_type)
            params.append(limit)
            rows = conn.execute(
                "SELECT effect_id FROM source_outbox WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, effect_id LIMIT ?",
                params,
            ).fetchall()
            ids = [str(row["effect_id"]) for row in rows]
            for effect_id in ids:
                conn.execute(
                    "UPDATE source_outbox SET status = 'leased', lease_owner = ?, "
                    "lease_until = ?, attempts = attempts + 1, error_code = NULL, "
                    "updated_at = ? WHERE effect_id = ?",
                    (worker_id, until, now, effect_id),
                )
            if not ids:
                return ()
            leased = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id IN ("
                + ",".join("?" for _ in ids)
                + ") ORDER BY created_at, effect_id",
                ids,
            ).fetchall()
            return tuple(_effect_from_row(row) for row in leased)

    def lease_exact(
        self,
        effect_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        at: str | None = None,
    ) -> OutboxEffect | None:
        """Lease one known effect without consuming another domain's work."""

        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 256:
            raise InvalidSourceRequest()
        now = at or utc_now()
        until = _lease_until(now, lease_seconds)
        with self.store.write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                raise SourceNotFound()
            if row["status"] == "leased" and row["lease_until"] <= now:
                conn.execute(
                    "UPDATE source_outbox SET status = 'retryable', lease_owner = NULL, "
                    "lease_until = NULL, updated_at = ? WHERE effect_id = ?",
                    (now, effect_id),
                )
                row = conn.execute(
                    "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
                ).fetchone()
            if row["status"] == "leased":
                if row["lease_owner"] != worker_id:
                    raise SourceLeaseConflict()
                return _effect_from_row(row)
            if row["status"] not in {"pending", "retryable"}:
                return None
            if (
                row["authorization_expires_at"] is not None
                and row["authorization_expires_at"] <= now
            ):
                conn.execute(
                    "UPDATE source_outbox SET status = 'paused', "
                    "error_code = 'authorization_expired', updated_at = ? WHERE effect_id = ?",
                    (now, effect_id),
                )
                return None
            conn.execute(
                "UPDATE source_outbox SET status = 'leased', lease_owner = ?, "
                "lease_until = ?, attempts = attempts + 1, error_code = NULL, "
                "updated_at = ? WHERE effect_id = ?",
                (worker_id, until, now, effect_id),
            )
            updated = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            assert updated is not None
            return _effect_from_row(updated)

    def list(
        self,
        *,
        status: str | None = None,
        target_domain: str | None = None,
        effect_type: str | None = None,
        limit: int = 100,
    ) -> tuple[OutboxEffect, ...]:
        if not (1 <= limit <= 1000):
            raise InvalidSourceRequest()
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("status", status),
            ("target_domain", target_domain),
            ("effect_type", effect_type),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT * FROM source_outbox"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, effect_id LIMIT ?"
        params.append(limit)
        conn = self.store.connect()
        try:
            return tuple(_effect_from_row(row) for row in conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    def renew(
        self,
        effect_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        at: str | None = None,
    ) -> OutboxEffect:
        now = at or utc_now()
        until = _lease_until(now, lease_seconds)
        with self.store.write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "leased"
                or row["lease_owner"] != worker_id
                or row["lease_until"] <= now
            ):
                raise SourceLeaseConflict()
            conn.execute(
                "UPDATE source_outbox SET lease_until = ?, updated_at = ? WHERE effect_id = ?",
                (until, now, effect_id),
            )
            updated = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            assert updated is not None
            return _effect_from_row(updated)

    def complete(
        self,
        effect_id: str,
        worker_id: str,
        *,
        result_ref: str,
        result_sha256: str,
        at: str | None = None,
    ) -> EffectReceipt:
        if (
            not isinstance(result_ref, str)
            or not result_ref
            or len(result_ref) > 512
            or any(ord(ch) < 0x20 for ch in result_ref)
        ):
            raise InvalidSourceRequest()
        validate_sha256(result_sha256)
        now = at or utc_now()
        with self.store.write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                raise SourceNotFound()
            if row["status"] == "succeeded":
                if row["result_ref"] != result_ref or row["result_sha256"] != result_sha256:
                    raise SourceLeaseConflict()
                receipt = conn.execute(
                    "SELECT * FROM source_effect_receipts WHERE effect_id = ?", (effect_id,)
                ).fetchone()
                assert receipt is not None
                return self._receipt_from_row(receipt)
            if (
                row["status"] != "leased"
                or row["lease_owner"] != worker_id
                or row["lease_until"] <= now
            ):
                raise SourceLeaseConflict()
            receipt_id = new_id()
            conn.execute(
                "INSERT INTO source_effect_receipts "
                "(receipt_id, effect_id, target_domain, result_ref, result_sha256, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    effect_id,
                    row["target_domain"],
                    result_ref,
                    result_sha256,
                    now,
                ),
            )
            conn.execute(
                "UPDATE source_outbox SET status = 'succeeded', lease_owner = NULL, "
                "lease_until = NULL, result_ref = ?, result_sha256 = ?, error_code = NULL, "
                "updated_at = ? WHERE effect_id = ?",
                (result_ref, result_sha256, now, effect_id),
            )
            return EffectReceipt(
                receipt_id=receipt_id,
                effect_id=effect_id,
                target_domain=str(row["target_domain"]),
                result_ref=result_ref,
                result_sha256=result_sha256,
                received_at=now,
            )

    def fail(
        self,
        effect_id: str,
        worker_id: str,
        *,
        error_code: str,
        retryable: bool,
        at: str | None = None,
    ) -> OutboxEffect:
        if not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code):
            raise InvalidSourceRequest()
        now = at or utc_now()
        with self.store.write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "leased"
                or row["lease_owner"] != worker_id
                or row["lease_until"] <= now
            ):
                raise SourceLeaseConflict()
            status = "retryable" if retryable else "failed_terminal"
            conn.execute(
                "UPDATE source_outbox SET status = ?, lease_owner = NULL, lease_until = NULL, "
                "error_code = ?, updated_at = ? WHERE effect_id = ?",
                (status, error_code, now, effect_id),
            )
            updated = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            assert updated is not None
            return _effect_from_row(updated)

    def reauthorize(
        self,
        effect_id: str,
        *,
        authorization_fingerprint: str,
        authorization_expires_at: str | None,
        at: str | None = None,
    ) -> OutboxEffect:
        validate_sha256(authorization_fingerprint)
        now = at or utc_now()
        with self.store.write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                raise SourceNotFound()
            if row["status"] not in {"paused", "retryable", "pending"}:
                raise SourceLeaseConflict()
            conn.execute(
                "UPDATE source_outbox SET authorization_fingerprint = ?, "
                "authorization_expires_at = ?, status = 'pending', error_code = NULL, "
                "updated_at = ? WHERE effect_id = ?",
                (authorization_fingerprint, authorization_expires_at, now, effect_id),
            )
            updated = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            assert updated is not None
            return _effect_from_row(updated)

    @staticmethod
    def _receipt_from_row(row: Any) -> EffectReceipt:
        return EffectReceipt(
            receipt_id=str(row["receipt_id"]),
            effect_id=str(row["effect_id"]),
            target_domain=str(row["target_domain"]),
            result_ref=str(row["result_ref"]),
            result_sha256=str(row["result_sha256"]),
            received_at=str(row["received_at"]),
        )


class OutboxDispatcher:
    """Small delivery loop that never stores a consumer exception message."""

    def __init__(self, outbox: SourceOutbox, worker_id: str) -> None:
        self.outbox = outbox
        self.worker_id = worker_id

    def deliver_once(
        self,
        handler: Callable[[OutboxEffect], tuple[str, str]],
        *,
        lease_seconds: int = 60,
    ) -> OutboxEffect | None:
        leased = self.outbox.lease(
            self.worker_id, limit=1, lease_seconds=lease_seconds
        )
        if not leased:
            return None
        effect = leased[0]
        try:
            result_ref, result_sha256 = handler(effect)
            self.outbox.complete(
                effect.effect_id,
                self.worker_id,
                result_ref=result_ref,
                result_sha256=result_sha256,
            )
        except Exception:
            self.outbox.fail(
                effect.effect_id,
                self.worker_id,
                error_code="consumer_failure",
                retryable=True,
            )
        return self.outbox.get(effect.effect_id)
