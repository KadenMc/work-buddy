"""Python client for the work-buddy Obsidian plugin bridge.

Talks to the HTTP server running inside Obsidian via the Work Buddy plugin.
Follows the same pattern as work_buddy.messaging.client.

Failure model
-------------
Bridge calls raise typed exceptions from
:mod:`work_buddy.obsidian.errors` (``ObsidianError`` and subclasses) on
failure. The gateway classifies them via ``isinstance`` rather than
substring-matching error strings; the dashboard sparkline reads the
module-level ``_last_failure_kind`` (preserved as the legacy strings
``"timeout" | "unreachable" | "http_error" | ""`` for backward compat).

``write_file_raw`` keeps its boolean return contract for the
transitional CP1-CP5 window — TRANSLATE-pattern callers will be
migrated in CP6, after which the function returns to its native
typed-exception contract.
"""

import hashlib
import json
import platform
import subprocess
import time
import urllib.parse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from work_buddy.config import load_config
from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger

# Re-export the typed exceptions at the bridge module level so legacy
# callers that do ``from work_buddy.obsidian.bridge import EditorConflict``
# keep working through the transition. ``EditorConflict`` is an alias for
# ``ObsidianEditorConflict``; CP9 removes the alias.
from work_buddy.obsidian.errors import (
    EditorConflict,  # alias for ObsidianEditorConflict; removed in CP9
    ObsidianEditorConflict,
    ObsidianError,
    ObsidianHTTPError,
    ObsidianNotRunning,
    ObsidianPluginDisabled,
    ObsidianPluginMissing,
    ObsidianPostWriteUncertain,
    ObsidianRefused,
    ObsidianServerError,
    ObsidianStartupRace,
    ObsidianTimeout,
    ObsidianUnreachable,
)

logger = get_logger(__name__)

# Compatible work-buddy plugin version range for this work-buddy release.
# Lower bound (inclusive): bump when this work-buddy needs new plugin endpoints.
# Upper bound (exclusive): bump when a future plugin drops deprecated endpoints.
PLUGIN_VERSION_MIN = "0.1.0"  # inclusive — oldest plugin that has all needed endpoints
PLUGIN_VERSION_MAX = "0.2.0"  # exclusive — first plugin version NOT tested/supported

# ---------------------------------------------------------------------------
# Lightweight latency tracking (module-level, no external dependencies)
# ---------------------------------------------------------------------------

_last_success_ts: float = 0.0      # epoch of last successful request
_last_success_ms: float = 0.0      # latency of last successful request
_consecutive_failures: int = 0     # reset on success
_last_failure_reason: str = ""     # e.g. "TimeoutError", "ConnectionRefusedError"
_last_failure_kind: str = ""       # "timeout" | "unreachable" | "http_error" | ""
_last_failure_status: int | None = None  # HTTP status on 4xx/5xx, else None


def _record_probe_success(elapsed_ms: float) -> None:
    """Record a successful out-of-band probe (e.g. ``_probe_obsidian``).

    The main ``_request()`` path updates ``_last_success_*`` for real
    bridge calls. Probes use ``http.client`` directly and skip that
    path, so without this helper the probe's own round-trip is
    invisible to ``get_latency_context()``. Call this on probe
    success so the very first status check after startup reflects
    real data instead of "No successful bridge calls recorded."
    """
    global _last_success_ts, _last_success_ms, _consecutive_failures
    _last_success_ts = time.time()
    _last_success_ms = elapsed_ms
    _consecutive_failures = 0


def _record_probe_failure(reason: str) -> None:
    """Record a probe failure for inclusion in ``get_latency_context()``."""
    global _consecutive_failures, _last_failure_reason
    _consecutive_failures += 1
    _last_failure_reason = reason


