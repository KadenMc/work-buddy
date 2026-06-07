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


def check_lmstudio() -> dict[str, Any]:
    """Check LM Studio is reachable on the configured base URL.

    LM Studio is an optional external service — the embedding system
    uses sentence-transformers by default, but can offload the passage
    encoder to LM Studio's ``/v1/embeddings`` endpoint (see
    ``docs/handbook/features_lmstudio-offload-setup.md``). This check
    surfaces "is LM Studio actually reachable?" on the Settings page so
    the user gets a clear answer when a configured ``provider:
    lmstudio`` entry doesn't work.

    Uses the same base-URL resolution helper as the embedding provider
    so both read from the single ``lmstudio.base_url`` config key.
    Probes ``GET /v1/models`` — a ``200`` confirms the server is both
    up and responding to the OpenAI-compatible API surface.
    """
    from work_buddy.embedding.providers.lmstudio import resolve_base_url
    from work_buddy.config import load_config

    base_url = resolve_base_url(load_config())
    # Parse host/port from base_url for the TCP pre-check. Falls back
    # to (127.0.0.1, 1234) if parsing fails so we still get a useful
    # probe result rather than a traceback.
    host, port = "127.0.0.1", 1234
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        if parsed.hostname:
            host = parsed.hostname
        if parsed.port:
            port = parsed.port
    except Exception:
        pass

    tcp = _tcp_check(port, host=host, timeout=1.5)
    if not tcp["ok"]:
        return {
            "ok": False,
            "detail": (
                f"LM Studio not reachable at {base_url} "
                f"(port {port} closed). Start LM Studio and enable its "
                f"local server (Developer tab → Start Server)."
            ),
        }
    return _http_check(port, "/v1/models", timeout=5.0)


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


def check_obsidian_plugin_datacore() -> dict[str, Any]:
    return _check_obsidian_plugin("datacore")


def check_obsidian_plugin_calendar() -> dict[str, Any]:
    return _check_obsidian_plugin("google-calendar")


def check_thunderbird_bridge() -> dict[str, Any]:
    """Runtime diagnostic for the thunderbird-work-buddy companion add-on.

    Wraps :func:`work_buddy.email.providers.thunderbird.probe_thunderbird_bridge`.
    Surfaces actionable detail strings: connection-file missing, port closed,
    auth rejected, or "ok with N accessible accounts".
    """
    try:
        from work_buddy.email.providers.thunderbird import probe_thunderbird_bridge
    except ImportError as exc:
        return {"ok": False, "detail": f"work_buddy.email module not importable: {exc}"}
    available, reason = probe_thunderbird_bridge()
    return {"ok": bool(available), "detail": reason or ("ok" if available else "not reachable")}


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


def check_sidecar_heartbeat() -> dict[str, Any]:
    """Sidecar daemon liveness — reads sidecar_state.json top-level fields.

    The sidecar writes ``last_tick_at`` every tick; freshness within 120s
    is considered healthy. An older timestamp, missing file, or missing
    pid means the daemon is not running or has become unresponsive.
    """
    import json as _json
    import time as _time
    from work_buddy.paths import resolve as _resolve

    state_file = _resolve("runtime/sidecar-state")
    if not state_file.exists():
        return {"ok": False, "detail": "sidecar_state.json missing — daemon not started"}
    try:
        data = _json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "detail": f"sidecar_state.json unreadable: {exc}"}

    pid = data.get("pid")
    last_tick = data.get("last_tick_at")
    if not pid or not last_tick:
        return {"ok": False, "detail": "sidecar has no pid or last_tick_at — daemon not started"}

    age = _time.time() - float(last_tick)
    if age > 120:
        return {
            "ok": False,
            "detail": f"sidecar last tick was {int(age)}s ago (threshold 120s) — daemon likely frozen",
        }
    return {"ok": True, "detail": f"sidecar alive (pid {pid}, tick age {int(age)}s)"}


# ---------------------------------------------------------------------------
# Tailscale
# ---------------------------------------------------------------------------
#
# Single helper shared by:
#   - The ``tailscale_status`` MCP capability (registry.py).
#   - The ``check_tailscale_*`` component health checks below.
#   - The ``check_tailscale_*`` requirement checks in requirement_checks.py.
#
# Each ``setup_help`` / ``setup_wizard(mode="diagnose")`` invocation runs
# multiple of these in quick succession; without memoization that means
# 3+ subprocess calls to ``tailscale status``. The 5-second TTL keeps them
# coherent within a diagnose pass without holding a stale view.

