"""On-demand diagnostic check functions for the health subsystem.

Each check function returns ``{"ok": bool, "detail": str}``.
These are slower than startup-time probes (tools.py) — they answer
"what specifically is wrong?" rather than just "is it available?".

Import patterns:
- Uses ``socket.create_connection`` for TCP checks (fast, no overhead)
- Uses ``http.client`` for HTTP checks (avoids urllib issues with
  winloop on Windows in asyncio contexts)
- Reads sidecar_state.json for process-level status
"""

from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from work_buddy.paths import resolve


def _tcp_check(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> dict[str, Any]:
    """TCP connect check — returns {ok, detail}."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "detail": f"Port {port} accepting connections"}
    except (OSError, ConnectionRefusedError) as exc:
        return {"ok": False, "detail": f"Port {port} not reachable: {exc}"}


def _http_check(port: int, path: str = "/health", timeout: float = 3.0) -> dict[str, Any]:
    """HTTP GET check — returns {ok, detail, status_code}."""
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status == 200:
            return {"ok": True, "detail": f"HTTP {resp.status} on :{port}{path}", "status_code": resp.status}
        return {"ok": False, "detail": f"HTTP {resp.status} on :{port}{path}: {body[:200]}", "status_code": resp.status}
    except Exception as exc:
        return {"ok": False, "detail": f"HTTP request to :{port}{path} failed: {exc}"}


def _read_sidecar_service(service_name: str) -> dict[str, Any]:
    """Read a service's status from sidecar_state.json."""
    state_file = resolve("runtime/sidecar-state")
    if not state_file.exists():
        return {"ok": False, "detail": "sidecar_state.json not found — sidecar not running?"}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        svc = data.get("services", {}).get(service_name)
        if svc is None:
            return {"ok": False, "detail": f"Service '{service_name}' not in sidecar state"}
        status = svc.get("status", "unknown")
        if status == "healthy":
            return {"ok": True, "detail": f"Sidecar reports '{service_name}' as healthy (PID {svc.get('pid', '?')})"}
        return {
            "ok": False,
            "detail": f"Sidecar reports '{service_name}' as {status} (crashes: {svc.get('crash_count', 0)})",
        }
    except Exception as exc:
        return {"ok": False, "detail": f"Failed to read sidecar state: {exc}"}


# ---------------------------------------------------------------------------
# Component-specific checks
# ---------------------------------------------------------------------------


def check_postgresql() -> dict[str, Any]:
    """Check PostgreSQL is accepting connections on port 5432."""
    return _tcp_check(5432, timeout=2.0)


def check_obsidian_bridge() -> dict[str, Any]:
    """Check Obsidian bridge health endpoint.

    Uses generous timeout (10s) due to documented latency spikes up to ~4s.
    """
    from work_buddy.config import load_config
    cfg = load_config()
    port = cfg.get("obsidian", {}).get("bridge_port", 27125)
    # Fast TCP pre-check
    tcp = _tcp_check(port, timeout=1.0)
    if not tcp["ok"]:
        return {"ok": False, "detail": f"Bridge port {port} not open — Obsidian likely not running"}
    return _http_check(port, "/health", timeout=10.0)


def check_hindsight_api() -> dict[str, Any]:
    """Check Hindsight API on port 8888."""
    from work_buddy.config import load_config
    cfg = load_config()
    base_url = cfg.get("hindsight", {}).get("base_url", "http://localhost:8888")
    try:
        port = int(base_url.rsplit(":", 1)[-1].rstrip("/"))
    except (ValueError, IndexError):
        port = 8888
    tcp = _tcp_check(port, timeout=1.0)
    if not tcp["ok"]:
        return {"ok": False, "detail": f"Hindsight port {port} not bound — API likely crashed"}
    return _http_check(port, "/health", timeout=3.0)


def check_chrome_ledger() -> dict[str, Any]:
    """Check Chrome extension health via rolling ledger freshness.

    The extension writes periodic snapshots to the ledger every 5 minutes.
    We consider it healthy if updated within 10 minutes (2× interval).
    """
    from work_buddy.paths import resolve
    ledger = resolve("chrome/ledger")
    if not ledger.exists():
        return {"ok": False, "detail": "Chrome ledger not found — extension may not be installed"}
    age = time.time() - ledger.stat().st_mtime
    if age < 600:
        return {"ok": True, "detail": f"Ledger is {age:.0f}s old (fresh, threshold 600s)"}
    return {"ok": False, "detail": f"Ledger is {age:.0f}s old (stale, threshold 600s)"}


def _check_obsidian_plugin(plugin_id: str) -> dict[str, Any]:
    """Check if an Obsidian plugin is loaded (from batch probe cache)."""
    from work_buddy.tools import _OBSIDIAN_PLUGINS
    if _OBSIDIAN_PLUGINS is None:
        return {"ok": False, "detail": "Obsidian plugin cache not populated (bridge may be down)"}
    loaded = _OBSIDIAN_PLUGINS.get(plugin_id, False)
    if loaded:
        return {"ok": True, "detail": f"Plugin '{plugin_id}' is active"}
    return {"ok": False, "detail": f"Plugin '{plugin_id}' is not loaded in Obsidian"}


def check_obsidian_plugin_smart() -> dict[str, Any]:
    return _check_obsidian_plugin("smart-connections")


def check_obsidian_plugin_datacore() -> dict[str, Any]:
    return _check_obsidian_plugin("datacore")


def check_obsidian_plugin_calendar() -> dict[str, Any]:
    return _check_obsidian_plugin("google-calendar")


# --- Sidecar service checks (combine process + HTTP health) ---

def _check_sidecar_service(service_name: str, port: int) -> dict[str, Any]:
    """Check a sidecar-managed service: process status + HTTP health."""
    process = _read_sidecar_service(service_name)
    http = _http_check(port, "/health", timeout=3.0)
    if process["ok"] and http["ok"]:
        return {"ok": True, "detail": f"{service_name}: process healthy, API responding"}
    if not process["ok"] and not http["ok"]:
        return {"ok": False, "detail": f"{service_name}: {process['detail']}; {http['detail']}"}
    if process["ok"] and not http["ok"]:
        return {"ok": False, "detail": f"{service_name}: process alive but API not responding (degraded)"}
    # http ok but process not — unlikely but possible race
    return {"ok": True, "detail": f"{service_name}: API responding (process status unclear)"}


def check_sidecar_service_messaging() -> dict[str, Any]:
    return _check_sidecar_service("messaging", 5123)


def check_sidecar_service_embedding() -> dict[str, Any]:
    return _check_sidecar_service("embedding", 5124)


def check_sidecar_service_telegram() -> dict[str, Any]:
    return _check_sidecar_service("telegram", 5125)


def check_sidecar_service_dashboard() -> dict[str, Any]:
    return _check_sidecar_service("dashboard", 5127)
