"""Help-agent brief builder.

The "?" button on any non-ok requirement (and on every component) in
the Settings tab spawns a Claude Code session via this module. The
brief includes everything the agent needs to help the user without
the user having to re-explain context:

  * What the node is (id, label, description, kind)
  * Current state (effective_state + status_reason + blocking_issues)
  * For components: DiagnosticRunner output (root cause + step results
    + fix suggestion)
  * For requirements: fix_hint, severity, fix_kind, parent component
  * Pointers to relevant agent docs (so the agent can /agent_docs deeper)
  * What the agent is empowered to do

The brief is a self-contained prompt — no follow-up questions to the
launching dashboard required.

This module subsumes the old ``🪄 /wb-setup diagnose <id>`` hint shown
in the Status tab's diagnose panel — same diagnostic data, but
delivered through the same fix-agent-launch path as the rest of the
fix system.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def build_help_brief(node_id: str) -> str:
    """Return the prompt a help-agent session should be launched with.

    Accepts any node id from the control graph: ``component:obsidian``,
    ``req:obsidian/plugins/work-buddy-plugin``, ``subsystem:daily-notes``,
    etc. Subsystem and domain ids return a roll-up brief listing their
    problematic descendants.
    """
    from work_buddy.control.graph import build_graph
    nodes = build_graph()
    node = nodes.get(node_id)
    if node is None:
        return _unknown_node_brief(node_id)

    if node.kind == "requirement":
        return _requirement_brief(node)
    if node.kind == "component":
        return _component_brief(node)
    if node.kind in ("subsystem", "domain"):
        return _grouping_brief(node, nodes)
    if node.kind == "capability":
        return _capability_brief(node)
    return _generic_brief(node)


def build_help_brief_for_event(
    event: dict[str, Any],
    component_id: str | None = None,
) -> str:
    """Return the prompt for a session spawned to investigate a sidecar event.

    Peer of :func:`build_help_brief`, but keyed on a sidecar event rather
    than a control-graph node. Always includes the event metadata and the
    resolved sidecar log-file path. When ``component_id`` is given, also
    embeds the full component diagnostic section (the same content the
    component "?" help-brief produces) so the spawned agent starts with
    control-graph context for whatever emitted the event.

    Powers ``POST /api/investigate``: the Activity event log passes just
    the event; a per-component event chip passes the event + component_id.
    """
    from datetime import datetime, timezone

    ts = event.get("ts", 0)
    try:
        time_str = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
            if ts
            else "unknown"
        )
    except Exception:
        time_str = "unknown"

    parts = [
        "You are helping the user investigate a work-buddy sidecar event.",
        "",
        "## Event",
        f"**Time:** {time_str}",
        f"**Kind:** `{event.get('kind', '?')}`",
        f"**Source:** `{event.get('source', '?')}`",
        f"**Level:** `{event.get('level', '?')}`",
        f"**Summary:** {event.get('summary', '?')}",
        "",
        "## Sidecar log",
    ]
    log_path = resolve_sidecar_log_path()
    if log_path:
        parts.append(f"The sidecar log file is at: `{log_path}`")
        parts.append(
            "Search for the source name and timestamp to find the full "
            "context around this event."
        )
    else:
        parts.append(
            "No sidecar log file was found. Check the sidecar console "
            "output instead."
        )
    parts.append("")

    if component_id:
        from work_buddy.control.graph import build_graph
        nodes = build_graph()
        comp_node = nodes.get(f"component:{component_id}")
        if comp_node is not None:
            parts.append(f"## Linked component: `component:{component_id}`")
            parts.append(
                "This event's source maps to a control-graph component. "
                "Its current diagnostic context:"
            )
            parts.append("")
            parts += _component_context_lines(comp_node)
            parts.append("")
        else:
            parts.append(f"## Linked component: `{component_id}`")
            parts.append("(not found in the current control graph)")
            parts.append("")

    parts.append("## What you can do")
    parts.append(
        "Diagnose the root cause and fix the issue if possible. Use the "
        "Bash, Read, and Grep tools to inspect the log file, configuration, "
        "and relevant source. Lead with the fix, not the architecture."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Per-kind brief builders
# ---------------------------------------------------------------------------

def _requirement_brief(node) -> str:
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY
    raw_id = node.id.removeprefix("req:")
    req = REQUIREMENT_REGISTRY.get(raw_id)
    fix_hint = req.fix_hint if req else "(no fix hint registered)"
    severity = req.severity if req else "unknown"
    component = req.component if req else None
    fix_kind = req.fix_kind if req else "none"

    parts = [
        "You are helping the user investigate and fix a work-buddy requirement.",
        "",
        f"## Requirement: `{raw_id}`",
        f"**Description:** {node.label}",
        f"**Severity:** {severity}  ·  **Current state:** `{node.effective_state}`",
    ]
    if component:
        parts.append(f"**Owning component:** `component:{component}`")
    parts.append("")
    parts.append("## Why it's currently in this state")
    parts.append(node.status_reason or "(no reason captured)")
    parts.append("")
    parts.append("## Fix guidance")
    parts.append(fix_hint or "(no fix hint registered)")
    parts.append("")
    parts.append(f"**Fix kind:** `{fix_kind}`  — " + _fix_kind_explanation(fix_kind))
    parts.append("")
    parts.append("## What you can do")
    parts.append(
        "1. Use `mcp__work-buddy__wb_run(\"agent_docs\", {\"path\": "
        "\"status/setup-help-directions\", \"depth\": \"full\"})` to load "
        "the standard diagnose/fix protocol."
    )
    parts.append(
        "2. Inspect related context with the Bash, Read, and Grep tools — "
        "config files, the Obsidian vault, .env, etc."
    )
    parts.append(
        "3. Walk the user through any manual steps. When done, ask them to "
        "click the **Re-check** button on this requirement in the dashboard "
        "Settings tab (or refresh the page) to confirm it now passes."
    )
    parts.append("")
    parts.append(
        "Lead with the fix, not the architecture. Be concrete about which "
        "files to edit, which commands to run, and what success looks like."
    )
    return "\n".join(parts)


def _component_context_lines(node) -> list[str]:
    """State + diagnostic + linked-requirements + affected-capabilities
    lines for a component node.

    Shared by ``_component_brief`` (the ? help button) and
    ``build_help_brief_for_event`` (the Investigate button when the event
    links to a component) so both surfaces describe a component the same
    way. Does NOT include the intro line or the "what you can do" footer —
    callers add those.
    """
    from work_buddy.health.diagnostics import DiagnosticRunner
    diag_summary = ""
    try:
        runner = DiagnosticRunner()
        diag = runner.diagnose(node.component_id or node.id)
        diag_summary = _format_diagnostic(diag)
    except Exception as exc:
        diag_summary = f"(DiagnosticRunner unavailable: {exc})"

    parts = [
        f"**Display name:** {node.label}",
        f"**Current state:** `{node.effective_state}`",
        f"**Preference:** `{node.preference}`",
    ]
    if node.status_reason:
        parts.append(f"**Reason:** {node.status_reason}")
    if node.blocking_issues:
        parts.append(f"**Blocking issues:** {', '.join(node.blocking_issues)}")
    parts.append("")
    parts.append("## Diagnostic check sequence")
    parts.append(diag_summary)
    parts.append("")
    parts.append("## Linked requirements")
    if node.requirement_ids:
        for rid in node.requirement_ids:
            parts.append(f"  - `{rid}`")
    else:
        parts.append("  (none)")
    parts.append("")
    parts.append(
        f"## Capabilities affected by this component "
        f"({len(node.affects_capabilities)})"
    )
    if node.affects_capabilities:
        sample = node.affects_capabilities[:8]
        parts.append("  " + ", ".join(f"`{c}`" for c in sample))
        if len(node.affects_capabilities) > 8:
            parts.append(f"  …and {len(node.affects_capabilities) - 8} more.")
    else:
        parts.append("  (none — no capabilities currently list this as a `requires`)")
    return parts


def _component_brief(node) -> str:
    """Brief for a component — bundles DiagnosticRunner output."""
    parts = [
        "You are helping the user diagnose and fix a work-buddy component.",
        "",
        f"## Component: `{node.id}`",
    ]
    parts += _component_context_lines(node)
    parts.append("")
    parts.append("## What you can do")
    parts.append(
        "1. Load the diagnose protocol: "
        "`mcp__work-buddy__wb_run(\"agent_docs\", "
        "{\"path\": \"status/setup-help-directions\", \"depth\": \"full\"})`."
    )
    parts.append(
        "2. For each linked requirement that's currently failing, walk the "
        "user through fixing it (or attempt a programmatic fix if available)."
    )
    parts.append(
        "3. After each fix, ask the user to refresh the dashboard Settings "
        "tab to confirm the component returns to `ok`."
    )
    parts.append("")
    parts.append("Lead with the fix, not the architecture.")
    return "\n".join(parts)


def _grouping_brief(node, nodes) -> str:
    """Brief for a domain or subsystem — list problematic descendants."""
    bad_states = {"blocked", "unconfigured", "degraded", "unknown"}
    descendants = []
    visited = set()
    stack = [node.id]
    while stack:
        nid = stack.pop()
        if nid in visited:
            continue
        visited.add(nid)
        for other in nodes.values():
            if nid in (other.grouping_parents or []):
                if other.kind in ("component", "requirement", "subsystem"):
                    if other.effective_state in bad_states:
                        descendants.append(other)
                stack.append(other.id)

    parts = [
        f"You are helping the user investigate problems in a work-buddy "
        f"{node.kind}.",
        "",
        f"## {node.kind.title()}: `{node.id}`",
        f"**Label:** {node.label}",
        f"**Current state:** `{node.effective_state}`",
        f"**Description:** {node.description}",
        "",
    ]
    if descendants:
        parts.append(f"## Problematic descendants ({len(descendants)})")
        for d in descendants[:20]:
            parts.append(
                f"  - `{d.id}` [{d.effective_state}] {d.label}"
                + (f" — {d.status_reason}" if d.status_reason else "")
            )
        if len(descendants) > 20:
            parts.append(f"  …and {len(descendants) - 20} more.")
    else:
        parts.append("## Problematic descendants")
        parts.append("  (none — but this node still rolls up to a non-ok state; check the rollup logic)")
    parts.append("")
    parts.append(
        "## What you can do\n"
        "Walk the user through each problematic descendant in turn. For "
        "each, decide if it can be fixed programmatically (cheaper) or "
        "needs the user to take action. Use the dashboard Settings tab "
        "as your reference UI."
    )
    return "\n".join(parts)


def _capability_brief(node) -> str:
    """Brief for a capability — explain its dep chain."""
    parts = [
        "You are helping the user investigate a work-buddy capability that "
        "isn't fully usable.",
        "",
        f"## Capability: `{node.id}`",
        f"**Description:** {node.description}",
        f"**Current state:** `{node.effective_state}`",
        "",
    ]
    if node.dependencies:
        parts.append("## Dependencies")
        for e in node.dependencies:
            parts.append(f"  - `{e.target_id}` ({e.hardness})")
    else:
        parts.append("## Dependencies\n  (none — capability is a leaf)")
    parts.append("")
    parts.append(
        "## What you can do\n"
        "If this capability is degraded/blocked, follow its dependency chain "
        "until you find the underlying problem. Use the Settings tab's "
        "drill-down on the failing dependency."
    )
    return "\n".join(parts)


def _generic_brief(node) -> str:
    return (
        f"You are helping the user investigate a work-buddy node "
        f"(`{node.id}`, kind={node.kind}). Current state: "
        f"`{node.effective_state}`. Reason: {node.status_reason or '(none)'}. "
        f"Open the dashboard Settings tab for context and walk them through "
        f"the fix."
    )


def _unknown_node_brief(node_id: str) -> str:
    return (
        f"You were asked to help with work-buddy node `{node_id}`, but it "
        f"is not currently in the control graph. Either it was just removed "
        f"or the id is misspelled. Ask the user what they were trying to "
        f"investigate."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_sidecar_log_path() -> str | None:
    """Return the most recent agent session's sidecar ``work_buddy.log``.

    Scans ``data/agents/*sidecar*/logs/work_buddy.log`` newest-first.
    Returns ``None`` when no such file exists. Extracted from the old
    inline logic in ``service.api_investigate`` so the event-brief
    builder can reuse it.
    """
    try:
        from work_buddy.paths import data_dir
        agents_dir = data_dir("agents")
        if not agents_dir.exists():
            return None
        for d in sorted(agents_dir.iterdir(), reverse=True):
            if "sidecar" in d.name:
                candidate = d / "logs" / "work_buddy.log"
                if candidate.exists():
                    return str(candidate)
    except Exception as exc:
        log.warning("Could not resolve sidecar log path: %s", exc)
    return None


def _fix_kind_explanation(kind: str) -> str:
    return {
        "none": "no automated fix; manual only.",
        "programmatic": "the dashboard can apply this with a single click; "
                        "if you're here, the user wanted human guidance instead.",
        "input_required": "the dashboard form needs a value; help the user "
                          "decide what to enter, then they can apply via the form.",
        "agent_handoff": "this fix was always intended to need a Claude Code "
                         "session — that's you. Take it from here.",
    }.get(kind, f"unrecognized fix_kind={kind!r}")


def _format_diagnostic(diag) -> str:
    """Render a DiagnosticResult into a markdown summary."""
    out: list[str] = []
    status = getattr(diag, "status", "unknown")
    out.append(f"**Status:** {status}")
    steps = getattr(diag, "steps_run", [])
    if steps:
        out.append("")
        out.append("**Steps:**")
        for step in steps:
            ok = getattr(step, "ok", False)
            icon = "✓" if ok else "✗"
            desc = getattr(step, "description", "?")
            detail = getattr(step, "detail", "")
            out.append(f"  - {icon} {desc} — {detail}")
    root = getattr(diag, "root_cause", "")
    if root:
        out.append("")
        out.append(f"**Root cause:** {root}")
    fix = getattr(diag, "fix_suggestion", "")
    if fix:
        out.append("")
        out.append("**Fix suggestion:**")
        out.append("```")
        out.append(fix)
        out.append("```")
    return "\n".join(out)


def spawn_help_agent(node_id: str) -> dict[str, Any]:
    """Build the brief and launch a Claude Code session.

    Called by ``POST /api/control/help/<node_id>``. Same launch pattern
    as ``fix_runner._spawn_fix_agent`` (consent grant + session begin).
    """
    from work_buddy.consent import grant_consent
    from work_buddy.session_launcher import begin_session

    brief = build_help_brief(node_id)
    grant_consent("sidecar:remote_session_launch", mode="once")

    try:
        # Interactive desktop — no remote-control bridge. See
        # fix_runner._spawn_fix_agent for the same rationale.
        result = begin_session(prompt=brief, remote_control=False)
    except Exception as exc:
        log.exception("Failed to spawn help agent for %s", node_id)
        return {"ok": False, "detail": f"Could not launch help session: {exc}"}

    if result.get("status") != "ok":
        return {"ok": False, "detail": result.get("error", "Help launch failed.")}

    return {
        "ok": True,
        "detail": (
            "Help session launched — see the new terminal window for guidance."
        ),
        "session_id": result.get("session_id", ""),
        "pid": result.get("pid"),
        "message": result.get("message", "Session started."),
    }
