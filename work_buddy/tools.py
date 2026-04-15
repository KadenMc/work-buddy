"""Dependency-aware feature toggle system for work-buddy.

This module provides:
- **Tool probes** — lightweight checks for external tool/service availability
- **@requires_tool** decorator — gates functions on tool availability (modeled
  after @requires_consent in consent.py)
- **Registry filtering** — unavailable tools cause dependent capabilities to be
  excluded from wb_search results
- **Config-driven toggles** — ``tools.<id>.enabled: false`` in config disables
  a tool without probing

Three-layer dependency model:
    Layer 1: Tools (Obsidian bridge, Chrome extension, Hindsight, Telegram, etc.)
      → missing tool disables...
    Layer 2: Capabilities (journal_write, chrome_activity, memory_read, etc.)
      → missing capability disables...
    Layer 3: Workflows (morning-routine, chrome-triage, collect-and-orient, etc.)

Flow:
    1. ``_register_default_probes()`` registers all built-in tool probes
    2. ``probe_all()`` runs probes at registry build time (respects config toggles)
    3. Capabilities with ``requires=[...]`` are filtered from the registry
    4. ``@requires_tool`` decorator provides runtime safety as belt-and-suspenders
    5. ``feature_status`` MCP capability shows what's available/disabled and why
"""

import functools
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from work_buddy.paths import resolve

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ToolProbe:
    """Describes how to check whether an external tool/service is available.

    Attributes:
        id: Short identifier, e.g. "obsidian", "hindsight".
        display_name: Human-readable name, e.g. "Obsidian Bridge".
        probe_fn: Callable returning True if the tool is reachable.
            Must complete within ``probe_timeout`` seconds.
        config_key: Dot-separated config path for enabled toggle,
            e.g. "tools.obsidian.enabled". If the config value is
            explicitly ``false``, the probe is skipped and the tool
            is marked unavailable.
        depends_on: Other tool IDs that must be available first.
            If any dependency is unavailable, this probe short-circuits
            to False without running.
        reason_when_missing: Explanation shown when the tool is unavailable.
        probe_timeout: Max seconds for the probe function (default 2).
    """

    id: str
    display_name: str
    probe_fn: Callable[[], bool]
    config_key: str | None = None
    depends_on: list[str] = field(default_factory=list)
    reason_when_missing: str = ""
    probe_timeout: float = 2.0


class ToolUnavailable(Exception):
    """Raised when a capability requires a tool that is not available.

    Mirrors ``ConsentRequired`` from consent.py — the gateway catches this
    and returns a structured JSON error instead of crashing.

    Attributes:
        tool_id: Which tool is missing.
        display_name: Human-readable tool name.
        reason: Why it's unavailable.
    """

    def __init__(self, tool_id: str, display_name: str, reason: str):
        self.tool_id = tool_id
        self.display_name = display_name
        self.reason = reason
        super().__init__(
            f"ToolUnavailable: '{display_name}' ({tool_id})\n"
            f"Reason: {reason}\n"
            f"\n"
            f"This capability requires the '{display_name}' integration.\n"
            f"Run wb_run('feature_status') for details."
        )


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_TOOL_PROBES: dict[str, ToolProbe] = {}
_TOOL_STATUS: dict[str, dict[str, Any]] | None = None  # {id: {available, probe_ms, reason, config_enabled}}

# Populated during registry filtering — maps capability name → list of missing tool IDs.
# Read by the feature_status diagnostic capability.
DISABLED_CAPABILITIES: dict[str, list[str]] = {}

# ---------------------------------------------------------------------------
# Probe registration and execution
# ---------------------------------------------------------------------------


def register_tool_probe(probe: ToolProbe) -> None:
    """Register a tool probe. Overwrites any existing probe with the same ID."""
    _TOOL_PROBES[probe.id] = probe


def _get_config_enabled(cfg: dict, config_key: str | None) -> bool | None:
    """Resolve a dot-separated config key. Returns None if not set."""
    if not config_key:
        return None
    parts = config_key.split(".")
    node = cfg
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, bool):
        return node
    return None


_TOOL_STATUS_FILE = resolve("runtime/tool-status")


