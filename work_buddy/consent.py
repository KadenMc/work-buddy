"""Consent system for gating sensitive work-buddy operations.

This module provides a hard programmatic stop for operations that require
user approval. The decorator raises ConsentRequired if no valid consent
exists in the cache — the function body never executes without it.

Consent is stored in a session-scoped SQLite database:
    - agents/<session>/consent.db — all consent grants (session-scoped)
    - agents/<session>/consent_audit.log — audit trail (session-scoped)

ALL grants are session-scoped — new sessions start with a clean slate.

Three consent modes:
    - "always": long-lived (24h TTL), session-scoped
    - "temporary": time-limited via caller-specified TTL
    - "once": single-use, auto-revoked after successful execution

Flow:
    1. Decorated function is called
    2. Decorator checks session DB for valid consent
    3. If found and valid: function proceeds (once grants auto-revoke after success)
    4. If not found: raises ConsentRequired with operation details
    5. Caller grants consent via grant_consent() or wb_run("consent_grant", ...)
    6. Caller retries the function (DB now has valid entry)
"""

import functools
import sqlite3
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from work_buddy.agent_session import (
    get_session_consent_db_path,
    get_session_audit_path,
)


class Risk(str, Enum):
    """Risk levels for consent-gated operations."""
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class ConsentRequired(Exception):
    """Raised when a function requires user consent to proceed.

    Attributes:
        operation: Unique identifier for the operation.
        reason: Human-readable explanation of what the operation does.
        risk: Risk level — "low", "moderate", or "high".
        default_ttl: Suggested TTL in minutes for temporary grants.
    """

    def __init__(
        self,
        operation: str,
        reason: str,
        risk: str,
        default_ttl: int,
    ):
        self.operation = operation
        self.reason = reason
        self.risk = risk
        self.default_ttl = default_ttl
        super().__init__(
            f"ConsentRequired: '{operation}' ({risk} risk)\n"
            f"Reason: {reason}\n"
            f"Suggested TTL: {default_ttl} minutes\n"
            f"\n"
            f"To proceed, call:\n"
            f"  grant_consent('{operation}', mode='always')\n"
            f"  OR\n"
            f"  grant_consent('{operation}', mode='temporary', "
            f"ttl_minutes={default_ttl})\n"
            f"  OR\n"
            f"  grant_consent('{operation}', mode='once')"
        )


class ConsentCache:
    """Session-scoped consent cache backed by SQLite.

    All grants live in agents/<session>/consent.db. No global tier —
    every session starts with a clean slate.

    Workflow consent: When a workflow is active, a blanket grant
    (``__workflow_consent__``) covers all operations unless a step
    explicitly opts out via ``requires_individual_consent: true``.
    """

    # "Always" grants expire after 24 hours
    _ALWAYS_TTL_HOURS = 24

    # Sentinel operation for workflow-level blanket consent
    WORKFLOW_CONSENT_OP = "__workflow_consent__"

    def __init__(self):
        self._db_path: Path | None = None
        self._initialized = False

    def _get_db_path(self) -> Path:
        if self._db_path is None:
            self._db_path = get_session_consent_db_path()
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        """Open a connection and ensure the schema exists."""
        db_path = self._get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        if not self._initialized:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS grants (
                    operation TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    expires_at TEXT
                )
            """)
            conn.commit()
            self._initialized = True
        return conn

    def is_granted(self, operation: str) -> bool:
        """Check if a valid consent exists for the operation.

        Checks in order:
        1. Per-operation grant (explicit consent for this exact operation)
        2. Workflow blanket grant (active workflow implies consent for all ops)
        """
        conn = self._connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            # 1. Check per-operation grant
            row = conn.execute(
                """SELECT 1 FROM grants
                   WHERE operation = ?
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (operation, now),
            ).fetchone()
            if row:
                return True

            # 2. Check workflow blanket (unless operation explicitly excluded)
            if operation != self.WORKFLOW_CONSENT_OP:
                wf_row = conn.execute(
                    """SELECT 1 FROM grants
                       WHERE operation = ?
                         AND (expires_at IS NULL OR expires_at > ?)""",
                    (self.WORKFLOW_CONSENT_OP, now),
                ).fetchone()
                if wf_row:
                    return True

            # Clean up expired entries lazily
            conn.execute(
                "DELETE FROM grants WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
            conn.commit()
            return False
        finally:
            conn.close()

    def get_mode(self, operation: str) -> str | None:
        """Return the mode of a grant, or None if not found/expired."""
        conn = self._connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            row = conn.execute(
                """SELECT mode FROM grants
                   WHERE operation = ?
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (operation, now),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def grant(
        self,
        operation: str,
        mode: str,
        ttl_minutes: int | None = None,
    ) -> None:
        """Grant consent for an operation (all grants are session-scoped).

        mode="always": 24h TTL, session-scoped.
        mode="temporary": caller-specified TTL, session-scoped.
        mode="once": no expiry (revoked programmatically after execution).
        """
        now = datetime.now(timezone.utc)

        if mode == "always":
            expires_at = (now + timedelta(hours=self._ALWAYS_TTL_HOURS)).isoformat()
        elif mode == "temporary":
            if ttl_minutes is None:
                raise ValueError("ttl_minutes is required for temporary consent")
            expires_at = (now + timedelta(minutes=ttl_minutes)).isoformat()
        elif mode == "once":
            expires_at = None
        else:
            raise ValueError(
                f"Invalid mode: {mode}. Must be 'always', 'temporary', or 'once'."
            )

        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO grants (operation, mode, granted_at, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (operation, mode, now.isoformat(), expires_at),
            )
            conn.commit()
        finally:
            conn.close()

    def revoke(self, operation: str) -> None:
        """Revoke consent for an operation."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM grants WHERE operation = ?", (operation,))
            conn.commit()
        finally:
            conn.close()

    def list_all(self) -> dict[str, Any]:
        """Return all consent entries, marking expired ones."""
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT operation, mode, granted_at, expires_at FROM grants"
            ).fetchall()
        finally:
            conn.close()

        result = {}
        for operation, mode, granted_at, expires_at in rows:
            entry: dict[str, Any] = {
                "mode": mode,
                "granted_at": granted_at,
            }
            if expires_at:
                entry["expires_at"] = expires_at
                try:
                    expiry = datetime.fromisoformat(expires_at)
                    entry["expired"] = now >= expiry
                except ValueError:
                    entry["expired"] = True
            result[operation] = entry

        return result


