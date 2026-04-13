"""Obsidian-level diagnostics: bridge health, plugins, log parsing, crash detection.

This module does NOT import from work_buddy.obsidian.smart — it only knows
about the bridge, plugins, and Obsidian itself.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_JS_DIR = Path(__file__).parent / "_js"


def _load_js(name: str) -> str:
    """Load a JS snippet from the obsidian-level _js directory."""
    return (_JS_DIR / name).read_text(encoding="utf-8")


# ── Log Reading ──────────────────────────────────────────────────


def _obsidian_log_path() -> Path:
    """Resolve the Obsidian main process log file path."""
    from work_buddy.compat import obsidian_log_path
    return obsidian_log_path()


def read_obsidian_log(
    tail_lines: int = 200,
    filter_pattern: str | None = None,
) -> list[dict[str, str]]:
    """Read recent entries from the Obsidian main process log.

    Uses reverse-seek tail reading to avoid loading the entire file.
    The log contains startup, update checks, and version info — NOT
    plugin console output (that requires console capture via eval_js).

    Args:
        tail_lines: Number of lines to read from the end (default 200).
        filter_pattern: Optional regex to filter entries.

    Returns:
        List of {timestamp, message, raw} dicts, oldest first.
    """
    log_path = _obsidian_log_path()
    if not log_path.exists():
        logger.debug("Obsidian log not found at %s", log_path)
        return []

    # Efficient tail read: seek from end in chunks
    lines: list[str] = []
    chunk_size = 8192
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)  # seek to end
            remaining = f.tell()
            buffer = b""

            while remaining > 0 and len(lines) < tail_lines + 1:
                read_size = min(chunk_size, remaining)
                remaining -= read_size
                f.seek(remaining)
                buffer = f.read(read_size) + buffer
                lines = buffer.decode("utf-8", errors="replace").splitlines()

        # Take only the last tail_lines
        lines = lines[-tail_lines:]
    except OSError as e:
        logger.warning("Failed to read obsidian.log: %s", e)
        return []

    # Parse entries: format is "YYYY-MM-DD HH:MM:SS message"
    ts_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.+)$")
    regex = re.compile(filter_pattern, re.IGNORECASE) if filter_pattern else None

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = ts_pattern.match(line)
        if m:
            entry = {"timestamp": m.group(1), "message": m.group(2), "raw": line}
        else:
            entry = {"timestamp": "", "message": line, "raw": line}

        if regex and not regex.search(line):
            continue
        entries.append(entry)

    return entries


def detect_restarts(since_hours: int = 24) -> list[dict[str, Any]]:
    """Detect Obsidian restarts by finding 'Loading app package' log entries.

    Args:
        since_hours: Only return restarts within this many hours.

    Returns:
        List of {timestamp, message, version} dicts.
    """
    entries = read_obsidian_log(tail_lines=2000, filter_pattern=r"Loading.*app package")
    cutoff = datetime.now() - timedelta(hours=since_hours)

    restarts = []
    for e in entries:
        ts_str = e.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                if ts < cutoff:
                    continue
            except ValueError:
                pass

        # Extract version from asar filename
        version = None
        ver_match = re.search(r"obsidian-(\d+\.\d+\.\d+)\.asar", e.get("message", ""))
        if ver_match:
            version = ver_match.group(1)

        restarts.append({
            "timestamp": ts_str,
            "message": e["message"],
            "version": version,
        })

    return restarts


# ── Plugin Inventory ─────────────────────────────────────────────


def plugin_inventory_runtime() -> list[dict[str, str]]:
    """List all loaded Obsidian plugins with versions via the live runtime.

    Falls back to disk-based plugin reading if the bridge is unavailable.

    Returns:
        List of {id, name, version, author} dicts.
    """
    if bridge.is_available():
        try:
            js = _load_js("plugin_inventory.js")
            result = bridge.eval_js(js, timeout=15)
            if isinstance(result, dict) and "plugins" in result:
                return result["plugins"]
        except Exception as e:
            logger.debug("Runtime plugin inventory failed: %s", e)

    # Fallback: disk-based
    logger.debug("Falling back to disk-based plugin inventory")
    from work_buddy.obsidian.plugins import installed_plugins
    cfg = load_config()
    vault_root = Path(cfg["vault_root"])
    installed = installed_plugins(vault_root)
    return [
        {"id": pid, "name": info.get("name", pid), "version": info.get("version", "?"), "author": "?"}
        for pid, info in installed.items()
    ]


# ── Service Checks ───────────────────────────────────────────────


def check_local_rest_api() -> dict[str, Any]:
    """Check if the Obsidian Local REST API (port 27124) is reachable.

    Returns:
        Dict with 'available', 'port', and optionally 'error'.
    """
    cfg = load_config()
    vault_root = Path(cfg["vault_root"])
    port = cfg.get("obsidian", {}).get("api_port", 27124)

    try:
        from work_buddy.obsidian.commands import ObsidianCommands
        cmds = ObsidianCommands(vault_root, port=port)
        available = cmds.is_available()
        return {"available": available, "port": port}
    except Exception as e:
        return {"available": False, "port": port, "error": str(e)}


# ── Crash Detection ──────────────────────────────────────────────


def crash_detection_heuristic() -> dict[str, Any]:
    """Detect probable Obsidian crashes by combining bridge state and restart history.

    Heuristic: 3+ restarts in 1 hour = instability suspected.

    Returns:
        Dict with bridge_available, recent_restarts, crash_suspected, evidence.
    """
    available = bridge.is_available()
    restarts = detect_restarts(since_hours=1)

    crash_suspected = False
    evidence = ""

    if not available and restarts:
        crash_suspected = True
        evidence = f"Bridge down + {len(restarts)} restart(s) in last hour"
    elif len(restarts) >= 3:
        crash_suspected = True
        evidence = f"{len(restarts)} restarts in last hour (instability)"
    elif not available:
        evidence = "Bridge unavailable (Obsidian may be closed or crashed)"

    return {
        "bridge_available": available,
        "recent_restarts": restarts,
        "restart_count_1h": len(restarts),
        "crash_suspected": crash_suspected,
        "evidence": evidence,
    }


# ── Unified Health Report ────────────────────────────────────────


def obsidian_health_report() -> str:
    """Generate a unified markdown health report for Obsidian.

    Covers bridge status, Local REST API, plugin count, memory, restarts,
    and crash detection. Degrades gracefully when bridge is down.
    """
    lines = ["## Obsidian Health", ""]

    # Bridge
    bridge_up = bridge.is_available()
    lines.append(f"**Bridge:** {'online' if bridge_up else 'OFFLINE'}")

    # Local REST API
    try:
        api = check_local_rest_api()
        api_status = "reachable" if api["available"] else "unreachable"
        lines.append(f"**Local REST API:** {api_status} (port {api['port']})")
    except Exception:
        lines.append("**Local REST API:** check failed")

    # Plugins
    try:
        plugins = plugin_inventory_runtime()
        lines.append(f"**Plugins:** {len(plugins)} loaded")
    except Exception:
        lines.append("**Plugins:** count unavailable")

    # Memory (requires bridge)
    if bridge_up:
        try:
            mem = bridge.eval_js("""
                const nm = process.memoryUsage();
                const pm = performance.memory || {};
                return {
                    rss_mb: Math.round(nm.rss / 1048576),
                    heap_used_mb: Math.round(nm.heapUsed / 1048576),
                    heap_total_mb: Math.round(nm.heapTotal / 1048576),
                    heap_limit_mb: pm.jsHeapSizeLimit ? Math.round(pm.jsHeapSizeLimit / 1048576) : null
                };
            """, timeout=10)
            if mem:
                heap_str = f"{mem['heap_used_mb']}/{mem['heap_total_mb']} MB"
                if mem.get("heap_limit_mb"):
                    pct = round(mem["heap_used_mb"] / mem["heap_limit_mb"] * 100)
                    heap_str += f" ({pct}% of {mem['heap_limit_mb']} MB limit)"
                lines.append(f"**Memory:** RSS {mem['rss_mb']} MB | Heap {heap_str}")
        except Exception:
            lines.append("**Memory:** measurement failed")
    else:
        lines.append("**Memory:** unavailable (bridge offline)")

    # Restarts
    restarts = detect_restarts(since_hours=24)
    if restarts:
        latest = restarts[-1]
        lines.append(
            f"**Restarts (24h):** {len(restarts)} "
            f"(latest: {latest['timestamp']}, v{latest.get('version', '?')})"
        )
    else:
        lines.append("**Restarts (24h):** 0")

    # Crash heuristic
    crash = crash_detection_heuristic()
    if crash["crash_suspected"]:
        lines.append(f"**Crash suspected:** Yes — {crash['evidence']}")

    return "\n".join(lines)
