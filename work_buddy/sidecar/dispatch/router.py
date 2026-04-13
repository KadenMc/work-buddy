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