def _audit_log(event: str, operation: str, details: str = "") -> None:
    """Append an entry to the session's audit log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{now} | {event} | {operation}"
    if details:
        line += f" | {details}"
    try:
        audit_path = get_session_audit_path()
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # Don't let audit failures block operations


# Module-level cache instance
_cache = ConsentCache()

# Registry of consent metadata — populated at import time by @requires_consent.
# Maps operation ID → {reason, risk, default_ttl}.
# Used by the gateway's auto-request logic to build rich notification bodies.
_CONSENT_REGISTRY: dict[str, dict[str, Any]] = {}


def get_consent_metadata(operation: str) -> dict[str, Any] | None:
    """Look up metadata for a consent-gated operation.

    Returns {reason, risk, default_ttl} or None if the operation
    hasn't been registered (i.e., no @requires_consent has been
    imported for it yet).
    """
    return _CONSENT_REGISTRY.get(operation)


def requires_consent(
    operation: str,
    reason: str,
    risk: str = "moderate",
    default_ttl: int = 5,
):
    """Decorator that gates a function on user consent.

    The function body will NEVER execute without a valid consent entry
    in the cache. If no consent exists, ConsentRequired is raised.

    For mode="once" grants, the grant is auto-revoked after the function
    executes successfully. If the function raises, the grant is preserved
    so the caller can retry.

    Args:
        operation: Unique identifier for this operation.
        reason: Human-readable explanation shown to the user.
        risk: "low", "moderate", or "high" (validated against Risk enum).
        default_ttl: Suggested TTL in minutes for temporary grants.
    """
    # Register metadata for gateway auto-request lookup
    _CONSENT_REGISTRY[operation] = {
        "reason": reason,
        "risk": risk,
        "default_ttl": default_ttl,
    }

    # Validate risk at decoration time (fail-fast on typos)
    if risk not in (r.value for r in Risk):
        raise ValueError(
            f"Invalid risk value: {risk!r}. "
            f"Must be one of: {', '.join(r.value for r in Risk)}"
        )

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if _cache.is_granted(operation):
                # Determine consent source for audit
                op_mode = _cache.get_mode(operation)
                if op_mode is not None:
                    # Direct per-operation grant
                    is_once = op_mode == "once"
                    _audit_log("EXECUTED", operation, "consent_valid")
                else:
                    # Covered by workflow blanket
                    is_once = False
                    _audit_log("EXECUTED", operation, "workflow_blanket")

                result = fn(*args, **kwargs)

                # Auto-revoke "once" grants after successful execution
                if is_once:
                    _cache.revoke(operation)
                    _audit_log("AUTO_REVOKED", operation, "once_grant_consumed")

                return result

            # No valid consent — raise
            _audit_log("BLOCKED", operation, "no_consent")
            raise ConsentRequired(
                operation=operation,
                reason=reason,
                risk=risk,
                default_ttl=default_ttl,
            )
        return wrapper
    return decorator


def grant_consent(
    operation: str,
    mode: str = "always",
    ttl_minutes: int | None = None,
) -> None:
    """Grant consent for an operation.

    Args:
        operation: The operation identifier.
        mode: "always" (permanent), "temporary" (time-limited), or "once" (single-use).
        ttl_minutes: Expiry in minutes for "temporary" mode. Required for temporary,
                     ignored for always/once.
    """
    _cache.grant(operation, mode, ttl_minutes=ttl_minutes)
    details = f"{mode}"
    if mode == "temporary":
        details += f" | ttl={ttl_minutes}m"
    _audit_log("GRANTED", operation, details)


def grant_consent_batch(
    operations: list[str],
    mode: str = "always",
    ttl_minutes: int | None = None,
) -> None:
    """Grant consent for multiple operations at once.

    Used by the gateway's auto-consent flow to write grants for all
    operations in a bundled consent request after a single user approval.
    """
    for op in operations:
        grant_consent(op, mode=mode, ttl_minutes=ttl_minutes)


def revoke_consent(operation: str) -> None:
    """Revoke consent for an operation."""
    _cache.revoke(operation)
    _audit_log("REVOKED", operation)


def list_consents() -> dict[str, Any]:
    """List all consent entries with status."""
    return _cache.list_all()


# ---------------------------------------------------------------------------
# Workflow consent — blanket grants for active workflows
# ---------------------------------------------------------------------------

_WORKFLOW_DEFAULT_TTL_MINUTES = 180  # 3 hours


def grant_workflow_consent(
    workflow_run_id: str,
    ttl_minutes: int = _WORKFLOW_DEFAULT_TTL_MINUTES,
) -> None:
    """Grant blanket consent for all operations during a workflow run.

    When active, ``@requires_consent`` checks pass for ANY operation
    (unless the step explicitly opts out). The grant expires after
    *ttl_minutes* or when explicitly revoked at workflow completion.

    Args:
        workflow_run_id: For audit trail only.
        ttl_minutes: How long the blanket lasts (default 3h).
    """
    _cache.grant(
        ConsentCache.WORKFLOW_CONSENT_OP,
        mode="temporary",
        ttl_minutes=ttl_minutes,
    )
    _audit_log(
        "WORKFLOW_CONSENT_GRANTED",
        ConsentCache.WORKFLOW_CONSENT_OP,
        f"workflow={workflow_run_id} | ttl={ttl_minutes}m",
    )


def revoke_workflow_consent(workflow_run_id: str = "") -> None:
    """Revoke the workflow blanket consent (called on workflow completion)."""
    try:
        _cache.revoke(ConsentCache.WORKFLOW_CONSENT_OP)
        _audit_log(
            "WORKFLOW_CONSENT_REVOKED",
            ConsentCache.WORKFLOW_CONSENT_OP,
            f"workflow={workflow_run_id}" if workflow_run_id else "",
        )
    except Exception:
        pass  # Already revoked or never granted — no-op


def is_workflow_consent_active() -> bool:
    """Check if there's an active workflow blanket consent."""
    return _cache.is_granted(ConsentCache.WORKFLOW_CONSENT_OP)


