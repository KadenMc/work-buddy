"""Message-to-job router — polls for work-buddy messages and dispatches them.

When a message arrives with ``recipient="work-buddy"`` and
``status="pending"``, the router:
1. Reads the full message body
2. Classifies it: does the subject match a known capability or workflow?
3. Executes the job
4. Replies with results
5. Updates the message status to ``resolved``
"""

import time
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.sidecar.dispatch.executor import (
    _execute_capability,
    _execute_prompt,
    _execute_workflow,
)

logger = get_logger(__name__)

# The project name that identifies "us" as a recipient
_RECIPIENT = "work-buddy"
_SENDER = "sidecar"


class MessagePoller:
    """Polls the messaging service for pending work-buddy messages.

    Called by the daemon on each tick. Maintains its own poll interval
    independently from the daemon's health-check interval.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        sidecar_cfg = config.get("sidecar", {})
        self._poll_interval: float = sidecar_cfg.get("message_poll_interval", 15)
        self._last_poll: float = 0.0

    def poll(self) -> None:
        """Check for and dispatch pending messages."""
        now = time.time()
        if now - self._last_poll < self._poll_interval:
            return
        self._last_poll = now

        try:
            from work_buddy.messaging.client import (
                query_messages,
                read_message,
                reply,
                update_status,
                is_service_running,
            )

            if not is_service_running():
                return  # Messaging service not up yet

            msgs = query_messages(
                recipient=_RECIPIENT,
                status="pending",
                limit=10,
            )

            if not msgs:
                return

            # Filter out session-targeted messages — those are reserved for
            # AgentIngest hooks (PostToolUse / Stop) in the target session.
            # The sidecar should only consume broadcast messages.
            broadcast_msgs = [
                m for m in msgs if not m.get("recipient_session")
            ]

            if not broadcast_msgs:
                return

            logger.info("Found %d pending broadcast message(s) for work-buddy.", len(broadcast_msgs))

            for msg_summary in broadcast_msgs:
                msg_id = msg_summary.get("id")
                if not msg_id:
                    continue
                self._handle_message(msg_id, msg_summary)

        except Exception as exc:
            logger.error("Message poll failed: %s", exc)

    def _handle_message(self, msg_id: str, msg_summary: dict) -> None:
        """Classify and dispatch a single message."""
        from work_buddy.messaging.client import read_message, reply, update_status

        # Read full message body
        full_msg = read_message(msg_id)
        if full_msg is None:
            logger.warning("Could not read message %s — skipping.", msg_id)
            return

        subject = full_msg.get("subject", "")
        body = full_msg.get("body", "")
        sender = full_msg.get("sender", "unknown")
        recipient = full_msg.get("recipient", "unknown")
        msg_type = full_msg.get("type", "")

        logger.info(
            "Message %s: %s → %s [%s] %s",
            msg_id, sender, recipient, msg_type, subject,
        )

        # --- Special case: consent_grant from out-of-band surfaces ---
        # The Obsidian modal posts a ``consent_grant`` message when the
        # user clicks Allow on a notification whose gateway poll has
        # already timed out. Routing through the generic capability
        # dispatch would write the grant to the sidecar's own session
        # DB; we want it in the ORIGINATING agent's DB so the
        # ``@requires_consent`` decorators see it on the agent's next
        # call.
        normalized_subject = (
            subject.strip().lower().replace(" ", "_").replace("-", "_")
        )
        if normalized_subject == "consent_grant":
            result = _handle_consent_grant_message(body)
        else:
            # --- Classify: try to match subject to a known capability/workflow ---
            result = self._classify_and_execute(subject, body)

        # --- Reply with results ---
        reply_body = _format_reply(result)
        try:
            reply(
                msg_id,
                sender=_SENDER,
                body=reply_body,
                type="result",
            )
        except Exception as exc:
            logger.error("Failed to reply to %s: %s", msg_id, exc)

        # --- Mark resolved ---
        try:
            update_status(msg_id, "resolved")
        except Exception as exc:
            logger.error("Failed to update status for %s: %s", msg_id, exc)

    def _classify_and_execute(self, subject: str, body: str) -> dict[str, Any]:
        """Classify a message and execute the appropriate handler.

        Classification order:
        1. Exact match of subject against registered capability names
        2. Exact match against registered workflow names
        3. Fallback: treat body as freeform prompt
        """
        try:
            from work_buddy.mcp_server.registry import (
                get_registry,
                Capability,
                WorkflowDefinition,
            )

            registry = get_registry()

            # Normalize subject for matching
            subject_lower = subject.strip().lower().replace(" ", "_").replace("-", "_")

            # Try capability match
            for name, entry in registry.items():
                if isinstance(entry, Capability):
                    if name.lower() == subject_lower:
                        logger.info("Message matched capability: %s", name)
                        params = _parse_body_params(body)
                        return _execute_capability(name, params)

            # Try workflow match
            for name, entry in registry.items():
                if isinstance(entry, WorkflowDefinition):
                    if name.lower() == subject_lower:
                        logger.info("Message matched workflow: %s", name)
                        return _execute_workflow(name)

        except Exception as exc:
            logger.warning("Registry lookup failed: %s — falling back to prompt.", exc)

        # Fallback: treat as freeform prompt
        prompt = body if body else subject
        return _execute_prompt("message-dispatch", prompt)


def _handle_consent_grant_message(body: str) -> dict[str, Any]:
    """Handle an out-of-band ``consent_grant`` message.

    The Obsidian plugin's modal-click path posts this message when the
    user approves a consent prompt that the gateway's in-window poll
    already gave up on. The plugin includes ``notification_id`` in the
    body so we can look up the originating agent's session and route
    the grant to that session's DB — not the sidecar's.

    Body shape (current plugin): JSON of
        {operation, mode, ttl_minutes, notification_id}

    If ``notification_id`` is missing (out-of-sync plugin), fall back
    to an in-process ``grant_consent`` call. The grant lands in the
    sidecar's own session DB and won't unblock the originating agent,
    but the path doesn't crash. Logs a warning so the operator knows
    to rebuild the plugin.
    """
    params = _parse_body_params(body)
    operation = params.get("operation")
    mode = params.get("mode")
    ttl_minutes = params.get("ttl_minutes")
    notification_id = params.get("notification_id")

    if not operation or not mode:
        return {
            "status": "error",
            "error": (
                f"consent_grant message missing operation/mode: "
                f"got operation={operation!r}, mode={mode!r}"
            ),
        }

    if notification_id:
        # Route through resolve_consent_request — it loads the
        # notification, reads callback_session_id, writes grants to the
        # right session DB, unbundles bundle: operations into individual
        # ops, and dispatches any pending callback.
        try:
            from work_buddy.consent import resolve_consent_request
            resolved = resolve_consent_request(
                notification_id,
                approved=True,
                mode=mode,
                ttl_minutes=ttl_minutes,
            )
            return {
                "status": "ok",
                "result": {
                    "granted": True,
                    "via": "resolve_consent_request",
                    "notification_id": notification_id,
                    "dispatch": resolved.get("dispatch"),
                },
            }
        except ValueError as exc:
            # Already-responded notifications hit this — typically
            # benign (the gateway's in-window poll already grabbed
            # the response). Log and report no-op.
            logger.info(
                "consent_grant for already-resolved notification "
                "%s: %s",
                notification_id, exc,
            )
            return {
                "status": "ok",
                "result": {
                    "granted": False,
                    "via": "resolve_consent_request",
                    "note": "notification already resolved",
                    "notification_id": notification_id,
                },
            }
        except Exception as exc:
            logger.error(
                "consent_grant for notification %s failed: %s",
                notification_id, exc,
            )
            return {"status": "error", "error": str(exc)}

    # No notification_id — Obsidian plugin is out of sync (the current
    # plugin always sends one). Fall back to a bare in-process grant so
    # the path doesn't crash, but the cross-session routing is broken
    # until the plugin is rebuilt + reloaded.
    logger.warning(
        "consent_grant message missing notification_id — falling back "
        "to in-process grant. Rebuild the Obsidian plugin to enable "
        "cross-session consent routing."
    )
    try:
        from work_buddy.consent import grant_consent
        grant_consent(operation, mode=mode, ttl_minutes=ttl_minutes)
        return {
            "status": "ok",
            "result": {
                "granted": True,
                "via": "in_process_no_notification_id",
                "operation": operation,
            },
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _parse_body_params(body: str) -> dict[str, Any]:
    """Parse a message body as JSON params for capability dispatch.

    If the body is valid JSON dict, use it directly as params.
    If it's a JSON string containing a JSON dict, parse the inner dict.
    Otherwise return empty dict.
    """
    if not body:
        return {}
    import json
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _format_reply(result: dict[str, Any]) -> str:
    """Format an execution result as a human-readable reply body."""
    status = result.get("status", "unknown")
    if status == "ok":
        res = result.get("result", "")
        if isinstance(res, dict):
            import json
            res = json.dumps(res, indent=2, default=str)
        return f"[Sidecar] Executed successfully.\n\n{res}"
    elif status == "deferred":
        return f"[Sidecar] {result.get('result', 'Deferred for agent execution.')}"
    else:
        return f"[Sidecar] Execution failed: {result.get('error', 'Unknown error.')}"
