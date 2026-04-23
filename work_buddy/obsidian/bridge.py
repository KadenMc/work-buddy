"""Python client for the work-buddy Obsidian plugin bridge.

Talks to the HTTP server running inside Obsidian via the Work Buddy plugin.
Follows the same pattern as work_buddy.messaging.client.
"""

import json
import platform
import subprocess
import time
import urllib.parse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EditorConflict(Exception):
    """Bridge refused a write because an open editor has unsaved changes.

    Raised by ``write_file_raw`` after the in-bridge retry schedule
    (5s / 10s / 20s) is exhausted and the target file's editor still
    has uncommitted typing. Callers should treat this as a "retry
    later" signal — pushing to the existing retry queue is the right
    response for programmatic writes; agents can choose to surface it
    via ``wb_notify``.

    Critically: callers MUST NOT fall back to direct filesystem writes
    when this is raised. The whole point is that the user has unsaved
    work in their editor; a direct write would still be clobbered the
    moment they save.
    """

    def __init__(self, path: str, reason: str = "editor_dirty"):
        self.path = path
        self.reason = reason
        super().__init__(f"editor_dirty: {path}")

from work_buddy.config import load_config
from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger

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


def _request_with_status(
    method: str,
    path: str,
    data: dict | str | None = None,
    timeout: int = 10,
) -> tuple[int | None, dict | None]:
    """Make a bridge request returning (status_code, body).

    Distinct from ``_request`` because some callers — namely
    ``write_file_raw`` — need to distinguish HTTP status codes
    (specifically 409 Conflict) rather than collapsing every
    non-2xx response into ``None``.

    Returns ``(None, None)`` on network failure (bridge down,
    socket timeout). Returns ``(status, body_or_None)`` for any
    HTTP response — caller is responsible for status handling.
    No retries; caller orchestrates them.
    """
    url = f"{_base_url()}{path}"
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data).encode("utf-8")
        else:
            body = data.encode("utf-8")

    global _last_success_ts, _last_success_ms, _consecutive_failures
    global _last_failure_reason, _last_failure_kind, _last_failure_status

    req = Request(url, data=body, method=method)
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
            payload = resp.read().decode("utf-8")
            return resp.status, json.loads(payload) if payload else None
    except HTTPError as exc:
        # 4xx/5xx — server reachable, structured response. Read the
        # body if any so the caller can act on the error reason.
        try:
            payload = exc.read().decode("utf-8")
            err_body = json.loads(payload) if payload else None
        except Exception:
            err_body = None
        # 4xx is a structured refusal, not a bridge fault — don't bump
        # _consecutive_failures (latency tracking is for connectivity,
        # not application-level conflict).
        _last_failure_kind = "http_error"
        _last_failure_status = exc.code
        return exc.code, err_body
    except (TimeoutError, URLError, OSError) as exc:
        _consecutive_failures += 1
        _last_failure_reason = type(exc).__name__
        # Classify for the four-state taxonomy.
        #   TIMEOUT (state 2: bridge lagging)   — TCP connected, HTTP hung.
        #   UNREACHABLE (states 1/3/4)          — TCP never connected.
        # On Windows, urllib wraps ConnectionRefusedError inside URLError
        # and the stringification often contains the word "timed out"
        # even though the socket was refused — so we inspect the
        # underlying exception class via .reason, not the message.
        underlying: BaseException = exc
        if isinstance(exc, URLError) and exc.reason is not None:
            underlying = exc.reason if isinstance(exc.reason, BaseException) else exc
        if isinstance(underlying, ConnectionError):
            _last_failure_kind = "unreachable"
        elif isinstance(underlying, TimeoutError):
            # Ambiguous: could be (a) HTTP hung while TCP is open (real
            # state-2 timeout) or (b) the TCP connect itself timed out
            # because the port isn't listening (states 1/3/4 — Windows
            # often surfaces closed ports as socket timeouts rather
            # than ECONNREFUSED). Probe the TCP layer to disambiguate.
            _last_failure_kind = "timeout" if _probe_port_open() else "unreachable"
        else:
            # Last-resort disambiguation via TCP probe.
            _last_failure_kind = "unreachable" if not _probe_port_open() else "timeout"
        _last_failure_status = None
        logger.warning(
            "Bridge request failed: %s %s — %s [%s]",
            method, path, exc, get_latency_context(),
        )
        return None, None


def write_file_raw(path: str, content: str) -> bool:
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
    the user's in-flight edits. We raise ``EditorConflict`` immediately
    on the first 409 instead of retrying inside this function: the
    payload we'd send on retry is the *same* bytes the caller composed
    minutes ago, so even after the user's typing auto-saves to disk,
    a bridge-level retry would clobber the saved typing with stale
    content. Re-doing the read-modify-write is the caller's job.

    The right place for that retry is the gateway's transient-error
    auto-enqueue: ``EditorConflict`` is classified transient, and any
    capability with ``retry_policy`` ``replay`` or ``verify_first``
    will be re-invoked from scratch by the sidecar's retry sweep
    (work_buddy/sidecar/retry_sweep.py) on adaptive backoff. Each
    re-invocation reads the file fresh and recomputes the payload.

    Callers MUST NOT swallow ``EditorConflict`` into a direct filesystem
    write — see the ``EditorConflict`` docstring.

    Returns
    -------
    True on success. False on transport failure (bridge down, timeout,
    server-side 5xx). Raises ``EditorConflict`` on a 409 from the plugin.

    Bridge latency: uses a 15s per-request timeout (bridge has documented
    multi-second latency spikes especially on creates with large payloads).
    """
    encoded = urllib.parse.quote(path, safe="/")

    status, body = _request_with_status(
        "PUT", f"/files/{encoded}", {"content": content}, timeout=15,
    )

    if status is None:
        # Network/timeout failure — different failure mode from editor
        # conflict. Surface as False so the existing fallback / retry
        # machinery can handle it.
        return False

    if status in (200, 201):
        return True

    if status == 409:
        raise EditorConflict(path)

    # Other 4xx/5xx — structural failure.
    logger.warning(
        "Bridge write failed: status=%d body=%r path=%s",
        status, body, path,
    )
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