def _persist_tool_status(results: dict[str, dict[str, Any]]) -> None:
    """Atomically write probe results to tool_status.json.

    Enables cross-process data sharing: MCP server writes, dashboard reads.
    Uses the same atomic-write pattern as sidecar_state.json.
    """
    import tempfile
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=_TOOL_STATUS_FILE.parent,
            prefix=".tool_status_",
            suffix=".tmp",
        )
        os.write(fd, json.dumps(results, indent=2).encode())
        os.close(fd)
        os.replace(tmp_path, _TOOL_STATUS_FILE)
    except Exception as exc:
        log.warning("Failed to persist tool_status.json: %s", exc)
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        try:
            os.unlink(tmp_path)
        except (OSError, UnboundLocalError):
            pass


def probe_all(force: bool = False) -> dict[str, dict[str, Any]]:
    """Run all registered probes and cache results.

    Returns a dict mapping tool_id → {available, probe_ms, reason, config_enabled}.
    Respects config-driven toggles: if ``tools.<id>.enabled`` is explicitly
    ``false``, the probe is skipped and the tool is marked unavailable.

    Probes are ordered so that dependencies run first (via ``depends_on``).
    """
    global _TOOL_STATUS
    if _TOOL_STATUS is not None and not force:
        return _TOOL_STATUS

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from work_buddy.config import load_config
    from work_buddy.health.preferences import is_wanted
    cfg = load_config()

    # Topological order: probes with no dependencies first
    ordered = _topo_sort_probes()

    results: dict[str, dict[str, Any]] = {}

    def _run_probe(probe: ToolProbe) -> tuple[str, dict[str, Any]]:
        entry: dict[str, Any] = {
            "available": False,
            "probe_ms": 0,
            "reason": "",
            "config_enabled": True,
        }
        # Check user preference — if explicitly unwanted, skip probe
        pref = is_wanted(probe.id)
        if pref is False:
            entry["config_enabled"] = False
            entry["reason"] = "User opted out (features.{}.wanted: false)".format(probe.id)
            entry["user_opted_out"] = True
            return probe.id, entry

        # Check config toggle
        config_val = _get_config_enabled(cfg, probe.config_key)
        if config_val is False:
            entry["config_enabled"] = False
            entry["reason"] = f"Disabled in config ({probe.config_key})"
            return probe.id, entry

        t0 = time.time()
        try:
            available = probe.probe_fn()
            elapsed_ms = (time.time() - t0) * 1000
            entry["available"] = bool(available)
            entry["probe_ms"] = round(elapsed_ms, 1)
            if not available:
                entry["reason"] = probe.reason_when_missing or f"{probe.display_name} not reachable"
        except Exception as exc:
            elapsed_ms = (time.time() - t0) * 1000
            entry["probe_ms"] = round(elapsed_ms, 1)
            entry["reason"] = f"Probe error: {exc}"
            log.debug("Tool probe %s failed: %s", probe.id, exc)

        return probe.id, entry

    # Phase 1: Run independent probes (no depends_on) in parallel
    independent = [p for p in ordered if not p.depends_on]
    dependent = [p for p in ordered if p.depends_on]

    with ThreadPoolExecutor(max_workers=len(independent) or 1) as pool:
        futures = {pool.submit(_run_probe, p): p for p in independent}
        for future in as_completed(futures):
            pid, entry = future.result()
            results[pid] = entry

    # Phase 2: Run dependent probes serially (they need parent results)
    for probe in dependent:
        missing_deps = [
            dep for dep in probe.depends_on
            if not results.get(dep, {}).get("available", False)
        ]
        if missing_deps:
            results[probe.id] = {
                "available": False,
                "probe_ms": 0,
                "reason": f"Dependency unavailable: {', '.join(missing_deps)}",
                "config_enabled": True,
            }
            continue

        pid, entry = _run_probe(probe)
        results[pid] = entry

    _TOOL_STATUS = results
    log.info(
        "Tool probes complete: %d available, %d unavailable",
        sum(1 for r in results.values() if r["available"]),
        sum(1 for r in results.values() if not r["available"]),
    )
    _persist_tool_status(results)
    return results