def get_last_bridge_state() -> dict[str, Any]:
    """Classify the most recent bridge failure into the four-state taxonomy.

    Returns a dict with:

    * ``state``: one of ``"ok"`` (no recent failure), ``"timeout"`` (state
      2 — bridge responding slowly), ``"obsidian_not_running"`` (state
      1), ``"plugin_not_installed"`` (state 3),
      ``"plugin_disabled"`` (state 4), ``"http_error"`` (non-2xx /
      non-409 response from the bridge), or ``"unknown"`` (filesystem
      check couldn't resolve the vault).
    * ``detail``: human-readable one-liner explaining the state.
    * ``status``: HTTP status code if state is ``"http_error"``, else
      ``None``.
    * ``reason``: the underlying exception class name if available.

    Safe to call from any thread; reads module-level counters set by
    ``_request_with_status`` + the filesystem ``get_work_buddy_plugin_state``
    check. Cheap — no network, one optional filesystem touch.
    """
    if _last_failure_kind == "":
        return {"state": "ok", "detail": "no recent failure", "status": None, "reason": ""}

    if _last_failure_kind == "timeout":
        return {
            "state": "timeout",
            "detail": (
                "Bridge port is open but the request timed out — Obsidian is "
                "alive but the plugin is busy, the event loop is stalled, or "
                "a latency spike is in progress."
            ),
            "status": None,
            "reason": _last_failure_reason,
        }

    if _last_failure_kind == "http_error":
        return {
            "state": "http_error",
            "detail": f"Bridge returned HTTP {_last_failure_status}",
            "status": _last_failure_status,
            "reason": _last_failure_reason,
        }

    # _last_failure_kind == "unreachable" — connection refused / DNS /
    # host down. Disambiguate state 1 vs 3 vs 4 via the filesystem
    # check. Keep it cheap: process check first (state 1), fall through
    # to plugin state.
    if not is_obsidian_running():
        return {
            "state": "obsidian_not_running",
            "detail": "Obsidian is not running (port 27125 unreachable, Obsidian.exe not found).",
            "status": None,
            "reason": _last_failure_reason,
        }

    try:
        from work_buddy.health.requirement_checks import get_work_buddy_plugin_state
        plugin_state, plugin_detail = get_work_buddy_plugin_state()
    except Exception as exc:
        return {
            "state": "unknown",
            "detail": f"Unable to inspect plugin state: {exc}",
            "status": None,
            "reason": _last_failure_reason,
        }

    if plugin_state == "not_installed":
        return {
            "state": "plugin_not_installed",
            "detail": (
                "Obsidian is running but the work-buddy plugin is not "
                f"installed ({plugin_detail}). Install from "
                "https://github.com/KadenMc/obsidian-work-buddy."
            ),
            "status": None,
            "reason": _last_failure_reason,
        }

    if plugin_state == "disabled":
        return {
            "state": "plugin_disabled",
            "detail": (
                "Obsidian is running and the plugin is installed but not "
                f"enabled ({plugin_detail}). Open Obsidian → Settings → "
                "Community Plugins and toggle 'Work Buddy' on."
            ),
            "status": None,
            "reason": _last_failure_reason,
        }

    # plugin_state == "ok" — plugin enabled but port still unreachable.
    # This is the ambiguous case: state 1 process check said "running",
    # plugin is on, yet TCP refused. Most likely a race (Obsidian just
    # started, plugin not loaded yet) or a port binding error.
    if plugin_state == "unknown":
        return {
            "state": "unknown",
            "detail": (
                f"Bridge unreachable; plugin state could not be resolved: "
                f"{plugin_detail}."
            ),
            "status": None,
            "reason": _last_failure_reason,
        }
    return {
        "state": "obsidian_not_running",
        "detail": (
            "Bridge port refused connection despite Obsidian appearing "
            "to be running with the plugin enabled — Obsidian may still "
            "be starting up, or the plugin failed to bind to port 27125."
        ),
        "status": None,
        "reason": _last_failure_reason,
    }


def get_latency_context() -> str:
    """One-line latency summary for error messages."""
    if _last_success_ts == 0:
        if _consecutive_failures > 0:
            return f"No successful bridge calls in this process | {_consecutive_failures} failures ({_last_failure_reason})"
        return "No successful bridge calls recorded in this process."
    ago = time.time() - _last_success_ts
    if ago < 60:
        ago_str = f"{ago:.0f}s ago"
    elif ago < 3600:
        ago_str = f"{ago / 60:.0f}m ago"
    else:
        ago_str = f"{ago / 3600:.1f}h ago"
    parts = [f"Last OK: {ago_str} ({_last_success_ms:.0f}ms)"]
    if _consecutive_failures > 0:
        parts.append(f"{_consecutive_failures} failures since ({_last_failure_reason})")
    return " | ".join(parts)