# ---------------------------------------------------------------------------
# Async consent requests — thin wrappers around notifications/
# ---------------------------------------------------------------------------

def create_consent_request(
    operation: str,
    reason: str,
    risk: str = "moderate",
    default_ttl: int = 5,
    requester: str = "unknown",
    context: dict | None = None,
    callback: dict | None = None,
    callback_session_id: str | None = None,
    surfaces: list[str] | None = None,
) -> dict:
    """Create a pending consent request for out-of-conversation approval.

    This is a consent-specific wrapper around the generic notification system.
    Creates a CHOICE-type request with consent-specific options (always,
    temporary, once, deny).

    Args:
        operation: Operation identifier (same as @requires_consent keys).
        reason: Human-readable explanation.
        risk: "low", "moderate", or "high".
        default_ttl: Suggested TTL in minutes.
        requester: Who is requesting (e.g., "sidecar:cron_cleanup", "agent:<id>").
        context: Optional metadata for the UI (shown in the Obsidian modal).
        callback: What to run on approval: {"capability": "...", "params": {...}}.
        callback_session_id: If set, resume this Claude Code session on approval.

    Returns:
        Dict with notification_id (aliased as request_id) and the full record.
    """
    from work_buddy.notifications.models import (
        Notification, ResponseType, SourceType,
    )
    from work_buddy.notifications.store import create_notification

    # Determine source type from requester string
    source_type = (
        SourceType.AGENT.value
        if requester.startswith("agent:")
        else SourceType.PROGRAMMATIC.value
    )

    notification = Notification(
        title=f"Consent: {operation}",
        body=reason,
        priority="high" if risk == "high" else "normal",
        source=requester,
        source_type=source_type,
        tags=["consent", f"risk:{risk}", f"op:{operation}"],
        response_type=ResponseType.CHOICE.value,
        choices=[
            {"key": "always", "label": "Allow always (this session, 24h)", "description": "Session-scoped, expires after 24 hours"},
            {"key": "temporary", "label": f"Allow for {default_ttl} min", "description": f"Expires after {default_ttl} minutes"},
            {"key": "once", "label": "Allow once", "description": "Auto-revoked after one execution"},
            {"key": "deny", "label": "Deny", "description": "Do not proceed"},
        ],
        callback=callback,
        callback_session_id=callback_session_id,
        surfaces=surfaces,
    )

    # Store consent-specific metadata in custom_template for the resolver
    notification.custom_template = {
        "consent_meta": {
            "operation": operation,
            "risk": risk,
            "default_ttl": default_ttl,
            "context": context,
        },
    }

    created = create_notification(notification)

    # Return a dict for backward compatibility with MCP capabilities
    result = created.to_dict()
    result["request_id"] = result["notification_id"]  # alias
    _audit_log("REQUEST_CREATED", operation,
               f"request_id={created.notification_id} | requester={requester}")
    return result