def reprobe_one(tool_id: str) -> dict[str, Any] | None:
    """Re-run a single tool probe and update the cached status file.

    Returns the fresh probe entry, or None if the tool_id is unknown.
    """
    _register_default_probes()
    probe = _TOOL_PROBES.get(tool_id)
    if probe is None:
        return None

    from work_buddy.config import load_config
    cfg = load_config()

    entry: dict[str, Any] = {
        "available": False,
        "probe_ms": 0,
        "reason": "",
        "config_enabled": True,
    }

    config_val = _get_config_enabled(cfg, probe.config_key)
    if config_val is False:
        entry["config_enabled"] = False
        entry["reason"] = f"Disabled in config ({probe.config_key})"
    else:
        t0 = time.time()
        try:
            available = probe.probe_fn()
            elapsed_ms = (time.time() - t0) * 1000
            entry["available"] = bool(available)
            entry["probe_ms"] = round(elapsed_ms, 1)
            if not available:
                entry["reason"] = probe.reason_when_missing or f"{probe.display_name} not reachable"
        except Exception as exc:
            elapsed_ms = (time.time() - t0) * 1000
            entry["probe_ms"] = round(elapsed_ms, 1)
            entry["reason"] = f"Probe error: {exc}"

    # Merge into cached status and persist
    global _TOOL_STATUS
    if _TOOL_STATUS is None:
        # Load existing from disk if not in memory
        if _TOOL_STATUS_FILE.exists():
            try:
                _TOOL_STATUS = json.loads(_TOOL_STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                _TOOL_STATUS = {}
        else:
            _TOOL_STATUS = {}
    _TOOL_STATUS[tool_id] = entry
    _persist_tool_status(_TOOL_STATUS)

    return entry


def _topo_sort_probes() -> list[ToolProbe]:
    """Sort probes so dependencies come before dependents."""
    visited: set[str] = set()
    result: list[ToolProbe] = []

    def _visit(probe_id: str) -> None:
        if probe_id in visited:
            return
        visited.add(probe_id)
        probe = _TOOL_PROBES.get(probe_id)
        if probe is None:
            return
        for dep in probe.depends_on:
            _visit(dep)
        result.append(probe)

    for pid in _TOOL_PROBES:
        _visit(pid)
    return result


def is_tool_available(tool_id: str) -> bool:
    """Check if a tool is available. Returns False if not probed or unknown."""
    if _TOOL_STATUS is None:
        return False
    entry = _TOOL_STATUS.get(tool_id)
    if entry is None:
        return False
    return entry["available"]


def invalidate_tool_status() -> None:
    """Clear cached tool status so next ``probe_all()`` re-runs probes."""
    global _TOOL_STATUS, _OBSIDIAN_PLUGINS
    _TOOL_STATUS = None
    _OBSIDIAN_PLUGINS = None
    DISABLED_CAPABILITIES.clear()


def get_tool_status() -> dict[str, Any]:
    """Return full diagnostic info for the feature_status capability."""
    status = _TOOL_STATUS or {}
    return {
        "tools": status,
        "disabled_capabilities": dict(DISABLED_CAPABILITIES),
        "summary": {
            "tools_available": sum(1 for r in status.values() if r["available"]),
            "tools_unavailable": sum(1 for r in status.values() if not r["available"]),
            "capabilities_disabled": len(DISABLED_CAPABILITIES),
        },
    }


# ---------------------------------------------------------------------------
# @requires_tool decorator
# ---------------------------------------------------------------------------


def requires_tool(*tool_ids: str):
    """Decorator that gates a function on tool availability.

    Modeled after ``@requires_consent``. The function body NEVER executes
    if any required tool is unavailable — ``ToolUnavailable`` is raised.

    Also sets ``wrapper._requires_tools`` so the registry builder can
    auto-extract tool requirements without an explicit ``requires=`` field.

    Usage::

        @requires_tool("obsidian")
        def journal_write(...):
            ...

        @requires_tool("obsidian", "google_calendar")
        def calendar_sync(...):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for tid in tool_ids:
                if not is_tool_available(tid):
                    probe = _TOOL_PROBES.get(tid)
                    status_entry = (_TOOL_STATUS or {}).get(tid, {})
                    reason = (
                        status_entry.get("reason")
                        or (probe.reason_when_missing if probe else "")
                        or f"Tool '{tid}' is not available"
                    )
                    raise ToolUnavailable(
                        tool_id=tid,
                        display_name=probe.display_name if probe else tid,
                        reason=reason,
                    )
            return fn(*args, **kwargs)
        wrapper._requires_tools = list(tool_ids)  # type: ignore[attr-defined]
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Built-in probe functions
# ---------------------------------------------------------------------------

def _port_open(port: int, timeout: float = 0.5) -> bool:
    """Quick TCP connect check — avoids slow urllib timeout on dead ports."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


_OBSIDIAN_PLUGINS: dict[str, bool] | None = None


def _probe_obsidian_plugins() -> dict[str, bool]:
    """Batch-check all integrated Obsidian plugins in a single eval call.

    Called once from _probe_obsidian when the bridge health check passes.
    Results are cached in _OBSIDIAN_PLUGINS for dependent probes.
    """
    global _OBSIDIAN_PLUGINS
    import http.client
    from work_buddy.config import load_config
    cfg = load_config()
    port = cfg.get("obsidian", {}).get("bridge_port", 27125)
    plugin_ids = ["smart-connections", "datacore", "google-calendar"]
    # Single eval call that returns an object mapping plugin_id → boolean
    checks = ", ".join(f"'{pid}': !!app.plugins.plugins['{pid}']" for pid in plugin_ids)
    code = f"return {{{checks}}}"
    body = json.dumps({"code": code})
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/eval", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        result = json.loads(data).get("result", {})
        _OBSIDIAN_PLUGINS = {pid: bool(result.get(pid, False)) for pid in plugin_ids}
    except Exception as exc:
        log.warning("Obsidian plugin batch check failed: %s", exc)
        _OBSIDIAN_PLUGINS = {pid: False for pid in plugin_ids}
    return _OBSIDIAN_PLUGINS


def _bridge_plugin_available(plugin_id: str) -> bool:
    """Check cached plugin availability from the batch probe."""
    if _OBSIDIAN_PLUGINS is None:
        return False
    return _OBSIDIAN_PLUGINS.get(plugin_id, False)


def _probe_obsidian() -> bool:
    """Check if Obsidian bridge is reachable and batch-probe plugins.

    Uses http.client (not urllib) to avoid issues with urllib inside
    asyncio.to_thread + winloop on Windows. Also batch-checks all
    integrated Obsidian plugins in a single eval call, caching results
    for dependent probes (smart_connections, datacore, google_calendar).

    Note: The bridge has documented latency spikes up to ~4s. We use a
    generous timeout to avoid false negatives.
    """
    import http.client
    from work_buddy.config import load_config
    cfg = load_config()
    port = cfg.get("obsidian", {}).get("bridge_port", 27125)
    if not _port_open(port):
        return False
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status != 200:
            return False
    except Exception:
        return False
    # Bridge is up — batch-check plugins while we have a warm connection
    _probe_obsidian_plugins()
    return True


def _probe_chrome_extension() -> bool:
    """Check if Chrome extension is active (recent ledger writes).

    The periodic snapshot alarm writes to the rolling ledger every 5 minutes.
    We consider the extension active if the ledger was updated within the
    last 10 minutes (2× the snapshot interval).
    """
    from work_buddy.paths import resolve
    ledger = resolve("chrome/ledger")
    if not ledger.exists():
        return False
    age = time.time() - ledger.stat().st_mtime
    return age < 600  # 10 minutes


def _probe_postgresql() -> bool:
    """Check if PostgreSQL is accepting connections on port 5432."""
    return _port_open(5432, timeout=2.0)


def _probe_hindsight() -> bool:
    """Check if Hindsight memory server is reachable."""
    import urllib.request
    from work_buddy.config import load_config
    cfg = load_config()
    base_url = cfg.get("hindsight", {}).get("base_url", "http://localhost:8888")
    # Extract port from URL for fast pre-check
    try:
        _port = int(base_url.rsplit(":", 1)[-1].rstrip("/"))
    except (ValueError, IndexError):
        _port = 8888
    if not _port_open(_port):
        return False
    try:
        req = urllib.request.Request(f"{base_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def _probe_telegram() -> bool:
    """Check if Telegram bot is running.

    The bot token is loaded from .env by the sidecar, not available in
    the MCP server's environment. So we check the sidecar service health
    (port probe) instead of the env var.
    """
    from work_buddy.config import load_config
    cfg = load_config()
    svc_cfg = (
        cfg.get("sidecar", {})
        .get("services", {})
        .get("telegram", {})
    )
    if not svc_cfg.get("enabled", False):
        return False
    port = svc_cfg.get("port", 5125)
    if not _port_open(port):
        return False
    # Telegram service is up — verify it's healthy
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status == 200
    except Exception:
        return False


def _probe_smart_connections() -> bool:
    """Check if Smart Connections plugin is available (from batch probe)."""
    return _bridge_plugin_available("smart-connections")


def _probe_embedding() -> bool:
    """Check if the embedding service is reachable."""
    import urllib.request
    from work_buddy.config import load_config
    cfg = load_config()
    port = (
        cfg.get("sidecar", {})
        .get("services", {})
        .get("embedding", {})
        .get("port", 5124)
    )
    if not _port_open(port):
        return False
    try:
        req = urllib.request.Request(f"http://localhost:{port}/health", method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def _probe_messaging() -> bool:
    """Check if the messaging service is reachable."""
    import urllib.request
    from work_buddy.config import load_config
    cfg = load_config()
    port = (
        cfg.get("sidecar", {})
        .get("services", {})
        .get("messaging", {})
        .get("port", 5123)
    )
    if not _port_open(port):
        return False
    try:
        req = urllib.request.Request(f"http://localhost:{port}/health", method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def _probe_datacore() -> bool:
    """Check if Datacore plugin is available (from batch probe)."""
    return _bridge_plugin_available("datacore")


def _probe_google_calendar() -> bool:
    """Check if Google Calendar plugin is available (from batch probe)."""
    return _bridge_plugin_available("google-calendar")


# ---------------------------------------------------------------------------
# Default probe registration
# ---------------------------------------------------------------------------

_DEFAULT_PROBES_REGISTERED = False


def _register_default_probes() -> None:
    """Register all built-in tool probes. Idempotent."""
    global _DEFAULT_PROBES_REGISTERED
    if _DEFAULT_PROBES_REGISTERED:
        return
    _DEFAULT_PROBES_REGISTERED = True

    probes = [
        ToolProbe(
            id="postgresql",
            display_name="PostgreSQL",
            probe_fn=_probe_postgresql,
            config_key="tools.postgresql.enabled",
            reason_when_missing="PostgreSQL not accepting connections on port 5432",
        ),
        ToolProbe(
            id="obsidian",
            display_name="Obsidian Bridge",
            probe_fn=_probe_obsidian,
            config_key="tools.obsidian.enabled",
            reason_when_missing="Obsidian is not running or the bridge plugin is not active",
        ),
        ToolProbe(
            id="chrome_extension",
            display_name="Chrome Tab Extension",
            probe_fn=_probe_chrome_extension,
            config_key="tools.chrome_extension.enabled",
            reason_when_missing="Chrome ledger not found or stale (>600s)",
        ),
        ToolProbe(
            id="hindsight",
            display_name="Hindsight Memory Server",
            probe_fn=_probe_hindsight,
            config_key="tools.hindsight.enabled",
            reason_when_missing="Hindsight server not reachable",
        ),
        ToolProbe(
            id="telegram",
            display_name="Telegram Bot",
            probe_fn=_probe_telegram,
            config_key="tools.telegram.enabled",
            reason_when_missing="Telegram bot token not set or service not enabled",
        ),
        ToolProbe(
            id="smart_connections",
            display_name="Smart Connections",
            probe_fn=_probe_smart_connections,
            config_key="tools.smart_connections.enabled",
            depends_on=["obsidian"],
            reason_when_missing="Smart Connections plugin not available in Obsidian",
        ),
        ToolProbe(
            id="embedding",
            display_name="Embedding Service",
            probe_fn=_probe_embedding,
            config_key="tools.embedding.enabled",
            reason_when_missing="Embedding service not reachable",
        ),
        ToolProbe(
            id="messaging",
            display_name="Messaging Service",
            probe_fn=_probe_messaging,
            config_key="tools.messaging.enabled",
            reason_when_missing="Messaging service not reachable",
        ),
        ToolProbe(
            id="datacore",
            display_name="Datacore Plugin",
            probe_fn=_probe_datacore,
            config_key="tools.datacore.enabled",
            depends_on=["obsidian"],
            reason_when_missing="Datacore plugin not available in Obsidian",
        ),
        ToolProbe(
            id="google_calendar",
            display_name="Google Calendar Plugin",
            probe_fn=_probe_google_calendar,
            config_key="tools.google_calendar.enabled",
            depends_on=["obsidian"],
            reason_when_missing="Google Calendar plugin not available in Obsidian",
        ),
    ]

    for probe in probes:
        register_tool_probe(probe)
