"""SmartEnv-specific diagnostics: events, heap pressure, embed queue, health.

Builds on top of functions in env.py — composes them into higher-level
diagnostic capabilities. Does NOT move or duplicate existing functions.
"""

from typing import Any

from work_buddy.obsidian.smart.env import (
    _run_js,
    check_ready,
    monitor_model_config,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ── Heap Pressure ────────────────────────────────────────────────


def heap_pressure() -> dict[str, Any]:
    """Check V8 heap pressure with threshold classification.

    Thresholds:
    - <75%: ok
    - 75-85%: elevated
    - 85-93%: warning
    - >93%: critical

    Returns:
        Dict with heap_used_mb, heap_limit_mb, heap_percent, rss_mb, status.
    """
    return _run_js("heap_pressure.js", timeout=10)


# ── Embedding Queue ──────────────────────────────────────────────


def embed_queue_status() -> dict[str, Any]:
    """Check the embedding queue state on smart_sources and smart_blocks.

    Returns:
        Dict with sources_queue_size, blocks_queue_size, total_queued,
        process_available, model_state, model_loaded.
    """
    return _run_js("embed_queue_status.js", timeout=10)


# ── Event Logs ───────────────────────────────────────────────────


def read_event_logs(
    category: str | None = None,
    limit: int = 50,
    unseen_only: bool = False,
) -> dict[str, Any]:
    """Read SmartEnv event logs (same data as the Status view 'Events & notifications').

    47 event types across 22 categories including embedding, sources,
    connect_pro, context, lookup, settings, etc.

    Args:
        category: Filter by category prefix (e.g. 'embedding', 'connect_pro'). None = all.
        limit: Max entries to return.
        unseen_only: If True, only return unseen notifications.

    Returns:
        Dict with total_types, categories, error_count, unseen_count, entries.
        Each entry: {key, category, ct, first_at, last_at, sources}.
    """
    return _run_js(
        "event_logs.js",
        {
            "__UNSEEN_ONLY__": "true" if unseen_only else "false",
            "__CATEGORY__": category or "all",
            "__LIMIT__": str(limit),
        },
        timeout=15,
    )


def connect_pro_errors() -> dict[str, Any]:
    """Check for Smart Connect Pro tunnel errors.

    These are the "All tunnels dead" errors that fire when the tunnel
    to connect.smartconnections.app fails. Harmless if you're not
    using the ChatGPT vault bridge, but noisy.

    Returns:
        Dict with error_count, has_errors, details.
    """
    try:
        events = read_event_logs(category="connect_pro", limit=10)
        error_entries = [e for e in events.get("entries", []) if "error" in e.get("key", "")]
        total_errors = sum(e.get("ct", 0) for e in error_entries)
        return {
            "error_count": total_errors,
            "has_errors": total_errors > 0,
            "entries": error_entries,
        }
    except Exception as e:
        return {"error_count": -1, "has_errors": False, "error": str(e)}


# ── Unified Smart Health Report ──────────────────────────────────


def smart_health_report() -> str:
    """Generate a unified markdown health report for the Smart Environment.

    Composes check_ready, heap_pressure, embed_queue_status,
    monitor_model_config, event_logs, and connect_pro_errors.
    Degrades gracefully — shows whatever data is available.
    """
    lines = ["## Smart Environment Health", ""]

    # Readiness
    try:
        ready = check_ready()
        state = ready.get("state", "unknown")
        src_count = ready.get("sources_count", "?")
        blk_count = ready.get("blocks_count", "?")
        plugins = f"{ready.get('plugins_loaded', '?')}/{ready.get('plugins_total', '?')}"
        lines.append(f"**State:** {state} | **Pro:** {ready.get('is_pro', '?')}")
        lines.append(f"**Index:** {src_count:,} sources, {blk_count:,} blocks" if isinstance(src_count, int) else f"**Index:** {src_count} sources, {blk_count} blocks")
        lines.append(f"**Plugins:** {plugins} loaded | **Iframe:** {'ready' if ready.get('iframe_ready') else 'NOT ready'}")
    except Exception as e:
        lines.append(f"**Readiness check failed:** {e}")

    # Heap
    lines.append("")
    try:
        hp = heap_pressure()
        status_emoji = {"ok": "", "elevated": "", "warning": "**", "critical": "**"}.get(hp.get("status", ""), "")
        status_suffix = {"warning": "**", "critical": "**"}.get(hp.get("status", ""), "")
        lines.append(
            f"**Heap:** {hp.get('heap_used_mb', '?')} / {hp.get('heap_limit_mb', '?')} MB "
            f"({hp.get('heap_percent', '?')}%) — "
            f"{status_emoji}{hp.get('status', 'unknown').upper()}{status_suffix}"
        )
        if hp.get("rss_mb"):
            lines.append(f"**RSS:** {hp['rss_mb']} MB")
    except Exception as e:
        lines.append(f"**Heap check failed:** {e}")

    # Embedding model
    lines.append("")
    try:
        model = monitor_model_config()
        lines.append(
            f"**Model:** {model.get('model_key', '?')} "
            f"({model.get('dims', '?')}d, {model.get('provider_key', '?')})"
        )
        lines.append(f"**Adapter:** {model.get('adapter_type', '?')} | State: {model.get('state', '?')}")
    except Exception as e:
        lines.append(f"**Model check failed:** {e}")

    # Embed queue
    try:
        eq = embed_queue_status()
        total = eq.get("total_queued", 0)
        if total > 0:
            lines.append(f"**Embed queue:** {total} items pending (sources: {eq.get('sources_queue_size', 0)}, blocks: {eq.get('blocks_queue_size', 0)})")
        else:
            lines.append("**Embed queue:** idle (0 pending)")
    except Exception as e:
        lines.append(f"**Embed queue check failed:** {e}")

    # Event log errors
    lines.append("")
    try:
        events = read_event_logs(limit=5)
        error_ct = events.get("error_count", 0)
        unseen = events.get("unseen_count", 0)
        total_types = events.get("total_types", 0)
        lines.append(f"**Events:** {total_types} types | {error_ct} total errors | {unseen} unseen")

        # Recent entries
        recent = events.get("entries", [])[:5]
        if recent:
            lines.append("**Recent events:**")
            for e in recent:
                ct = e.get("ct", 0)
                lines.append(f"- `{e.get('key', '?')}` ({ct}x)")
    except Exception as e:
        lines.append(f"**Event log check failed:** {e}")

    # Connect Pro errors
    try:
        cpe = connect_pro_errors()
        if cpe.get("has_errors"):
            lines.append(f"\n**Connect Pro errors:** {cpe['error_count']}x 'All tunnels dead' (tunnel to smartconnections.app)")
    except Exception:
        pass

    return "\n".join(lines)
