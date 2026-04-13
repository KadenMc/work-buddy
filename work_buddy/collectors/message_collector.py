"""Context bundle collector for inter-agent messaging state.

Queries the messaging service (or falls back to direct SQLite) to
produce a summary of pending messages, active threads, and stale items.
"""

from datetime import datetime, timezone
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


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

    inbox = query_messages(recipient="work-buddy", status="pending")
    # Get all non-pending for thread activity
    all_msgs = query_messages(recipient="work-buddy", limit=100)

    return _format_summary(inbox, all_msgs)


def _collect_via_sqlite(cfg: dict[str, Any]) -> str:
    from work_buddy.messaging.models import get_connection, query_messages as db_query

    conn = get_connection(cfg)
    try:
        inbox = db_query(conn, recipient="work-buddy", status="pending")
        all_msgs = db_query(conn, recipient="work-buddy", limit=100)
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

    # Pending inbox
    if pending:
        lines.append(f"## Pending ({len(pending)})\n")
        urgent = [m for m in pending if m.get("priority") in ("high", "urgent")]
        if urgent:
            lines.append(f"**{len(urgent)} high/urgent priority**\n")

        for m in pending:
            age = _age_str(m.get("created_at", ""))
            flag = " **URGENT**" if m.get("priority") == "urgent" else ""
            flag = " **HIGH**" if m.get("priority") == "high" else flag
            lines.append(
                f"- [{m.get('type', '?')}] from **{m.get('sender', '?')}**: "
                f"{m.get('subject', '(no subject)')} ({age}){flag}"
            )

        # Flag stale messages
        stale = [m for m in pending if _is_stale(m.get("created_at", ""))]
        if stale:
            lines.append(f"\n**{len(stale)} message(s) pending >48h — possible avoidance signal**\n")
    else:
        lines.append("## Pending\n\nNo pending messages.\n")

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