_TAILSCALE_CACHE: dict[str, Any] = {"ts": 0.0, "result": None}
_TAILSCALE_CACHE_TTL_SEC = 5.0


def get_tailscale_status(force: bool = False) -> dict[str, Any]:
    """Fetch Tailscale daemon + Serve state via the local CLI.

    Returns a dict with at least ``installed`` (bool), ``running`` (bool),
    and ``serve`` (dict | None). When the daemon is reachable, also fills
    ``backend_state``, ``tailnet``, ``self``, and ``peers``. Failures
    populate ``error`` rather than raising.

    Memoized for ``_TAILSCALE_CACHE_TTL_SEC`` seconds so multiple checks
    in one diagnose pass don't each shell out. Pass ``force=True`` to
    bypass the cache (e.g. immediately after a fixer runs).
    """
    import time as _time

    now = _time.time()
    cached = _TAILSCALE_CACHE.get("result")
    if (
        not force
        and cached is not None
        and (now - _TAILSCALE_CACHE["ts"]) < _TAILSCALE_CACHE_TTL_SEC
    ):
        return cached

    import subprocess
    import json as _json

    result: dict[str, Any] = {"installed": False, "running": False, "serve": None}

    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            result["installed"] = True
            result["error"] = proc.stderr.strip()[:200]
            _TAILSCALE_CACHE.update({"ts": now, "result": result})
            return result

        data = _json.loads(proc.stdout)
        result["installed"] = True
        result["running"] = True
        result["backend_state"] = data.get("BackendState", "")
        result["tailnet"] = data.get("MagicDNSSuffix", "")
        result["self"] = {
            "name": data.get("Self", {}).get("HostName", ""),
            "dns_name": data.get("Self", {}).get("DNSName", ""),
            "online": data.get("Self", {}).get("Online", False),
            "os": data.get("Self", {}).get("OS", ""),
            "ips": data.get("Self", {}).get("TailscaleIPs", []),
        }
        peers = data.get("Peer", {})
        result["peers"] = [
            {
                "name": p.get("HostName", ""),
                "dns_name": p.get("DNSName", ""),
                "online": p.get("Online", False),
                "os": p.get("OS", ""),
                "last_seen": p.get("LastSeen", ""),
            }
            for p in peers.values()
        ]
    except FileNotFoundError:
        _TAILSCALE_CACHE.update({"ts": now, "result": result})
        return result
    except Exception as exc:
        result["error"] = str(exc)[:200]
        _TAILSCALE_CACHE.update({"ts": now, "result": result})
        return result

    try:
        serve_proc = subprocess.run(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if serve_proc.returncode == 0 and serve_proc.stdout.strip():
            result["serve"] = _json.loads(serve_proc.stdout)
        else:
            result["serve"] = None
    except Exception:
        result["serve"] = None

    _TAILSCALE_CACHE.update({"ts": now, "result": result})
    return result


def check_tailscale_daemon() -> dict[str, Any]:
    """Component health check: Tailscale daemon is installed and running."""
    status = get_tailscale_status()
    if not status.get("installed"):
        return {"ok": False, "detail": "tailscale CLI not found on PATH"}
    if status.get("error"):
        return {"ok": False, "detail": f"tailscale status failed: {status['error']}"}
    if not status.get("running"):
        return {"ok": False, "detail": "tailscale daemon not running"}
    backend = status.get("backend_state", "")
    if backend != "Running":
        return {"ok": False, "detail": f"tailscale backend_state is {backend!r}, expected 'Running'"}
    return {"ok": True, "detail": f"tailscale daemon running (backend {backend})"}


def check_tailscale_self_online() -> dict[str, Any]:
    """Component health check: this device is online on the tailnet."""
    status = get_tailscale_status()
    if not status.get("running"):
        return {"ok": False, "detail": "tailscale daemon not running — can't determine online state"}
    self_info = status.get("self") or {}
    if not self_info.get("online"):
        name = self_info.get("name") or "this device"
        return {
            "ok": False,
            "detail": f"{name} is not online on the tailnet (Tailscale signed out, paused, or key expired?)",
        }
    return {
        "ok": True,
        "detail": f"{self_info.get('name', 'self')} online on tailnet {status.get('tailnet', '')}".strip(),
    }


# ---------------------------------------------------------------------------
# github_backups
# ---------------------------------------------------------------------------


def _parse_backup_ts(ts_str: str):
    """Parse a ``last_run.json`` backup timestamp to a tz-aware UTC datetime.

    Accepts both the standard ISO-8601 form (``2026-05-20T16:00:20Z``,
    written by the backup op) and the compact snapshot form with dashes
    in the time component (``2026-05-20T16-00-20Z``, the snapshot-id
    shape) — ``last_run.json`` may carry either. Returns ``None`` if
    neither form matches.
    """
    from datetime import datetime, timezone

    s = (ts_str or "").strip()
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%dT%H-%M-%SZ")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_github_backup_freshness() -> dict[str, Any]:
    """Read ``.data/backups/last_run.json`` and assess freshness.

    Never hits GitHub on the hot path — the freshness check is
    entirely local. The sidecar cron writes a success/fail signal to
    ``last_run.json`` after each push attempt; this check is the
    dashboard-side observability for that signal.

    Returns ``{ok, detail}``:

    - No file yet → ``ok=False`` ("no backup run recorded").
    - Last run failed → ``ok=False`` with the failure detail.
    - Last run succeeded but is overdue (older than 2 ×
      cadence_minutes) → ``ok=False`` ("backup is overdue").
    - Last run succeeded and is fresh → ``ok=True``.
    """
    try:
        from work_buddy.backups.remote import read_last_run
        from work_buddy.config import load_config
    except Exception as exc:
        return {"ok": False, "detail": f"backups module unavailable: {exc}"}

    last = read_last_run()
    if last is None:
        return {"ok": False,
                "detail": ".data/backups/last_run.json not found — "
                "no backup has run yet"}

    if last.get("status") != "ok":
        return {
            "ok": False,
            "detail": (
                f"last backup status={last.get('status', '?')}: "
                f"{last.get('error') or last.get('message') or '(no detail)'}"
            ),
        }

    # Freshness check: compare last run timestamp against 2×cadence window.
    cfg = load_config()
    cadence_min = int(
        ((cfg.get("backups") or {}).get("github") or {})
        .get("cadence_minutes", 60)
    )
    deadline_seconds = max(1, cadence_min) * 60 * 2
    ts_str = last.get("ts") or last.get("at") or last.get("snapshot_ts")
    if not ts_str:
        return {"ok": True, "detail": "last backup ok (timestamp missing — "
                "freshness window not enforced)"}
    from datetime import datetime, timezone
    last_dt = _parse_backup_ts(ts_str)
    if last_dt is None:
        return {"ok": True, "detail": f"last backup ok (ts {ts_str!r} "
                "unparseable — freshness window not enforced)"}
    age_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()

    if age_seconds > deadline_seconds:
        mins = int(age_seconds / 60)
        return {
            "ok": False,
            "detail": f"last backup is {mins}m old (cadence is "
            f"{cadence_min}m; threshold is 2× = {cadence_min * 2}m). "
            "Run /wb-backup-now or check the sidecar.",
        }
    return {
        "ok": True,
        "detail": (
            f"last backup ok ({int(age_seconds / 60)}m ago; "
            f"snapshot {last.get('snapshot_id', '?')})"
        ),
    }


def check_google_calendar_native_api() -> dict[str, Any]:
    """Runtime probe: the Google Calendar API answers with the stored native
    OAuth token. Resolves only on diagnose (the component is ``health_source``
    ``custom`` — no continuous polling)."""
    try:
        from work_buddy.calendar.providers.google_native import (
            GoogleNativeCalendarProvider,
        )
        from work_buddy.config import load_config

        cfg = ((load_config() or {}).get("calendar", {}) or {}).get("google_native", {}) or {}
        health = GoogleNativeCalendarProvider(cfg).health()
        if health.get("ready"):
            return {
                "ok": True,
                "detail": f"Calendar API reachable; {health.get('calendar_count', 0)} calendars.",
            }
        return {"ok": False, "detail": health.get("reason", "Calendar API not ready")}
    except Exception as exc:  # pragma: no cover — defensive
        return {"ok": False, "detail": f"Native Calendar probe failed: {exc}"}