def resolve_consent_request(
    request_id: str,
    approved: bool,
    mode: str = "temporary",
    ttl_minutes: int | None = None,
) -> dict:
    """Approve or deny a pending consent request.

    Maps the user's choice to a consent grant and dispatches callbacks.

    Args:
        request_id: The notification ID to resolve.
        approved: True to approve, False to deny.
        mode: Grant mode if approved ("always", "temporary", "once").
        ttl_minutes: TTL for temporary grants.

    Returns:
        The resolved record with dispatch status.
    """
    from work_buddy.notifications.models import StandardResponse, ResponseType
    from work_buddy.notifications import store

    notification = store.get_notification(request_id)
    if notification is None:
        raise ValueError(f"Consent request not found: {request_id}")
    if notification.status not in ("pending", "delivered"):
        raise ValueError(f"Request {request_id} is already {notification.status}")

    # Map approved/mode to a choice key
    if approved:
        choice_key = mode  # "always", "temporary", or "once"
    else:
        choice_key = "deny"

    response = StandardResponse(
        response_type=ResponseType.CHOICE.value,
        value=choice_key,
        raw={"approved": approved, "mode": mode, "ttl_minutes": ttl_minutes},
        surface="direct",  # resolved programmatically, not via a UI surface
    )

    # Record the response
    store.respond_to_notification(request_id, response)

    # Extract consent metadata
    consent_meta = (notification.custom_template or {}).get("consent_meta", {})
    operation = consent_meta.get("operation", notification.title.removeprefix("Consent: "))
    default_ttl = consent_meta.get("default_ttl", 5)

    dispatch_status = None

    if approved:
        # Write the consent grant
        ttl = ttl_minutes or default_ttl
        grant_consent(
            operation, mode=mode,
            ttl_minutes=ttl if mode == "temporary" else None,
        )
        # Dispatch callback
        dispatch_status = store.dispatch_callback(notification)

        _audit_log("REQUEST_APPROVED", operation,
                   f"request_id={request_id} | mode={mode}"
                   + (f" | dispatch={dispatch_status}" if dispatch_status else ""))
    else:
        _audit_log("REQUEST_DENIED", operation, f"request_id={request_id}")

    # Return dict for backward compatibility
    result = store.get_notification(request_id)
    out = result.to_dict() if result else {}
    out["request_id"] = request_id
    if dispatch_status:
        out["dispatch"] = dispatch_status
    return out


def get_consent_request(request_id: str) -> dict | None:
    """Get a single consent request by ID."""
    from work_buddy.notifications.store import get_notification
    n = get_notification(request_id)
    if n is None:
        return None
    result = n.to_dict()
    result["request_id"] = result["notification_id"]
    return result


def list_pending_requests() -> list[dict]:
    """List all pending consent requests."""
    from work_buddy.notifications.store import list_pending
    pending = list_pending()
    results = []
    for n in pending:
        # Filter to consent requests only (tagged with "consent")
        if "consent" in (n.tags or []):
            d = n.to_dict()
            d["request_id"] = d["notification_id"]
            results.append(d)
    return results
