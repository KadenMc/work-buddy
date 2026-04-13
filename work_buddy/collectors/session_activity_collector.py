"""Collect session activity from the MCP activity ledger.

Reads the current session's ledger and produces a markdown summary
suitable for inclusion in context bundles. Downstream consumers
(morning routine, journal updates, blindspot detection) read this
file from the bundle directory.
"""

from typing import Any


def collect(cfg: dict[str, Any]) -> str:
    """Return a markdown summary of this session's work-buddy activity."""
    try:
        from work_buddy.mcp_server.activity_ledger import (
            query_activity,
            query_session_summary,
        )
    except Exception:
        return _empty("Activity ledger module not available.")

    summary = query_session_summary()
    if summary.get("total_events", 0) == 0:
        return _empty("No MCP-tracked work-buddy activity in this session.")

    events = query_activity(last_n=30)
    return _format(summary, events)


def _empty(reason: str) -> str:
    return f"## Session Activity\n\n_{reason}_\n"


def _format(summary: dict[str, Any], events: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("## Session Activity (MCP Ledger)")
    lines.append("")

    sid = summary.get("session_id", "unknown")
    total = summary.get("total_events", 0)
    dur = summary.get("duration_minutes")
    dur_str = f"{dur}m" if dur is not None else "unknown"
    lines.append(f"**Session:** `{sid[:12]}` | **Events:** {total} | **Duration:** {dur_str}")
    lines.append("")

    # By category
    by_cat = summary.get("by_category", {})
    if by_cat:
        lines.append("### Activity by category")
        for cat, count in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- **{cat}:** {count}")
        lines.append("")

    # Top capabilities
    by_cap = summary.get("by_capability", {})
    if by_cap:
        lines.append("### Top capabilities invoked")
        for cap, count in list(by_cap.items())[:8]:
            lines.append(f"- `{cap}` x{count}")
        lines.append("")

    # Workflows
    wf_started = summary.get("workflows_started", 0)
    wf_completed = summary.get("workflows_completed", 0)
    if wf_started:
        lines.append(f"### Workflows: {wf_completed}/{wf_started} completed")
        lines.append("")

    # Key artifacts
    artifacts = summary.get("key_artifacts", [])
    if artifacts:
        lines.append("### Key artifacts created")
        for a in artifacts[:10]:
            lines.append(f"- `{a}`")
        lines.append("")

    # Signals
    errors = summary.get("errors", 0)
    consent = summary.get("consent_requests", 0)
    mutations = summary.get("mutations", 0)
    lines.append(f"**Mutations:** {mutations} | **Errors:** {errors} | **Consent requests:** {consent}")
    lines.append("")

    # Recent events (compact timeline)
    event_list = events.get("events", [])
    if event_list:
        lines.append("### Recent events (newest first)")
        for ev in event_list[:15]:
            ts = ev.get("ts", "")
            # Extract just the time part
            time_part = ts[11:19] if len(ts) > 19 else ts
            ev_type = ev.get("type", "")
            if ev_type == "capability_invoked":
                cap = ev.get("capability", "?")
                status = ev.get("status", "?")
                dur_ms = ev.get("duration_ms", 0)
                marker = "[ERR]" if status == "error" else "[CONSENT]" if status == "consent_required" else "->"
                lines.append(f"- {time_part} {marker} `{cap}` ({dur_ms}ms)")
            elif ev_type == "workflow_started":
                wf = ev.get("workflow_name", "?")
                lines.append(f"- {time_part} [WF] workflow `{wf}`")
            elif ev_type == "workflow_step_completed":
                step = ev.get("step_id") or ev.get("step_name") or "?"
                lines.append(f"- {time_part} [STEP] step `{step}`")
            elif ev_type == "search_performed":
                q = ev.get("query", "?")
                lines.append(f"- {time_part} [SEARCH] {q}")
        lines.append("")

    return "\n".join(lines)