def _compare_semver(a: str, b: str) -> int:
    """Compare two semver strings. Returns -1 (a<b), 0 (equal), or 1 (a>b)."""
    pa = [int(x) for x in a.split(".")[:3]]
    pb = [int(x) for x in b.split(".")[:3]]
    for av, bv in zip(pa + [0, 0, 0], pb + [0, 0, 0]):
        if av < bv:
            return -1
        if av > bv:
            return 1
    return 0


def _wb_version() -> str:
    """Get the current work-buddy version from pyproject.toml metadata."""
    try:
        from importlib.metadata import version
        return version("work-buddy")
    except Exception:
        return "0.0.0"


def _base_url(cfg: dict[str, Any] | None = None) -> str:
    if cfg is None:
        cfg = load_config()
    port = cfg.get("obsidian", {}).get("bridge_port", 27125)
    return f"http://127.0.0.1:{port}"


def _request(
    method: str,
    path: str,
    data: dict | str | None = None,
    timeout: int = 10,
    retries: int = 0,
) -> dict | None:
    """Make a request to the bridge server. No auto-start — Obsidian must be running.

    Args:
        retries: Number of retry attempts on timeout. Each retry uses the same
                 timeout. The bridge has documented latency spikes that resolve
                 on immediate retry, so this is safe.
    """
    url = f"{_base_url()}{path}"

    body = None
    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data).encode("utf-8")
        else:
            body = data.encode("utf-8")

    global _last_success_ts, _last_success_ms, _consecutive_failures

    for attempt in range(1 + retries):
        req = Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")

        t0 = time.time()
        try:
            with urlopen(req, timeout=timeout) as resp:
                elapsed_ms = (time.time() - t0) * 1000
                _last_success_ts = time.time()
                _last_success_ms = elapsed_ms
                _consecutive_failures = 0
                if resp.status == 204:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except (TimeoutError, OSError) as exc:
            _consecutive_failures += 1
            _last_failure_reason = type(exc).__name__
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc)
            if is_timeout and attempt < retries:
                logger.info(
                    "Bridge timeout (attempt %d/%d): %s %s — retrying",
                    attempt + 1, 1 + retries, method, path,
                )
                continue
            logger.warning(
                "Bridge request failed: %s %s — %s (%s) [%s]",
                method, path, type(exc).__name__, exc, get_latency_context(),
            )
            return None
        except URLError as exc:
            _consecutive_failures += 1
            _last_failure_reason = type(exc).__name__
            logger.warning(
                "Bridge request failed: %s %s — %s [%s]",
                method, path, exc, get_latency_context(),
            )
            return None

    return None  # unreachable, but satisfies type checker


_bridge_confirmed = False


