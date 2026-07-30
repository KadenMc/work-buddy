"""Non-agent-callable work on the existing disk-backed operation queue.

Internal operations use the same ``agents/operations`` records and
``RetrySweep`` lease as capability retries, but resolve through this closed
allowlist instead of the MCP capability registry. This keeps recovery work
durable without making an execution primitive discoverable through
``wb_search`` or callable through ``wb_run``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from work_buddy.markdown_db.storage_helpers import atomic_write_text, file_lock
from work_buddy.truth.identity import canonical_json, sha256_text


INTERNAL_OPERATION_TYPE = "internal"
COWORK_VERIFY_LAUNCH = "cowork_verify_launch"
_ALLOWED_HANDLERS = frozenset({COWORK_VERIFY_LAUNCH})


class InternalOperationError(RuntimeError):
    """An internal queue record is unknown or malformed."""


class InternalOperationRetry(RuntimeError):
    """The exact internal operation is valid but not ready to finish."""


def _operations_dir() -> Path:
    from work_buddy.operations_read import operations_dir

    path = operations_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def internal_operation_id(handler: str, deduplication_key: str) -> str:
    """Return a stable queue identity for one logical internal operation."""

    if handler not in _ALLOWED_HANDLERS:
        raise InternalOperationError(f"unknown internal operation: {handler}")
    key = str(deduplication_key or "").strip()
    if not key:
        raise InternalOperationError(
            "internal operation deduplication_key must not be empty"
        )
    digest = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.internal-operation/v1",
                "handler": handler,
                "deduplication_key": key,
            }
        )
    )
    return f"op_internal_{digest[:32]}"


def _originating_session(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        from work_buddy.agent_session import get_originating_session

        current = get_originating_session()
        if current:
            return current
    except Exception:
        pass
    return os.environ.get("WORK_BUDDY_SESSION_ID", "unknown")


def enqueue_internal_operation(
    handler: str,
    params: Mapping[str, Any],
    *,
    deduplication_key: str,
    authorization_expires_at: str,
    originating_session_id: str | None = None,
    lease_seconds: int = 120,
    max_attempts: int = 3,
    operations_dir: Path | None = None,
) -> dict[str, Any]:
    """Create or replay one exact non-discoverable queue record.

    The deterministic operation ID plus immutable payload comparison makes
    concurrent identical enqueue calls converge. A conflicting reuse of the
    same logical key fails closed.
    """

    if handler not in _ALLOWED_HANDLERS:
        raise InternalOperationError(f"unknown internal operation: {handler}")
    if lease_seconds < 1:
        raise InternalOperationError("internal operation lease must be positive")
    if max_attempts < 2:
        raise InternalOperationError(
            "internal operations require a dispatch and a recovery attempt"
        )
    try:
        expiry = datetime.fromisoformat(
            str(authorization_expires_at).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise InternalOperationError(
            "internal operation authorization expiry must be an ISO timestamp"
        ) from exc
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    params_value = dict(params)
    op_id = internal_operation_id(handler, deduplication_key)
    root = _operations_dir() if operations_dir is None else Path(operations_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{op_id}.json"
    now = datetime.now(timezone.utc)
    immutable = {
        "type": INTERNAL_OPERATION_TYPE,
        "name": handler,
        "params": params_value,
        "authorization_expires_at": expiry.isoformat(),
    }
    immutable_sha256 = sha256_text(canonical_json(immutable))
    session_id = _originating_session(originating_session_id)
    record: dict[str, Any] = {
        "operation_id": op_id,
        **immutable,
        "immutable_sha256": immutable_sha256,
        "retry_policy": "verify_first",
        "status": "failed",
        "result": None,
        "error": None,
        "attempt": 0,
        "session_id": session_id,
        "originating_session_id": session_id,
        "locked_until": None,
        "lease_token": None,
        "created_at": now.isoformat(),
        "completed_at": None,
        "queued": True,
        "queue_reason": "deferred_submit",
        "queued_for_retry": True,
        "retry_at": now.isoformat(),
        "max_retries": max_attempts,
        "backoff_strategy": "fixed_10s",
        "lease_seconds": lease_seconds,
        "retry_history": [],
    }

    with file_lock(path, timeout=2.0):
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise InternalOperationError(
                    f"internal operation record is unreadable: {op_id}"
                ) from exc
            if existing.get("immutable_sha256") != immutable_sha256:
                raise InternalOperationError(
                    "internal operation key was reused with different inputs"
                )
            return {
                "operation_id": op_id,
                "status": existing.get("status"),
                "queued": bool(
                    existing.get("queued")
                    or existing.get("queued_for_retry")
                ),
                "replayed": True,
            }
        atomic_write_text(
            path,
            json.dumps(record, ensure_ascii=True, indent=2),
        )
    return {
        "operation_id": op_id,
        "status": "queued",
        "queued": True,
        "replayed": False,
    }


def execute_internal_operation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a closed internal handler without consulting the MCP registry."""

    if record.get("type") != INTERNAL_OPERATION_TYPE:
        raise InternalOperationError("record is not an internal operation")
    name = str(record.get("name") or "")
    if name != COWORK_VERIFY_LAUNCH:
        raise InternalOperationError(f"unknown internal operation: {name}")
    from work_buddy.cowork.verify_dispatch import dispatch_verify_launch

    return dispatch_verify_launch(record)


def reconcile_internal_operations(
    *,
    operations_dir: Path | None = None,
) -> dict[str, int]:
    """Repair durable internal handoffs before the queue scans its records."""

    from work_buddy.cowork.verify_dispatch import reconcile_verify_launches

    return reconcile_verify_launches(operations_dir=operations_dir)


def internal_operation_exhausted(
    record: Mapping[str, Any],
    error: str,
) -> None:
    """Give the owning subsystem one fail-closed terminalization hook."""

    if (
        record.get("type") == INTERNAL_OPERATION_TYPE
        and record.get("name") == COWORK_VERIFY_LAUNCH
    ):
        from work_buddy.cowork.verify_dispatch import (
            exhaust_verify_launch,
        )

        exhaust_verify_launch(record, error=error)


__all__ = [
    "COWORK_VERIFY_LAUNCH",
    "INTERNAL_OPERATION_TYPE",
    "InternalOperationError",
    "InternalOperationRetry",
    "enqueue_internal_operation",
    "execute_internal_operation",
    "internal_operation_exhausted",
    "internal_operation_id",
    "reconcile_internal_operations",
]
