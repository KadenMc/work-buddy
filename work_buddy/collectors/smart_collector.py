"""Collect semantically relevant vault context via Smart Environment.

Unlike other collectors which read files from disk, this collector
uses Smart Connections' embedding index to find vault content that
is semantically relevant to the user's current work — even content
that hasn't been recently modified.

Requires Obsidian to be running with the Smart Connections plugin.
Degrades gracefully if unavailable.
"""

from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def collect(cfg: dict[str, Any]) -> str:
    """Collect Smart Environment context.

    Gathers:
    1. Environment health status (readiness, memory, model info)
    2. Semantic matches for active contracts (if any)
    3. Workspace context (active file + semantic neighbors)
    4. Recent SmartEnv events (embedding activity, errors)

    Returns markdown summary. Returns a "not available" message
    if Obsidian/SmartEnv is not reachable.
    """
    from work_buddy.obsidian import bridge

    try:
        available = bridge.is_available()
    except Exception:
        available = False

    if not available:
        logger.info("Obsidian bridge not available — skipping Smart collection")
        return _unavailable_report("Obsidian bridge not reachable")

    # Check SmartEnv readiness
    try:
        from work_buddy.obsidian.smart import check_ready
        status = check_ready()
        if not status.get("ready"):
            logger.info("SmartEnv not ready (state: %s) — collecting partial", status.get("state"))
            return _partial_report(status)
    except Exception as e:
        logger.warning("SmartEnv check_ready failed: %s", e)
        return _unavailable_report(f"SmartEnv check failed: {e}")

    lines = [
        "# Smart Context",
        "",
        f"*SmartEnv: {status.get('state', '?')} | "
        f"{status.get('sources_count', '?'):,} sources, "
        f"{status.get('blocks_count', '?'):,} blocks | "
        f"Pro: {status.get('is_pro', '?')}*",
        "",
    ]

    # Memory status
    mem = status.get("performance_memory", {})
    if mem:
        heap_pct = round(mem.get("js_heap_used_mb", 0) / max(mem.get("js_heap_limit_mb", 1), 1) * 100)
        if heap_pct > 85:
            lines.append(f"**Heap warning:** {heap_pct}% ({mem.get('js_heap_used_mb')}MB / {mem.get('js_heap_limit_mb')}MB)")
            lines.append("")

    # Workspace context
    lines.extend(_collect_workspace())

    # Contract-based semantic search
    lines.extend(_collect_contract_context(cfg))

    # Recent Smart events
    lines.extend(_collect_recent_events())

    return "\n".join(lines)


def _collect_workspace() -> list[str]:
    """Get workspace state + semantic neighbors of active file."""
    lines = []
    try:
        from work_buddy.obsidian.smart import get_workspace_context
        ctx = get_workspace_context(semantic_limit=5)
        ws = ctx.get("workspace", {})
        active = ws.get("active_file")
        related = ctx.get("related", [])

        if active:
            lines.append("## Active File")
            lines.append(f"- `{active}`")
            if related:
                lines.append("")
                lines.append("**Semantically related:**")
                for r in related:
                    lines.append(f"- `{r['key']}` (score: {r['score']:.3f})")
            lines.append("")
    except Exception as e:
        logger.debug("Workspace context failed: %s", e)

    return lines


def _collect_contract_context(cfg: dict[str, Any]) -> list[str]:
    """Search vault for content relevant to active contracts."""
    lines = []

    try:
        from pathlib import Path
        from work_buddy.contracts import load_all_contracts, get_contracts_dir
        contracts_dir = get_contracts_dir()
        contracts = load_all_contracts(contracts_dir)
        active = [c for c in contracts if c.get("status") == "active"]

        if not active:
            return lines

        from work_buddy.obsidian.smart import semantic_search

        lines.append("## Contract-Relevant Vault Content")
        lines.append("")

        for contract in active[:3]:  # Cap at 3 active contracts
            title = contract.get("title", contract.get("path", Path()).stem)
            claim = contract.get("sections", {}).get("Claim", "")
            query = claim if claim else title

            if not query or len(query.strip()) < 5:
                continue

            try:
                results = semantic_search(query, limit=5, collection="smart_blocks")
                if results:
                    lines.append(f"### {title}")
                    lines.append(f"*Query: \"{query[:80]}\"*")
                    lines.append("")
                    for r in results:
                        lines.append(f"- `{r['key']}` ({r['score']:.3f})")
                    lines.append("")
            except Exception as e:
                logger.debug("Semantic search for contract '%s' failed: %s", title, e)

    except Exception as e:
        logger.debug("Contract context collection failed: %s", e)

    return lines


def _collect_recent_events() -> list[str]:
    """Get recent SmartEnv events for awareness."""
    lines = []
    try:
        from work_buddy.obsidian.smart.diagnostics import read_event_logs
        events = read_event_logs(limit=10)

        error_ct = events.get("error_count", 0)
        if error_ct > 0:
            lines.append("## Smart Environment Events")
            lines.append(f"**Errors:** {error_ct} total")
            lines.append("")
            for e in events.get("entries", [])[:5]:
                if "error" in e.get("key", ""):
                    lines.append(f"- `{e['key']}` ({e.get('ct', 0)}x, last: {e.get('last_at', '?')})")
            lines.append("")

    except Exception as e:
        logger.debug("Event log collection failed: %s", e)

    return lines


def _unavailable_report(reason: str) -> str:
    return (
        "# Smart Context\n\n"
        f"*Not available: {reason}*\n\n"
        "Smart context requires Obsidian running with Smart Connections plugin.\n"
    )


def _partial_report(status: dict) -> str:
    state = status.get("state", "unknown")
    sources = status.get("sources_count", 0)
    blocks = status.get("blocks_count", 0)
    return (
        "# Smart Context\n\n"
        f"*SmartEnv loading (state: {state}) — "
        f"{sources:,} sources, {blocks:,} blocks loaded so far*\n\n"
        "Semantic search not available until loading completes. "
        "Other collectors still ran normally.\n"
    )
