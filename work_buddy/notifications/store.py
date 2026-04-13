"""Persistent store for notifications and requests.

Notifications are stored as individual JSON files in a shared directory:
    agents/consent/requests/  (legacy path, will be migrated in future)

This store handles CRUD operations and callback dispatch when a request
is responded to. Dispatch paths:
    1. callback_session_id → resume a Claude Code session via `claude -p -r`
    2. callback → dispatch via messaging service to sidecar executor
    3. neither → just update the record (caller polls for status)
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from work_buddy.notifications.models import (
    Notification,
    NotificationStatus,
    ResponseType,
    StandardResponse,
)


# ---------------------------------------------------------------------------
# Storage directory
# ---------------------------------------------------------------------------

def _get_store_dir() -> Path:
    """Return the shared notification store directory."""
    from work_buddy.agent_session import get_consent_requests_dir
    return get_consent_requests_dir()


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def create_notification(notification: Notification) -> Notification:
    """Persist a new notification to the store.

    Auto-generates an ID and timestamps if not set.
    Assigns a 4-digit short_id for requests (used by Telegram /reply).
    Returns the notification with populated fields.
    """
    if not notification.notification_id:
        notification.notification_id = f"req_{uuid.uuid4().hex[:8]}"
    if not notification.created_at:
        notification.created_at = datetime.now(timezone.utc).isoformat()

    # Assign a memorable 4-digit short ID for requests
    if notification.is_request() and not notification.short_id:
        notification.short_id = _assign_short_id()

    # Default TTL: requests expire after 2 hours, notifications after 1 hour
    if not notification.expires_at:
        ttl_minutes = 120 if notification.is_request() else 60
        expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        notification.expires_at = expires.isoformat()

    _write_notification(notification)
    _audit("CREATED", notification)
    return notification


def get_notification(notification_id: str) -> Notification | None:
    """Get a single notification by ID."""
    path = _get_store_dir() / f"{notification_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Notification.from_dict(data)
    except (json.JSONDecodeError, OSError):
        return None


def list_pending() -> list[Notification]:
    """List all pending (unresolved) notifications/requests.

    Also sweeps expired notifications: any pending/delivered notification
    past its expires_at is moved to EXPIRED status.
    """
    store_dir = _get_store_dir()
    now = datetime.now(timezone.utc)
    pending = []
    for path in sorted(store_dir.glob("req_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            status = data.get("status")
            if status not in (
                NotificationStatus.PENDING.value,
                NotificationStatus.DELIVERED.value,
            ):
                continue

            # Sweep expired notifications
            expires_at = data.get("expires_at")
            if expires_at:
                try:
                    expires = datetime.fromisoformat(expires_at)
                    if now >= expires:
                        data["status"] = NotificationStatus.EXPIRED.value
                        notif = Notification.from_dict(data)
                        _write_notification(notif)
                        continue
                except (ValueError, TypeError):
                    pass

            pending.append(Notification.from_dict(data))
        except (json.JSONDecodeError, OSError):
            continue
    return pending


def list_all(
    status: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[Notification]:
    """List notifications with optional filters."""
    store_dir = _get_store_dir()
    results = []
    for path in sorted(store_dir.glob("req_*.json"), reverse=True):
        if len(results) >= limit:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if status and data.get("status") != status:
                continue
            if source and not data.get("source", "").startswith(source):
                continue
            results.append(Notification.from_dict(data))
        except (json.JSONDecodeError, OSError):
            continue
    return results


def respond_to_notification(
    notification_id: str,
    response: StandardResponse,
) -> Notification:
    """Record a user's response to a request.

    Updates the notification status to RESPONDED and stores the
    standardized response. If the notification has a callback,
    dispatch is triggered.

    Returns the updated notification.

    Raises ValueError if notification not found or not in a respondable state.
    """
    notification = get_notification(notification_id)
    if notification is None:
        raise ValueError(f"Notification not found: {notification_id}")
    if notification.status not in (
        NotificationStatus.PENDING.value,
        NotificationStatus.DELIVERED.value,
    ):
        raise ValueError(
            f"Notification {notification_id} is {notification.status}, "
            f"cannot respond"
        )
    if not notification.is_request():
        raise ValueError(
            f"Notification {notification_id} is type "
            f"{notification.response_type}, does not accept responses"
        )

    now = datetime.now(timezone.utc)
    notification.status = NotificationStatus.RESPONDED.value
    notification.responded_at = now.isoformat()
    notification.response = {
        "response_type": response.response_type,
        "value": response.value,
        "raw": response.raw,
        "surface": response.surface,
    }

    _write_notification(notification)
    _audit("RESPONDED", notification, f"value={response.value}")
    return notification


def mark_delivered(
    notification_id: str,
    surface: str,
) -> Notification | None:
    """Mark a notification as delivered to a specific surface.

    Supports multi-surface delivery: tracks each surface in
    delivered_surfaces and sets status to DELIVERED on first success.
    """
    notification = get_notification(notification_id)
    if notification is None:
        return None

    # Track this surface in the delivery list
    if surface not in notification.delivered_surfaces:
        notification.delivered_surfaces.append(surface)

    # First delivery transitions status
    if notification.status == NotificationStatus.PENDING.value:
        notification.status = NotificationStatus.DELIVERED.value
        notification.delivered_at = datetime.now(timezone.utc).isoformat()
        notification.surface = surface  # first surface to deliver

    _write_notification(notification)
    return notification


def cancel_notification(notification_id: str) -> Notification | None:
    """Cancel a pending notification."""
    notification = get_notification(notification_id)
    if notification is None:
        return None
    if notification.status in (
        NotificationStatus.PENDING.value,
        NotificationStatus.DELIVERED.value,
    ):
        notification.status = NotificationStatus.CANCELLED.value
        _write_notification(notification)
        _audit("CANCELLED", notification)
    return notification


# ---------------------------------------------------------------------------
# Callback dispatch
# ---------------------------------------------------------------------------

def dispatch_callback(notification: Notification) -> dict | None:
    """Dispatch the notification's callback after a response.

    Returns dispatch status dict, or None if no callback configured.

    Dispatch paths (not mutually exclusive):
        1. callback → dispatch via messaging service (for AgentIngest hook delivery)
        2. callback_session_id without callback → also dispatch via messaging
           with session targeting so hooks can pick it up
        3. callback_session_id → resume via `claude -p -r <id>` (legacy path)
        4. neither → None

    When callback_session_id is set, messaging dispatch always includes
    session targeting so the AgentIngest hooks (PostToolUse / Stop) can
    surface the event mid-turn.
    """
    results = []
    session_id = notification.callback_session_id

    # Always dispatch via messaging if there's a callback or session target.
    # This is the AgentIngest delivery path — hooks will pick it up.
    if notification.callback:
        results.append(_dispatch_via_messaging(
            notification.callback,
            notification.title,
            notification.notification_id,
            recipient_session=session_id,
        ))
    elif session_id:
        # No explicit callback but we have a session — create a minimal
        # messaging dispatch so hooks can still surface the event.
        results.append(_dispatch_via_messaging(
            {"capability": "notification_response", "params": {}},
            notification.title,
            notification.notification_id,
            recipient_session=session_id,
        ))

    # Legacy path: also attempt session resume if configured.
    # This spawns a new CLI process — useful for headless/daemon sessions
    # but not needed when hooks handle delivery.
    if session_id and notification.callback:
        # Skip session resume when messaging dispatch succeeded —
        # the hooks will handle it. Only resume if messaging failed.
        msg_result = results[0] if results else None
        if msg_result and not msg_result.get("success"):
            results.append(_dispatch_session_resume(
                session_id,
                notification.title,
                notification.notification_id,
            ))

    return results[0] if len(results) == 1 else (results or None)


def _dispatch_session_resume(
    session_id: str,
    title: str,
    notification_id: str,
) -> dict:
    """Resume a Claude Code session after user response.

    This is the **headless_persistent resume path**: a previously spawned
    persistent agent is woken up by replaying its session_id via
    ``claude --print --resume <id>``.

    Note on persistence semantics:
        The resume itself uses ``--no-session-persistence``, so this is a
        **one-write** model: the original session's context is preserved
        (it was spawned without ``--no-session-persistence``), but the
        resume run does not accumulate further state. This is intentional
        — the callback response is a single continuation, not an
        open-ended conversation.

    Future: ``interactive_persistent`` will need a different resume
    mechanism (e.g., launching a visible Claude Code window or sending
    a prompt to an existing interactive session).
    """
    import subprocess
    from work_buddy.logging_config import get_logger

    logger = get_logger(__name__)
    repo_root = Path(__file__).parent.parent.parent

    prompt = (
        f"User has responded to notification '{title}' "
        f"(id: {notification_id}). Please proceed."
    )

    cmd = [
        "claude",
        "--print",
        "--resume", session_id,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        prompt,
    ]

    try:
        from work_buddy.config import load_config
        cfg = load_config()
        timeout = cfg.get("sidecar", {}).get("agent_spawn", {}).get("timeout_seconds", 300)

        logger.info("Resuming session %s for notification %s", session_id, notification_id)

        # Update agent registry if this session is tracked
        _mark_agent_resumed(session_id)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(repo_root),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {
            "type": "session_resume",
            "session_id": session_id,
            "return_code": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        logger.warning("Session resume timed out: %s", session_id)
        return {"type": "session_resume", "session_id": session_id, "error": "timeout"}
    except Exception as exc:
        logger.error("Session resume failed: %s — %s", session_id, exc)
        return {"type": "session_resume", "session_id": session_id, "error": str(exc)}


def _mark_agent_resumed(session_id: str) -> None:
    """Update the agent registry when a persistent session is resumed.

    Best-effort: failure does not block the resume itself.
    """
    try:
        from work_buddy.sidecar.dispatch.registry import mark_resumed
        result = mark_resumed(session_id)
        if result:
            from work_buddy.logging_config import get_logger
            get_logger(__name__).debug(
                "Marked agent %s as resumed in registry.", session_id,
            )
    except Exception:
        pass  # Registry is non-critical for resume operation


def _dispatch_via_messaging(
    callback: dict,
    title: str,
    notification_id: str,
    recipient_session: str | None = None,
) -> dict:
    """Dispatch a callback via the messaging service.

    If *recipient_session* is provided, the message is targeted to that
    specific session.  The AgentIngest hooks (PostToolUse / Stop) will
    pick it up during the agent's turn via session-filtered queries.
    """
    from work_buddy.logging_config import get_logger

    logger = get_logger(__name__)
    capability = callback.get("capability", "")
    params = callback.get("params", {})

    try:
        from work_buddy.messaging.client import send_message

        result = send_message(
            sender="notification-system",
            recipient="work-buddy",
            type="result",
            subject=capability,
            body=json.dumps({
                "source": "notification_response",
                "notification_id": notification_id,
                "title": title,
                "params": params,
            }),
            priority="high",
            tags=["notification-callback", "agent-ingest"],
            recipient_session=recipient_session,
        )
        logger.info(
            "Dispatched notification callback via messaging: %s (notification=%s)",
            capability, notification_id,
        )
        return {
            "type": "messaging_dispatch",
            "capability": capability,
            "message_id": result.get("id") if result else None,
            "success": result is not None,
        }
    except Exception as exc:
        logger.error("Messaging dispatch failed: %s — %s", capability, exc)
        return {
            "type": "messaging_dispatch",
            "capability": capability,
            "error": str(exc),
            "success": False,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _assign_short_id() -> str | None:
    """Assign a unique 4-digit short ID for request-type notifications.

    Scans pending notifications to avoid collisions. With <100 concurrent
    pending requests vs 9000 slots (1000-9999), collision is negligible.
    """
    used = {n.short_id for n in list_pending() if n.short_id}
    pool = [i for i in range(1000, 10000) if str(i) not in used]
    if not pool:
        return None
    return str(random.choice(pool))


def _write_notification(notification: Notification) -> None:
    """Persist a notification to disk (atomic write)."""
    path = _get_store_dir() / f"{notification.notification_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(notification.to_dict(), indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _audit(event: str, notification: Notification, extra: str = "") -> None:
    """Log a notification lifecycle event."""
    from work_buddy.agent_session import get_session_audit_path
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (
        f"{now} | {event} | {notification.notification_id} | "
        f"{notification.title}"
    )
    if extra:
        line += f" | {extra}"
    try:
        audit_path = get_session_audit_path()
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
