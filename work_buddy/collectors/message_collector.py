"""Context bundle collector for inter-agent messaging state.

Queries the messaging service (or falls back to direct SQLite) to
produce a summary of pending messages, active threads, and stale items.
"""

import os
from datetime import datetime, timezone
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Senders whose pending messages are machine traffic (notification acks, retry
# pings, system FYIs), not human/agent correspondence. They are reported as a
# single collapsed line and never feed the priority / avoidance heuristics — a
# pile of unresolved machine pings is not a backlog the user is avoiding.
_MACHINE_SENDERS = {
    "notification-system",
    "agent-ingest",
    "obsidian-gateway",
    "obsidian-consent-modal",
    "dashboard",
}


def _is_machine(sender: str) -> bool:
    return sender in _MACHINE_SENDERS or sender.startswith("sidecar")


def _current_session() -> str | None:
    """The session whose inbox this digest is for, used to drop other sessions'
    session-targeted pings. ``None`` falls back to project-wide (broadcasts plus
    every session's targeted messages)."""
    return os.environ.get("WORK_BUDDY_SESSION_ID")


def collect(cfg: dict[str, Any]) -> str:
    """Return a markdown summary of messaging state.

    Tries the HTTP client first (if service is running), then falls
    back to direct SQLite queries.
    """
    try:
        return _collect_via_client(cfg)
    except Exception as exc:
        logger.debug("Client collect failed (%s), trying direct SQLite", exc)

    try:
        return _collect_via_sqlite(cfg)
    except Exception as exc:
        logger.warning("Message collection failed: %s", exc)
        return "# Messages\n\nMessage collection unavailable.\n"


def _collect_via_client(cfg: dict[str, Any]) -> str:
    from work_buddy.messaging.client import is_service_running, query_messages

    if not is_service_running():
        raise RuntimeError("service not running")

    session = _current_session()
    inbox = query_messages(recipient="work-buddy", session=session, status="pending")
    # Get all non-pending for thread activity
    all_msgs = query_messages(recipient="work-buddy", session=session, limit=100)

    return _format_summary(inbox, all_msgs)


def _collect_via_sqlite(cfg: dict[str, Any]) -> str:
    from work_buddy.messaging.models import get_connection, query_messages as db_query

    conn = get_connection(cfg)
    try:
        session = _current_session()
        inbox = db_query(conn, recipient="work-buddy", session=session, status="pending")
        all_msgs = db_query(conn, recipient="work-buddy", session=session, limit=100)
        return _format_summary(inbox, all_msgs)
    finally:
        conn.close()


def _format_summary(
    pending: list[dict[str, Any]],
    all_messages: list[dict[str, Any]],
) -> str:
    lines = ["# Messages\n"]

    if not pending and not all_messages:
        lines.append("No messages.\n")
        return "\n".join(lines)

    # Partition pending into correspondence (genuine action items from a human or
    # another agent) vs machine traffic (notification acks, retry pings, system
    # FYIs). Only correspondence feeds the priority + avoidance heuristics; a pile
    # of unresolved machine pings is not a backlog the user is avoiding, so it is
    # collapsed to one de-emphasized line.
    correspondence: list[dict[str, Any]] = []
    machine: list[dict[str, Any]] = []
    for m in pending:
        disposition = m.get("disposition") or "actionable"
        if disposition == "actionable" and not _is_machine(m.get("sender", "")):
            correspondence.append(m)
        else:
            machine.append(m)

    if correspondence:
        lines.append(f"## Pending ({len(correspondence)})\n")
        urgent = [m for m in correspondence if m.get("priority") in ("high", "urgent")]
        if urgent:
            lines.append(f"**{len(urgent)} high/urgent priority**\n")

        for m in correspondence:
            age = _age_str(m.get("created_at", ""))
            flag = " **URGENT**" if m.get("priority") == "urgent" else ""
            flag = " **HIGH**" if m.get("priority") == "high" else flag
            lines.append(
                f"- [{m.get('type', '?')}] from **{m.get('sender', '?')}**: "
                f"{m.get('subject', '(no subject)')} ({age}){flag}"
            )

        # Flag stale correspondence only — machine pings never read as avoidance.
        stale = [m for m in correspondence if _is_stale(m.get("created_at", ""))]
        if stale:
            lines.append(f"\n**{len(stale)} message(s) pending >48h — possible avoidance signal**\n")
    else:
        lines.append("## Pending\n\nNo correspondence pending.\n")

    if machine:
        lines.append(
            f"\n_{len(machine)} system notification(s)/ping(s) pending "
            f"(machine traffic, not counted as correspondence)._\n"
        )

    # Active threads
    thread_ids = {m.get("thread_id") for m in all_messages if m.get("thread_id")}
    if thread_ids:
        lines.append(f"\n## Active Threads ({len(thread_ids)})\n")
        for tid in sorted(thread_ids):
            thread_msgs = [m for m in all_messages if m.get("thread_id") == tid]
            latest = max(thread_msgs, key=lambda m: m.get("created_at", ""))
            lines.append(
                f"- `{tid}`: {len(thread_msgs)} message(s), "
                f"latest from {latest.get('sender', '?')} ({_age_str(latest.get('created_at', ''))})"
            )

    return "\n".join(lines) + "\n"


def _age_str(iso_ts: str) -> str:
    try:
        created = datetime.fromisoformat(iso_ts)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{int(hours)}h ago"
        return f"{int(hours / 24)}d ago"
    except (ValueError, TypeError):
        return "?"


def _is_stale(iso_ts: str, threshold_hours: float = 48) -> bool:
    try:
        created = datetime.fromisoformat(iso_ts)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        return delta.total_seconds() > threshold_hours * 3600
    except (ValueError, TypeError):
        return False
