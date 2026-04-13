"""AgentIngest — unified inbound event delivery to agent sessions.

This module provides a thin abstraction over work-buddy's messaging
service for delivering external events to running Claude Code sessions.

Architecture
~~~~~~~~~~~~

Events reach agents through three delivery tiers, all backed by the
messaging service:

1. **UserPromptSubmit hook** (existing) — fires at the start of each
   user turn; surfaces *all* pending messages.  Durable, guaranteed.
2. **PostToolUse hook** (AgentIngest) — fires after each ``wb_*`` MCP
   tool call; surfaces *high-priority* session-targeted events mid-turn.
3. **Stop hook** (AgentIngest) — fires when the agent finishes
   responding; blocks the stop (exit 2) if events are pending so the
   agent can review them before going idle.

All three tiers call ``check_messages.sh`` with different modes.  The
script queries the messaging service for session-targeted messages
tagged ``agent-ingest`` and/or ``notification-callback``.

Disposition model
~~~~~~~~~~~~~~~~~

When an agent receives ingest events (especially via the Stop hook), it
resolves each with a disposition:

- **process** — handle it now (continue working)
- **defer** — leave for the next session to handle
- **ack** — acknowledge receipt, no action needed (informational)
- **redirect** — forward to another session/agent

Not every message is a task.  Informational events (e.g., context shared
between collaborating sessions) only need acknowledgment.

Future: Channels
~~~~~~~~~~~~~~~~

Claude Code Channels (``notifications/claude/channel``) can push events
into a running session between *any* tool calls, not just ``wb_*``
calls.  When channel support is added, ``create_ingest_event`` will emit
through both hooks (durable) and channels (fast-path).  Channels require
the ``--channels`` CLI flag and are not available in Desktop sessions
without the CLI + Remote Control workaround.
"""

from __future__ import annotations

import json
import os
from typing import Any

_INGEST_TAGS = ["agent-ingest"]


def create_ingest_event(
    *,
    session_id: str | None = None,
    event_type: str,
    payload: dict[str, Any],
    priority: str = "high",
    subject: str | None = None,
    extra_tags: list[str] | None = None,
) -> dict | None:
    """Create an agent-ingest event in the messaging service.

    The event is targeted at *session_id* (defaults to the current
    ``WORK_BUDDY_SESSION_ID``).  The PostToolUse and Stop hooks will
    pick it up via session-filtered queries for messages tagged
    ``agent-ingest``.

    Parameters
    ----------
    session_id:
        Target session.  Defaults to ``WORK_BUDDY_SESSION_ID``.
    event_type:
        Semantic type, e.g. ``consent.resolved``, ``notification.responded``,
        ``context.shared``, ``system.warning``.
    payload:
        Arbitrary JSON-serialisable data attached to the event.
    priority:
        Message priority (``low``, ``normal``, ``high``, ``urgent``).
        Defaults to ``high`` so the urgent/stop hooks surface it.
    subject:
        Human-readable one-liner.  Defaults to *event_type*.
    extra_tags:
        Additional tags beyond the default ``agent-ingest``.

    Returns the messaging service response dict, or None on failure.
    """
    from work_buddy.messaging.client import send_message

    sid = session_id or os.environ.get("WORK_BUDDY_SESSION_ID")
    tags = list(_INGEST_TAGS) + (extra_tags or [])

    return send_message(
        sender="agent-ingest",
        recipient="work-buddy",
        type="event",
        subject=subject or event_type,
        body=json.dumps({"event_type": event_type, **payload}),
        priority=priority,
        tags=tags,
        recipient_session=sid,
    )


def resolve_event(
    message_id: str,
    disposition: str,
    details: str = "",
) -> dict | None:
    """Mark an ingest event with the agent's chosen disposition.

    Parameters
    ----------
    message_id:
        The messaging-service message ID.
    disposition:
        One of ``process``, ``defer``, ``ack``, ``redirect``.
    details:
        Optional context (e.g., redirect target session ID).

    This updates the message status so subsequent hook checks no longer
    surface it.
    """
    from work_buddy.messaging.client import update_status

    # ``process`` and ``ack`` → mark as read (handled).
    # ``defer`` → leave as pending (next session picks it up).
    # ``redirect`` → leave as pending but re-target.
    if disposition in ("process", "ack"):
        return update_status(message_id, "read")
    elif disposition == "defer":
        # Nothing to do — message stays pending for the next session.
        return {"status": "deferred", "message_id": message_id}
    elif disposition == "redirect":
        # Re-targeting requires updating recipient_session.
        # For now, log and leave pending — the redirect target session
        # will pick it up via its own hooks once we add re-targeting
        # support to the messaging API.
        return {"status": "redirect_pending", "message_id": message_id, "details": details}
    else:
        raise ValueError(f"Unknown disposition: {disposition!r}. Expected: process, defer, ack, redirect")