def _probe_port_open(timeout: float = 0.5) -> bool:
    """Fast TCP check: is the bridge port actually listening?

    Distinguishes "port refused" (states 1/3/4 in the four-state
    taxonomy) from "port open but HTTP hung" (state 2). Used by
    ``_request_with_status`` as a fallback when the urllib exception
    stringification doesn't cleanly identify the underlying cause
    (common on Windows where ``ConnectionRefusedError`` can surface as
    an OSError whose message mentions "timed out").
    """
    import socket
    try:
        cfg = load_config()
        port = cfg.get("obsidian", {}).get("bridge_port", 27125)
    except Exception:
        port = 27125
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def is_obsidian_running() -> bool:
    """Fast process-level check (~60ms) for whether Obsidian is open.

    Uses ctypes on Windows for speed (no subprocess overhead).
    Falls back to subprocess pgrep on other platforms.
    """
    try:
        if platform.system() == "Windows":
            return _check_process_windows("Obsidian.exe")
        else:
            result = subprocess.run(
                ["pgrep", "-xi", "obsidian"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
    except Exception:
        return True  # assume running if we can't check — let HTTP decide


def _check_process_windows(name: str) -> bool:
    """Check if a process is running on Windows via ctypes (~60ms)."""
    import ctypes
    import ctypes.wintypes as wt

    psapi = ctypes.windll.psapi
    kernel32 = ctypes.windll.kernel32

    pids = (wt.DWORD * 4096)()
    needed = wt.DWORD()
    psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed))

    target = name.lower()
    for i in range(needed.value // ctypes.sizeof(wt.DWORD)):
        pid = pids[i]
        if pid == 0:
            continue
        # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        handle = kernel32.OpenProcess(0x0410, False, pid)
        if handle:
            buf = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleBaseNameW(handle, None, buf, 260):
                if buf.value.lower() == target:
                    kernel32.CloseHandle(handle)
                    return True
            kernel32.CloseHandle(handle)
    return False


def is_available() -> bool:
    """Check if the bridge server is reachable.

    First does an instant process check (~50ms). If Obsidian isn't running,
    returns False immediately without waiting for HTTP timeouts. If it is
    running, retries the health check once (10s then 15s) to handle the
    bridge's intermittent latency spikes.
    """
    # Fast check: is Obsidian even open?
    if not is_obsidian_running():
        return False

    # Send work-buddy version so the plugin can warn if outdated
    health_path = f"/health?wb_version={_wb_version()}"
    result = _request("GET", health_path, timeout=10)
    if result is None:
        result = _request("GET", health_path, timeout=15)
    return result is not None and result.get("status") == "ok"


def _get_health() -> dict | None:
    """Call /health and return the full response dict, or None on failure."""
    health_path = f"/health?wb_version={_wb_version()}"
    result = _request("GET", health_path, timeout=10)
    if result is None:
        result = _request("GET", health_path, timeout=15)
    return result


def require_available() -> None:
    """Raise RuntimeError if the bridge is not reachable or incompatible.

    Checks:
    1. Obsidian is running (fast process check)
    2. Bridge responds to /health
    3. Plugin version is within supported range (>= MIN, < MAX)
    """
    global _bridge_confirmed
    if not is_obsidian_running():
        raise RuntimeError(
            "Obsidian is not running. Please open Obsidian."
        )

    health = _get_health()
    if health is None or health.get("status") != "ok":
        raise RuntimeError(
            "Obsidian is running but the Work Buddy bridge is not responding. "
            "Check that the Work Buddy plugin is enabled in Obsidian settings."
        )

    # Version compatibility check (range: >= PLUGIN_VERSION_MIN, < PLUGIN_VERSION_MAX)
    plugin_version = health.get("version", "0.0.0")
    if _compare_semver(plugin_version, PLUGIN_VERSION_MIN) < 0:
        raise RuntimeError(
            f"work-buddy plugin is v{plugin_version}, but this version "
            f"of work-buddy requires >= v{PLUGIN_VERSION_MIN}. "
            f"Update the plugin in Obsidian: Settings → Community plugins."
        )
    if _compare_semver(plugin_version, PLUGIN_VERSION_MAX) >= 0:
        raise RuntimeError(
            f"work-buddy plugin is v{plugin_version}, but this version "
            f"of work-buddy supports < v{PLUGIN_VERSION_MAX}. "
            f"Update work-buddy, or downgrade the plugin."
        )

    if not _bridge_confirmed:
        _bridge_confirmed = True
        try:
            from work_buddy.obsidian.plugin_versions import confirm_working
            confirm_working("work-buddy", plugin_version)
        except Exception:
            pass  # Best-effort — don't break bridge availability check


# ── Typed wrappers ──────────────────────────────────────────────


def get_tags() -> dict[str, int]:
    """Get all vault-wide tags with occurrence counts.

    Returns dict mapping tag name (e.g. "#project") to count.

    Delegates to the tags integration (metadataCache via eval_js).
    Case is preserved from Obsidian's metadata cache.
    """
    from work_buddy.obsidian.tags import get_all_tags

    try:
        raw = get_all_tags(include_files=False)
        return {t["tag"]: t["count"] for t in raw}
    except Exception:
        logger.debug("tags.get_all_tags() failed, falling back to REST API")
        result = _request("GET", "/tags")
        if result is None:
            return {}
        return result.get("tags", {})


def get_tag_files(tag: str) -> list[str]:
    """Get file paths containing a specific tag.

    Args:
        tag: Tag name, with or without # prefix.

    Delegates to the tags integration (metadataCache via eval_js).
    Falls back to REST API if eval_js is unavailable.
    """
    from work_buddy.obsidian.tags import search_by_tag

    if not tag.startswith("#"):
        tag = "#" + tag
    try:
        result = search_by_tag(tag, mode="exact", limit=500)
        return [f["path"] for f in result.get("files", [])]
    except Exception:
        logger.debug("tags.search_by_tag() failed, falling back to REST API")
        encoded = urllib.parse.quote(tag, safe="")
        result = _request("GET", f"/tags/{encoded}")
        if result is None:
            return []
        return result.get("files", [])


def read_file(path: str) -> str | None:
    """Read a file's content by vault-relative path.

    Returns file content string, or None if not found / bridge unavailable.
    """
    encoded = urllib.parse.quote(path, safe="/")
    result = _request("GET", f"/files/{encoded}", retries=1)
    if result is None:
        return None
    return result.get("content")


# ---------------------------------------------------------------------------
# Failure-class helpers (typed-exception construction)
# ---------------------------------------------------------------------------


def _http_status_to_exception_type(status: int) -> type[ObsidianHTTPError]:
    """Map an HTTP status code to the right ObsidianHTTPError subclass."""
    if status == 409:
        return ObsidianEditorConflict
    if 400 <= status < 500:
        return ObsidianRefused
    if status >= 500:
        return ObsidianServerError
    # Anything else with status set (1xx, 3xx) — keep generic, shouldn't happen.
    return ObsidianHTTPError


def _refine_unreachable_kind() -> type[ObsidianUnreachable]:
    """Pick the most specific ObsidianUnreachable subclass for the current state.

    Mirrors the disambiguation in :func:`get_last_bridge_state` (state
    1/3/4 + startup race). The cost of process + filesystem checks is
    paid only on connection failures, so the slow-path is fine.

    Returns the base ``ObsidianUnreachable`` if the disambiguation
    helpers themselves error — better to raise a less-specific type
    than to mask the original failure with a secondary one.
    """
    try:
        if not is_obsidian_running():
            return ObsidianNotRunning
    except Exception:
        return ObsidianUnreachable

    try:
        from work_buddy.health.requirement_checks import get_work_buddy_plugin_state
        plugin_state, _detail = get_work_buddy_plugin_state()
    except Exception:
        return ObsidianUnreachable

    if plugin_state == "not_installed":
        return ObsidianPluginMissing
    if plugin_state == "disabled":
        return ObsidianPluginDisabled
    if plugin_state == "ok":
        # Plugin enabled but port still refused — startup race window
        # (Obsidian just started, plugin not loaded yet) or a bind error.
        return ObsidianStartupRace
    # plugin_state == "unknown" or anything else — generic unreachable.
    return ObsidianUnreachable


def _make_content_hint(content: str, write_mode: str) -> str:
    """Compute the verification hint for a write payload.

    For ``write_mode="replace"`` the verifier needs to confirm the
    *full* content matches — sha256 is the right shape. For
    ``insert`` / ``append`` the verifier needs only to confirm a
    unique fragment landed; first 256 chars is enough for inserted
    addendums (which carry timestamped or otherwise unique markers).

    The 256-char prefix is intentionally generous: short enough that
    the post-write read-back can do an in-memory substring check
    cheaply, long enough that two different inserts at the same path
    won't collide.
    """
    if write_mode == "replace":
        return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
    return content[:256]


def _classify_request_failure(exc: BaseException) -> type[ObsidianError]:
    """Decide the right typed-exception class for a urllib failure.

    Pure classifier — no side effects. The caller is responsible for
    setting ``_last_failure_kind`` / ``_last_failure_status`` and
    raising the actual instance.

    On Windows, urllib wraps ``ConnectionRefusedError`` inside ``URLError``
    and the stringification often contains "timed out" even though the
    socket was refused — so we inspect the underlying exception class
    via ``.reason``, not the message.
    """
    underlying: BaseException = exc
    if isinstance(exc, URLError) and exc.reason is not None:
        if isinstance(exc.reason, BaseException):
            underlying = exc.reason

    if isinstance(underlying, ConnectionError):
        # TCP refused — definitely state 1/3/4. Refine.
        return _refine_unreachable_kind()

    if isinstance(underlying, TimeoutError):
        # Ambiguous: HTTP hung (state 2) vs TCP-connect timeout (state
        # 1/3/4 surfaced as socket.timeout on Windows). TCP probe.
        if _probe_port_open():
            return ObsidianTimeout
        return _refine_unreachable_kind()

    # Last-resort disambiguation: TCP probe.
    if _probe_port_open():
        return ObsidianTimeout
    return _refine_unreachable_kind()


def _exception_to_failure_kind(exc_cls: type[ObsidianError]) -> tuple[str, int | None]:
    """Map a typed exception class to (legacy `_last_failure_kind`, status).

    Preserves the dashboard sparkline contract: it consumes
    ``_last_failure_kind`` strings ``"timeout" | "unreachable" |
    "http_error" | ""`` to pick bar classes (``bar-fail``, ``bar-unreachable``,
    etc.) — see ``work_buddy/dashboard/api.py::get_bridge_status``.

    All ObsidianUnreachable subclasses → "unreachable".
    All ObsidianHTTPError subclasses → "http_error".
    All ObsidianTimeout subclasses → "timeout" (PostWriteUncertain
    included — the dashboard treats it as a regular timeout).
    """
    if issubclass(exc_cls, ObsidianHTTPError):
        return "http_error", None  # status filled in by caller
    if issubclass(exc_cls, ObsidianUnreachable):
        return "unreachable", None
    if issubclass(exc_cls, ObsidianTimeout):
        return "timeout", None
    return "", None  # generic ObsidianError — shouldn't happen at raise sites


# ---------------------------------------------------------------------------
# Internal request helpers
# ---------------------------------------------------------------------------


def _request_with_status(
    method: str,
    path: str,
    data: dict | str | None = None,
    timeout: int = 10,
) -> tuple[int, dict | None]:
    """Make a bridge request, raising typed exceptions on failure.

    Returns ``(status, body)`` for 2xx success — body is the parsed JSON
    response, or None for 204 No Content / empty bodies.

    Raises:
      ``ObsidianEditorConflict`` — on 409 (file open with unsaved typing)
      ``ObsidianRefused`` — on 4xx other than 409 (structural refusal)
      ``ObsidianServerError`` — on 5xx (plugin-side fault)
      ``ObsidianTimeout`` — port open, HTTP hung past ``timeout``
      ``ObsidianUnreachable`` (or specific subclass) — TCP refused

    Distinct from ``_request`` because some callers — namely
    ``write_file_raw`` — need to distinguish HTTP status codes
    (specifically 409) and post-write timeouts. No retries; caller
    orchestrates them.

    Side effects:
      - Updates the module-level latency / failure counters.
      - Sets ``_last_failure_kind`` and ``_last_failure_status`` from
        the typed exception class BEFORE raising — preserves the
        dashboard sparkline contract.
    """
    url = f"{_base_url()}{path}"
    payload_bytes: bytes | None = None
    if data is not None:
        if isinstance(data, dict):
            payload_bytes = json.dumps(data).encode("utf-8")
        else:
            payload_bytes = data.encode("utf-8")

    global _last_success_ts, _last_success_ms, _consecutive_failures
    global _last_failure_reason, _last_failure_kind, _last_failure_status

    req = Request(url, data=payload_bytes, method=method)
    req.add_header("Content-Type", "application/json")

    t0 = time.time()
    try:
        with urlopen(req, timeout=timeout) as resp:
            elapsed_ms = (time.time() - t0) * 1000
            _last_success_ts = time.time()
            _last_success_ms = elapsed_ms
            _consecutive_failures = 0
            _last_failure_kind = ""
            _last_failure_status = None
            if resp.status == 204:
                return resp.status, None
            response_payload = resp.read().decode("utf-8")
            return resp.status, json.loads(response_payload) if response_payload else None
    except HTTPError as exc:
        # 4xx/5xx — server reachable, structured response. Read the body
        # so the typed exception carries it for downstream consumers.
        try:
            err_payload = exc.read().decode("utf-8")
            err_body = json.loads(err_payload) if err_payload else None
        except Exception:
            err_body = None

        # 4xx is a structured refusal, not a bridge fault — don't bump
        # _consecutive_failures (latency tracking is for connectivity,
        # not application-level conflict).
        _last_failure_kind = "http_error"
        _last_failure_status = exc.code

        exc_cls = _http_status_to_exception_type(exc.code)
        # ObsidianEditorConflict has a custom signature — handle separately
        # so the legacy "editor_dirty: <path>" message format is preserved.
        if exc_cls is ObsidianEditorConflict:
            # ``path`` is the URL path here (already decoded); strip the
            # ``/files/`` prefix for the EditorConflict.path field.
            file_path = path
            if file_path.startswith("/files/"):
                file_path = urllib.parse.unquote(file_path[len("/files/"):])
            raise ObsidianEditorConflict(file_path, body=err_body) from exc
        raise exc_cls(exc.code, body=err_body) from exc

    except (TimeoutError, URLError, OSError) as exc:
        _consecutive_failures += 1
        _last_failure_reason = type(exc).__name__

        exc_cls = _classify_request_failure(exc)
        kind, _status = _exception_to_failure_kind(exc_cls)
        _last_failure_kind = kind
        _last_failure_status = None

        logger.warning(
            "Bridge request failed: %s %s — %s [%s]",
            method, path, exc, get_latency_context(),
        )

        # Build the right instance. ObsidianUnreachable subclasses and
        # ObsidianTimeout take no constructor args; ObsidianHTTPError
        # subclasses won't reach here (they go through the HTTPError
        # branch above).
        raise exc_cls() from exc


def write_file_raw(
    path: str,
    content: str,
    *,
    write_mode: str = "replace",
    content_hint: str | None = None,
) -> bool:
    """Write or create a vault file (bridge-only, no consent check, no fallback).

    For internal callers that handle consent at a higher level (e.g.,
    ``append_to_journal`` which has its own ``@requires_consent``) or
    that own files the Tasks plugin has state for and so cannot use
    the fallback-capable ``vault_write`` helper. See the
    ``obsidian/vault-write-decision`` knowledge unit for the picking
    rule between this and ``vault_write``.

    Editor-conflict handling
    ------------------------
    The plugin returns ``409 Conflict`` if the target file is open in a
    MarkdownView with unsaved typing — writing would silently clobber
    the user's in-flight edits. We raise :class:`ObsidianEditorConflict`
    immediately on the first 409 instead of retrying inside this
    function: the payload we'd send on retry is the *same* bytes the
    caller composed minutes ago, so even after the user's typing
    auto-saves to disk, a bridge-level retry would clobber the saved
    typing with stale content. Re-doing the read-modify-write is the
    caller's job.

    The right place for that retry is the gateway's transient-error
    auto-enqueue: ``ObsidianEditorConflict`` is classified transient,
    and any capability with ``retry_policy`` ``replay`` or
    ``verify_first`` will be re-invoked from scratch by the sidecar's
    retry sweep (work_buddy/sidecar/retry_sweep.py) on adaptive
    backoff. Each re-invocation reads the file fresh and recomputes
    the payload.

    Callers MUST NOT swallow ``ObsidianEditorConflict`` into a direct
    filesystem write — see the ``ObsidianEditorConflict`` docstring.

    Post-write uncertainty
    ----------------------
    A client-side timeout AFTER the PUT body has been sent is
    ambiguous: the plugin may have committed the write before the
    response failed to arrive. Returning ``False`` for this case
    (the legacy behavior) silently double-writes on retry — the
    capability sees "failure", caller retries, plugin processes the
    second PUT, file ends up with two copies of the inserted content.

    To prevent this we raise :class:`ObsidianPostWriteUncertain`,
    carrying ``(path, content_hint, write_mode)`` so the gateway
    can call :func:`work_buddy.obsidian.post_write_verify.verify_post_write`
    and decide whether the write actually landed. ``content_hint`` is
    used to fingerprint the write — for ``insert``/``append`` modes
    pass the unique inserted fragment; for ``replace`` (the default
    for full-file writes through this function) we compute a sha256
    of the full payload automatically.

    Args:
        path: Vault-relative file path.
        content: The full file content to write.
        write_mode: ``"replace"`` (default — full-file PUT),
            ``"insert"`` or ``"append"`` (when the caller is doing a
            section-aware modification and wants a substring-witness
            verification rather than a full-content sha256). Affects
            only the post-write-uncertain hint shape; the actual PUT
            sends the full file content regardless.
        content_hint: Optional override for the verification witness
            string. Defaults to a sha256 hash for ``replace`` mode and
            the first 256 chars of ``content`` otherwise.

    Returns:
        True on success.

        False on:
          - Bridge unreachable (port refused — write definitely did NOT happen).
          - HTTP 4xx other than 409 / HTTP 5xx (logged with status).

        This bool return is a transitional shim for legacy TRANSLATE-pattern
        callers; CP6 unwraps it and the function returns to its native
        typed-exception contract.

    Raises:
        :class:`ObsidianEditorConflict` on 409.
        :class:`ObsidianPostWriteUncertain` on PUT timeout (port open).

    Bridge latency: uses a 15s per-request timeout (bridge has documented
    multi-second latency spikes especially on creates with large payloads).
    """
    encoded = urllib.parse.quote(path, safe="/")
    hint = content_hint if content_hint is not None else _make_content_hint(content, write_mode)

    try:
        status, _body = _request_with_status(
            "PUT", f"/files/{encoded}", {"content": content}, timeout=15,
        )
        # _request_with_status returns only on 2xx; status here is 200/201/204.
        return status in (200, 201, 204)
    except ObsidianEditorConflict:
        # 409 — re-raise. Caller (or the gateway's retry queue) handles it.
        # The exception already carries `path` (set inside _request_with_status
        # from the URL path); but bridge.write_file_raw was invoked with a
        # bare vault-relative path, so prefer that for downstream consumers
        # that key on it.
        raise
    except ObsidianTimeout as exc:
        # Body may have been sent — translate to post-write-uncertain so
        # the gateway-side verifier can decide whether the write landed.
        # This closes the latent double-write hazard: a real-but-unacked
        # write is verified and returned as success; a never-sent write
        # is recognised as absent and re-enqueued.
        raise ObsidianPostWriteUncertain(
            path, content_hint=hint, write_mode=write_mode,
        ) from exc
    except ObsidianHTTPError as exc:
        # 4xx other than 409, or 5xx. Transitional: log + return False so
        # legacy TRANSLATE-pattern callers continue to work. CP6 removes
        # this shim and re-raises typed.
        logger.warning(
            "Bridge write failed: status=%d body=%r path=%s",
            exc.status, exc.body, path,
        )
        return False
    except ObsidianUnreachable:
        # Connection refused / not running. Body was NOT sent — safe to
        # return False (no double-write risk). Caller's fallback (e.g.
        # vault_write's filesystem path) takes over. CP6 removes this
        # shim too.
        return False


@requires_consent(
    operation="obsidian.write_file",
    reason="Write or create a file in the Obsidian vault.",
    risk="moderate",
    default_ttl=15,
)
def write_file(path: str, content: str) -> bool:
    """Write or create a file by vault-relative path (consent-gated).

    Returns True on success, False on failure.
    """
    return write_file_raw(path, content)


def get_metadata(path: str) -> dict | None:
    """Get cached metadata for a file (frontmatter, tags, links, headings, etc.).

    Returns metadata dict, or None if not found / bridge unavailable.
    """
    encoded = urllib.parse.quote(path, safe="/")
    result = _request("GET", f"/metadata/{encoded}")
    if result is None:
        return None
    return result.get("metadata")


def search(query: str) -> list[dict]:
    """Search vault files by name or content.

    Returns list of {path, match} dicts.
    """
    encoded = urllib.parse.quote(query)
    result = _request("GET", f"/search?q={encoded}")
    if result is None:
        return []
    return result.get("results", [])


@requires_consent(
    operation="obsidian.eval_js",
    reason="Execute arbitrary JavaScript inside Obsidian with full Plugin API access.",
    risk="high",
    default_ttl=10,
)
def eval_js(code: str, timeout: int = 15) -> Any:
    """Execute arbitrary JavaScript inside Obsidian with access to the app object.

    The code is wrapped in an async function. Use 'return' to produce a result.
    Example: eval_js("return app.vault.getMarkdownFiles().length")

    Args:
        code: JavaScript code to execute.
        timeout: HTTP request timeout in seconds (should exceed the plugin's eval timeout).

    Returns the result value, or None on failure.
    """
    result = _request("POST", "/eval", {"code": code}, timeout=timeout)
    if result is None:
        return None
    if "error" in result:
        raise RuntimeError(f"Eval error: {result['error']}")
    return result.get("result")


# ── Workspace ──────────────────────────────────────────────────


def get_workspace() -> dict | None:
    """Get current workspace state: open tabs, active file, pane layout.

    Returns dict with 'active_file', 'open_files', etc., or None if unavailable.
    """
    result = _request("GET", "/workspace")
    if result is None:
        return None
    return result
