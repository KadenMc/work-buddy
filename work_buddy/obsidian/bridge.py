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
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from work_buddy.config import load_config
from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger

# Re-export the typed Obsidian exceptions at the bridge module level so
# callers can ``from work_buddy.obsidian.bridge import ObsidianEditorConflict``
# without a separate import from work_buddy.obsidian.errors. The legacy
# ``EditorConflict`` alias was removed in CP9 — use ``ObsidianEditorConflict``.
from work_buddy.obsidian.errors import (
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
      ``"plugin_disabled"`` (state 4), ``"obsidian_startup_race"``
      (Obsidian + plugin up but the port hasn't bound yet — non-terminal,
      worth retrying), ``"http_error"`` (non-2xx / non-409 response from
      the bridge), or ``"unknown"`` (filesystem check couldn't resolve the
      vault). The terminal states (retrying won't help) are
      ``obsidian_not_running`` / ``plugin_not_installed`` /
      ``plugin_disabled``; ``obsidian_startup_race`` is explicitly NOT
      terminal.
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
    # host down. Delegate the state-1/3/4 + startup-race disambiguation to
    # the single source of truth shared with _refine_unreachable_kind, so
    # the string-state and typed-exception representations can never drift.
    leaf = _classify_unreachable()
    return {
        "state": leaf.state,
        "detail": leaf.detail,
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


@dataclass(frozen=True)
class _UnreachableLeaf:
    """One leaf of the 'why is the bridge unreachable' decision tree.

    Carries all three representations so the string-state classifier
    (:func:`get_last_bridge_state`) and the typed-exception classifier
    (:func:`_refine_unreachable_kind`) derive from a single decision and
    can never drift:

    * ``exc_type`` — the ObsidianUnreachable subclass to raise.
    * ``state`` — the legacy four-state-taxonomy string.
    * ``detail`` — the rendered human-readable one-liner.
    """

    exc_type: type[ObsidianUnreachable]
    state: str
    detail: str


def _classify_unreachable() -> _UnreachableLeaf:
    """Single source of truth for 'why is the bridge unreachable'.

    Runs the process check (``is_obsidian_running``) and, if needed, the
    filesystem plugin-state check (``get_work_buddy_plugin_state``) once,
    and maps the result to a leaf carrying the exception type, the
    four-state-taxonomy string, and a human-readable detail. Both
    :func:`get_last_bridge_state` (string + detail) and
    :func:`_refine_unreachable_kind` (exception type) derive from this, so
    the two representations stay in lock-step.

    Only reached on ``_last_failure_kind == "unreachable"`` (TCP refused).
    The cost of the process + filesystem checks is paid only on connection
    failures, so the slow-path is fine. Returns the base
    ``ObsidianUnreachable`` / ``"unknown"`` if a check itself errors —
    better a less-specific classification than masking the original
    failure with a secondary one.
    """
    try:
        running = is_obsidian_running()
    except Exception:
        return _UnreachableLeaf(
            ObsidianUnreachable,
            "unknown",
            "Bridge unreachable; could not determine whether Obsidian is running.",
        )

    if not running:
        return _UnreachableLeaf(
            ObsidianNotRunning,
            "obsidian_not_running",
            "Obsidian is not running (port 27125 unreachable, Obsidian.exe not found).",
        )

    try:
        from work_buddy.health.requirement_checks import get_work_buddy_plugin_state
        plugin_state, plugin_detail = get_work_buddy_plugin_state()
    except Exception as exc:
        return _UnreachableLeaf(
            ObsidianUnreachable,
            "unknown",
            f"Unable to inspect plugin state: {exc}",
        )

    if plugin_state == "not_installed":
        return _UnreachableLeaf(
            ObsidianPluginMissing,
            "plugin_not_installed",
            (
                "Obsidian is running but the work-buddy plugin is not "
                f"installed ({plugin_detail}). Install from "
                "https://github.com/KadenMc/obsidian-work-buddy."
            ),
        )

    if plugin_state == "disabled":
        return _UnreachableLeaf(
            ObsidianPluginDisabled,
            "plugin_disabled",
            (
                "Obsidian is running and the plugin is installed but not "
                f"enabled ({plugin_detail}). Open Obsidian → Settings → "
                "Community Plugins and toggle 'Work Buddy' on."
            ),
        )

    if plugin_state == "ok":
        # Plugin enabled but port still refused — the startup-race window
        # (Obsidian just started, the plugin's HTTP listener hasn't bound
        # yet) or a port-binding error. Non-terminal: worth a retry.
        return _UnreachableLeaf(
            ObsidianStartupRace,
            "obsidian_startup_race",
            (
                "Bridge port refused connection despite Obsidian appearing "
                "to be running with the plugin enabled — Obsidian may still "
                "be starting up, or the plugin failed to bind to port 27125."
            ),
        )

    # plugin_state == "unknown" or anything else — generic unreachable.
    return _UnreachableLeaf(
        ObsidianUnreachable,
        "unknown",
        f"Bridge unreachable; plugin state could not be resolved: {plugin_detail}.",
    )


def _refine_unreachable_kind() -> type[ObsidianUnreachable]:
    """Pick the most specific ObsidianUnreachable subclass for the current state.

    Thin derivation over :func:`_classify_unreachable` (the single source
    of truth shared with :func:`get_last_bridge_state`), so the typed and
    string-state representations cannot diverge. Returns the base
    ``ObsidianUnreachable`` if the disambiguation helpers themselves error.
    """
    return _classify_unreachable().exc_type


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

    Post-write uncertainty
    ----------------------
    A client-side timeout AFTER the PUT body has been sent is
    ambiguous: the plugin may have committed the write before the
    response failed to arrive. We raise :class:`ObsidianPostWriteUncertain`,
    carrying ``(path, content_hint, write_mode)`` so the gateway can
    verify-then-decide via :func:`work_buddy.obsidian.post_write_verify.verify_post_write`.
    This closes the latent double-write hazard: replaying a successful-
    but-unacknowledged write would silently insert content twice.

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
        True on 2xx success. The bool return is preserved (rather than
        ``None``) for backward compatibility with callers that
        ``if write_file_raw(...): ...``-pattern check the return.

    Raises:
        :class:`ObsidianEditorConflict` on 409 (file open with unsaved typing).
        :class:`ObsidianPostWriteUncertain` on PUT timeout (port open).
        :class:`ObsidianRefused` on 4xx other than 409.
        :class:`ObsidianServerError` on 5xx.
        :class:`ObsidianUnreachable` (or specific subclass) on TCP refused.

    Post-CP6 the bool/exception split is final: success returns True;
    every failure raises a typed exception. The transitional shim from
    CP2 (which translated 4xx/5xx/unreachable to ``False``) is gone.
    Callers must either handle the typed exceptions explicitly or be
    wrapped by :func:`work_buddy.obsidian.retry.bridge_retry`, which
    catches typed exceptions and translates them at exhaustion.

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
    except ObsidianTimeout as exc:
        # Body may have been sent — translate to post-write-uncertain so
        # the gateway-side verifier can decide whether the write landed.
        # ObsidianPostWriteUncertain inherits from ObsidianTimeout, so
        # this catch matches plain timeouts AND post-write-uncertain
        # subclasses; we always wrap into the post-write variant here
        # because we have the (path, content_hint, write_mode) context.
        raise ObsidianPostWriteUncertain(
            path, content_hint=hint, write_mode=write_mode,
        ) from exc
    # Other ObsidianError subclasses (ObsidianEditorConflict,
    # ObsidianRefused, ObsidianServerError, ObsidianUnreachable and
    # subclasses) propagate unchanged. Callers are responsible — either
    # they have a domain-specific recovery (vault_write's filesystem
    # fallback for ObsidianUnreachable on reads), or they're inside
    # @bridge_retry which translates at exhaustion, or the gateway's
    # outer try/except classifies and enqueues.


@requires_consent(
    operation="obsidian.write_file",
    reason="Write or create a file in the Obsidian vault.",
    risk="moderate",
    default_ttl=15,
)
def write_file(
    path: str,
    content: str,
    *,
    write_mode: str = "replace",
    content_hint: str | None = None,
) -> bool:
    """Write or create a file by vault-relative path (consent-gated).

    Slice C.4: ``write_mode`` and ``content_hint`` kwargs surface
    write_file_raw's verification controls to consent-gated callers.
    For files that change concurrently (the master task list, archives,
    journals), prefer ``write_mode="insert"`` with ``content_hint``
    set to a unique substring of the inserted content (e.g. the new
    task line). This makes the post-write verifier do a substring
    match instead of a full-content sha256 — robust against
    concurrent unrelated edits to other parts of the file. Without
    this kwarg surface, callers had to choose between (a) using
    write_file_raw directly (bypassing consent) or (b) accepting
    sha256-based verification that fails any time the file changes
    between write and verify.

    Returns True on success, False on failure.
    """
    return write_file_raw(
        path, content, write_mode=write_mode, content_hint=content_hint,
    )


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
    return eval_js_internal(code, timeout=timeout)


def eval_js_internal(code: str, timeout: int = 15) -> Any:
    """Internal eval_js without the human-consent gate.

    For use only by bridge-internal helpers (e.g. atomic vault-write paths)
    whose calling capability ALREADY holds an equivalent or stronger
    consent (typically ``obsidian.write_file`` — the atomic-write helper
    is semantically a write, not arbitrary JS execution). Skipping a
    second ``obsidian.eval_js`` prompt avoids double-consenting the user
    for what is effectively one operation.

    Mirrors :func:`eval_js`'s return contract: the value the JS produced,
    or None on bridge failure. Raises :class:`RuntimeError` if the JS
    threw.

    Args:
        code: JavaScript code to execute.
        timeout: HTTP request timeout in seconds.
    """
    result = _request("POST", "/eval", {"code": code}, timeout=timeout)
    if result is None:
        return None
    if "error" in result:
        raise RuntimeError(f"Eval error: {result['error']}")
    return result.get("result")


def eval_js_for_write(
    code: str,
    *,
    write_path: str,
    content_hint: str,
    write_mode: str = "insert",
    timeout: int = 15,
) -> Any:
    """eval_js for atomic-write paths — translates timeouts to PWU.

    Difference from :func:`eval_js_internal`: routes through
    :func:`_request_with_status` (typed exceptions) instead of
    :func:`_request` (silent None). On HTTP timeout, raises
    :class:`ObsidianPostWriteUncertain` carrying the write_path and a
    content_hint so the gateway's CP-A7 verify-then-decide path can
    determine whether the JS callback successfully mutated the vault.

    Why this matters: the JS body inside ``code`` typically calls
    ``app.vault.process(...)`` which CAN have committed the modify
    callback before the bridge's HTTP response failed to arrive. Without
    this translation, eval_js_internal silently returns None on
    timeout, the atomic-write helper falls through to the legacy
    ``bridge.write_file`` path, and the conflict-detection / atomic
    semantics are bypassed — exactly the regression Slice C was meant
    to prevent.

    Mirrors :func:`write_file_raw`'s post-write-uncertain handling.

    Args:
        code: JavaScript body to execute.
        write_path: Vault-relative path the JS is mutating. Used by the
            verify-from-filesystem recovery to pick the file to inspect.
        content_hint: Substring witness — text the verifier expects to
            find (or NOT find, for ``write_mode="absent"``) in the file
            after the JS callback runs successfully. For line-replace
            operations the new line itself is the natural hint (unique
            by task_id). For line-delete operations use the unique
            substring of the line being removed (e.g. the task_id
            marker).
        write_mode: Verification semantics for the post-write recovery
            path:
            - ``"insert"`` (default) — verified iff hint IS present
              (line-replace, line-insert, append).
            - ``"absent"`` — verified iff hint IS NOT present
              (line-delete and other "make this go away"
              operations). Without this, a successful delete reads
              as "didn't land" via insert-mode substring verify.
            - ``"replace"`` — full sha256 match (rare for atomic
              eval-driven writes since JS computes the new content;
              caller would need to know the post-callback content
              ahead of time).
        timeout: HTTP request timeout in seconds.

    Returns:
        The value the JS produced.

    Raises:
        :class:`ObsidianPostWriteUncertain` on HTTP timeout — carries
            (write_path, content_hint, write_mode) so verify_post_write
            applies the right detection mode.
        :class:`ObsidianTimeout` if the bridge port was open but no
            data was sent (callable picks this up as a regular
            transient failure).
        :class:`ObsidianUnreachable` (or specific subclass) on TCP
            refused.
        :class:`RuntimeError` if the JS body threw inside Obsidian.
    """
    try:
        status, body = _request_with_status(
            "POST", "/eval", {"code": code}, timeout=timeout,
        )
    except ObsidianTimeout as exc:
        # The eval body was sent; the JS callback may have committed
        # the vault.process() write before the response failed to
        # arrive. Translate to PWU so the gateway can verify-then-decide
        # whether to retry.
        raise ObsidianPostWriteUncertain(
            write_path,
            content_hint=content_hint,
            write_mode=write_mode,
        ) from exc
    if body is None:
        return None
    if "error" in body:
        raise RuntimeError(f"Eval error: {body['error']}")
    return body.get("result")


def atomic_replace_line_by_task_id(
    file_path: str,
    task_id: str,
    expected_old_line: str,
    new_line: str,
    *,
    timeout: int = 15,
) -> dict[str, Any]:
    """Atomically rewrite the task line for ``task_id`` via app.vault.process().

    Uses Obsidian's ``app.vault.process(file, callback)`` — the canonical
    atomic read-modify-write API. The callback runs against the
    *current* file content (as Obsidian sees it, not Python's stale
    read), so the read-modify-write race in the legacy bridge.read_file
    + bridge.write_file pair is closed.

    Conflict detection: ``expected_old_line`` is what the caller read.
    If the line matching ``task_id`` in the *fresh* content differs (user
    edited between Python's read and this call), the JS callback returns
    the data unchanged and surfaces ``conflict=True`` in the response.
    The caller decides whether to retry, surface to user, or accept.

    Args:
        file_path: Vault-relative file path.
        task_id: Task ID to locate (matched by ``🆔 <task_id>`` substring).
        expected_old_line: Line content the caller read. Empty string
            disables conflict detection (use cautiously).
        new_line: Replacement line content.
        timeout: HTTP timeout in seconds.

    Returns:
        Dict with:
          - ``found`` (bool): True if the task_id was located in the file.
          - ``conflict`` (bool): True if the located line differed from
            ``expected_old_line`` (caller's read was stale).
          - ``replaced`` (bool): True iff the file was actually modified.
          - ``line_number`` (int | None): 1-indexed line of the match.
          - ``old_line`` (str | None): The line as it was *just before*
            the atomic write (the version JS saw, not the version
            Python read).
          - ``new_line`` (str | None): The line as written. Only set
            when ``replaced=True``.

    Raises the same typed Obsidian exceptions as ``eval_js_internal``
    on bridge failure (timeout, unreachable, plugin not loaded). The
    caller is responsible for fallback decisions — typically falling
    back to the legacy read-modify-write path if the atomic write
    fails for connectivity reasons.

    Consent: bypasses ``obsidian.eval_js`` consent. Callers must hold
    ``obsidian.write_file`` consent (or higher) — semantically this IS
    a write_file with a transform attached.
    """
    payload = json.dumps({
        "path": file_path,
        "task_id": task_id,
        "expected_old_line": expected_old_line or "",
        "new_line": new_line,
    })
    # Raw f-string so the `\u` JS escape isn't interpreted as a Python
    # unicode escape during parse. The JS body uses `\u{1F194}` (the 🆔
    # character) as a literal escape that the JS engine resolves.
    js = rf"""
return (async () => {{
    const params = {payload};
    const file = app.vault.getAbstractFileByPath(params.path);
    if (!file) {{
        return {{found: false, conflict: false, replaced: false, error: "file_not_found"}};
    }}
    const id_pattern = "\u{{1F194}} " + params.task_id;
    let result = {{found: false, conflict: false, replaced: false, line_number: null, old_line: null, new_line: null}};

    await app.vault.process(file, (data) => {{
        const lines = data.split("\n");
        for (let i = 0; i < lines.length; i++) {{
            if (lines[i].includes(id_pattern)) {{
                result.found = true;
                result.line_number = i + 1;
                result.old_line = lines[i];
                // Conflict check: caller's expected_old_line vs. fresh content.
                if (params.expected_old_line && lines[i] !== params.expected_old_line) {{
                    result.conflict = true;
                    return data;  // unchanged
                }}
                if (lines[i] === params.new_line) {{
                    // No-op rewrite; don't dirty the file.
                    return data;
                }}
                lines[i] = params.new_line;
                result.replaced = true;
                result.new_line = params.new_line;
                return lines.join("\n");
            }}
        }}
        return data;  // not found — unchanged
    }});
    return result;
}})()
"""

    # Route through the write-aware eval path so HTTP timeouts translate
    # into ObsidianPostWriteUncertain (with content_hint=new_line) instead
    # of silently returning None. Without this, the silent-None on
    # timeout caused atomic_replace to fall through to the legacy
    # bridge.write_file path, defeating Slice C's whole purpose. The
    # gateway's CP-A7 verify-then-decide path picks up the PWU and
    # checks whether the new_line is in the on-disk file.
    result = eval_js_for_write(
        js,
        write_path=file_path,
        content_hint=new_line,
        timeout=timeout,
    )
    if result is None:
        # Genuinely None (eval body returned undefined / null) —
        # treat as bridge_returned_none for fallback purposes.
        return {
            "found": False,
            "conflict": False,
            "replaced": False,
            "error": "bridge_returned_none",
        }
    return result


def atomic_delete_line_by_task_id(
    file_path: str,
    task_id: str,
    *,
    timeout: int = 15,
) -> dict[str, Any]:
    """Atomically remove the task line for ``task_id`` via app.vault.process().

    Mirrors :func:`atomic_replace_line_by_task_id` but the JS callback
    REMOVES the matched line entirely instead of replacing it. Used by
    ``mutations.delete_task`` so the line-removal step gets the same
    race-safety as the description-update path.

    Args:
        file_path: Vault-relative file path.
        task_id: Task ID to locate (matched by ``🆔 <task_id>`` substring).
        timeout: HTTP timeout in seconds.

    Returns:
        Dict with:
          - ``found`` (bool): True if the task_id was located.
          - ``removed`` (bool): True iff the file was actually modified.
          - ``line_number`` (int | None): 1-indexed line of the match
            BEFORE removal.
          - ``old_line`` (str | None): The line that was removed.

    Raises the same typed Obsidian exceptions as
    :func:`eval_js_for_write` on bridge failure. On timeout, raises
    :class:`ObsidianPostWriteUncertain` with content_hint of
    ``f"🆔 {task_id}"`` and ``write_mode="absent"``. The verifier's
    "absent" mode treats hint-NOT-present as verified (since this is
    a removal operation) — without it, a successful delete would
    read as "didn't land" via the default insert/substring semantics
    and trigger spurious retries. See :func:`verify_post_write`'s
    "absent" branch.

    Consent: bypasses ``obsidian.eval_js`` consent. Callers must hold
    ``obsidian.write_file`` consent (or higher) — this IS a write_file
    with a transform attached.
    """
    payload = json.dumps({
        "path": file_path,
        "task_id": task_id,
    })
    js = rf"""
return (async () => {{
    const params = {payload};
    const file = app.vault.getAbstractFileByPath(params.path);
    if (!file) {{
        return {{found: false, removed: false, error: "file_not_found"}};
    }}
    const id_pattern = "\u{{1F194}} " + params.task_id;
    let result = {{found: false, removed: false, line_number: null, old_line: null}};

    await app.vault.process(file, (data) => {{
        const lines = data.split("\n");
        for (let i = 0; i < lines.length; i++) {{
            if (lines[i].includes(id_pattern)) {{
                result.found = true;
                result.line_number = i + 1;
                result.old_line = lines[i];
                lines.splice(i, 1);
                result.removed = true;
                return lines.join("\n");
            }}
        }}
        return data;  // not found — unchanged
    }});
    return result;
}})()
"""

    # Content hint for the post-write verifier: the task_id marker. After
    # a successful removal, the marker should NOT be in the file. The
    # current verify_post_write does `hint in content` which means
    # absence-after-write reads as "absent" → retry. That's actually
    # the correct behavior here too: if a verify-after-PWU finds the
    # marker still in the file, the removal didn't land and a retry
    # is correct. If the marker is gone, verify says "absent" which
    # the gateway treats as "didn't land" — but the retry is now
    # idempotent because the second attempt's atomic find won't find
    # the line, and returns found=false (still success in delete
    # semantics).
    hint = f"🆔 {task_id}"
    result = eval_js_for_write(
        js,
        write_path=file_path,
        content_hint=hint,
        write_mode="absent",  # delete: verified iff hint NOT in content
        timeout=timeout,
    )
    if result is None:
        return {
            "found": False,
            "removed": False,
            "error": "bridge_returned_none",
        }
    return result


# ── Workspace ──────────────────────────────────────────────────


def get_workspace() -> dict | None:
    """Get current workspace state: open tabs, active file, pane layout.

    Returns dict with 'active_file', 'open_files', etc., or None if unavailable.
    """
    result = _request("GET", "/workspace")
    if result is None:
        return None
    return result
