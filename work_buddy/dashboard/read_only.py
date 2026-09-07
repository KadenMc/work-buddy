"""Dashboard request policy and an opt-in read-only process boundary."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from work_buddy.storage.read_only import enable_process_read_only, process_read_only

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

READ_ONLY_EXCEPTIONS = {
    "internal_bus": "Publishes only to bounded in-memory SSE subscriber queues; no persistence or domain callbacks.",
}
HOST_CALLBACK_EXCEPTIONS = {
    "internal_bus": "events.publish_cross_process forwards an ephemeral event from a local process.",
    "api_workflow_views_create": "notifications.surfaces.dashboard delivers a workflow view from the sidecar.",
    "api_workflow_view_dismiss": "notifications and conversations host adapters dismiss a workflow view.",
    "api_notification_log_add": "mcp_server.ops.notifications_ops records notification delivery.",
    "api_dashboard_interact": "mcp_server.ops.sidecar_ops forwards host dashboard interaction requests.",
}
SESSION_EXCEPTIONS = {
    "local_identity.redeem_bootstrap": "Redeems a host-minted grant before a session exists.",
    "local_identity.refresh_session_csrf": "Reports normal unauthenticated recovery and checks exact Origin itself.",
}

_connect = sqlite3.connect


def is_read_only() -> bool:
    if process_read_only():
        return True
    from flask import current_app, has_app_context
    if has_app_context() and "dashboard_read_only" in current_app.extensions:
        return bool(current_app.extensions["dashboard_read_only"]())
    from work_buddy.config import load_config
    return bool(load_config().get("dashboard", {}).get("read_only", False))


def reject_read_only():
    """Return a consistent refusal for route-level defense in depth."""
    if is_read_only():
        from flask import jsonify
        message = "Dashboard is in read-only mode"
        return jsonify({"ok": False, "error": message, "code": "read_only", "message": message}), 403
    return None


def register_session_exception(app, endpoint: str, reason: str) -> None:
    """Register an independently authenticated host control, never a write exception."""
    if endpoint not in app.view_functions or not reason.strip():
        raise ValueError("A session exception must name an existing endpoint and its authentication reason")
    app.extensions.setdefault("dashboard_session_exceptions", {})[endpoint] = reason


def validate_exception_registry(app) -> None:
    """Reject stale or unexplained request-policy declarations."""
    entries = {**READ_ONLY_EXCEPTIONS, **SESSION_EXCEPTIONS, **HOST_CALLBACK_EXCEPTIONS, **app.extensions.get("dashboard_session_exceptions", {})}
    for endpoint, reason in entries.items():
        if endpoint not in app.view_functions or not reason.strip():
            raise ValueError(f"Invalid dashboard request exception: {endpoint}")


def guard_dashboard_request():
    """Refuse non-safe methods before any domain lookup or request hook."""
    from flask import current_app, jsonify, request
    if request.method in SAFE_METHODS:
        return None
    if is_read_only() and request.endpoint not in READ_ONLY_EXCEPTIONS:
        return reject_read_only()
    if request.endpoint in SESSION_EXCEPTIONS or request.endpoint in current_app.extensions.get("dashboard_session_exceptions", {}):
        return None
    if request.endpoint in HOST_CALLBACK_EXCEPTIONS and is_host_callback_request():
        return None
    from work_buddy.dashboard.local_identity_api import authenticate_request_session
    from work_buddy.security.local_identity import LocalIdentityError
    try:
        authenticate_request_session(allow_rotation_due=True, touch=False)
    except LocalIdentityError:
        return jsonify({"ok": False, "error": "An authenticated local session is required.", "code": "session_required"}), 403
    return None


def is_host_callback_request() -> bool:
    """Preserve the existing local-process trust boundary, excluding browser traffic.

    Absence of Origin alone is not authentication. These named routes already
    accept local host publishers; remote peers and browser fetches cannot use
    the exception. Domain routes still enforce their own authority checks.
    """
    from flask import request
    from work_buddy.dashboard.local_identity_api import boundary_for_request
    from work_buddy.security.local_identity import LocalIdentityError, _request_origin
    if any(
        name.lower() in {"origin", "referer", "forwarded", "via", "x-real-ip"}
        or name.lower().startswith(("sec-fetch-", "x-forwarded-", "tailscale-"))
        for name in request.headers.keys()
    ):
        return False
    try:
        _request_origin(boundary_for_request(), require_origin=False)
    except LocalIdentityError:
        return False
    return True


def readonly_database(database) -> tuple[object, bool]:
    """Return a URI that preserves URI options while forcing on-disk read-only."""
    value = os.fsdecode(database)
    if value == ":memory:" or (value.startswith("file:") and (urlsplit(value).path == ":memory:" or dict(parse_qsl(urlsplit(value).query)).get("mode") == "memory")):
        return database, value.startswith("file:")
    if not value:
        # SQLite's anonymous temporary database is memory-like and not authority.
        return database, False
    if value.startswith("file:"):
        parsed = urlsplit(value)
        query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key not in {"mode", "immutable", "nolock"}]
        query.append(("mode", "ro"))
        return urlunsplit(parsed._replace(query=urlencode(query))), True
    return Path(value).expanduser().resolve().as_uri() + "?mode=ro", True


def _authorize_sql(action, argument, value, _database, _source):
    if action == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and str(argument).lower() == "query_only" and value is not None and str(value).lower() not in {"1", "on", "true", "yes"}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _readonly_connect(database, *args, **kwargs):
    database, uri = readonly_database(database)
    positional = list(args)
    if len(positional) >= 7:
        positional[6] = uri
        kwargs.pop("uri", None)
    else:
        kwargs["uri"] = uri
    connection = _connect(database, *positional, **kwargs)
    connection.execute("PRAGMA query_only = ON")
    connection.set_authorizer(_authorize_sql)
    return connection


def install_sqlite_read_only() -> None:
    """Install before importing stores so all SQLite connections share the policy."""
    enable_process_read_only()
    sqlite3.connect = _readonly_connect
    sqlite3.dbapi2.connect = _readonly_connect


class ReadOnlyProcessError(PermissionError):
    """An operation requires a write-capable dashboard process."""


def _deny_process_side_effects(event, args):
    if event == "open":
        _path, mode, flags = args
        if isinstance(_path, int) and stat.S_ISFIFO(os.fstat(_path).st_mode):
            return
        if isinstance(_path, (str, bytes, os.PathLike)) and os.path.normcase(os.fsdecode(_path)) == os.path.normcase(os.devnull):
            return
        if (mode and any(character in mode for character in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise ReadOnlyProcessError("File writes are disabled in the read-only dashboard process")
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.chmod", "os.truncate", "os.utime", "os.link", "os.symlink"}:
        if event == "os.mkdir" and Path(args[0]).is_dir():
            return
        raise ReadOnlyProcessError("Filesystem changes are disabled in the read-only dashboard process")
    elif event == "subprocess.Popen" and _is_document_kernel(args[1]):
        # The bundled kernel consumes structured input and returns bytes over pipes.
        return
    elif event in {"subprocess.Popen", "os.system", "os.spawn", "socket.connect"}:
        raise ReadOnlyProcessError("External execution and outbound connections are disabled in the read-only dashboard process")


def _is_document_kernel(command) -> bool:
    worker = Path(__file__).resolve().parents[1] / "document_kernel" / "runtime_dist" / "worker.mjs"
    if isinstance(command, str):
        # Windows emits the already-quoted command line in the Popen audit event.
        import shutil
        import subprocess
        node = shutil.which("node")
        return node is not None and command == subprocess.list2cmdline([node, str(worker)])
    if not isinstance(command, (list, tuple)) or len(command) != 2:
        return False
    return Path(command[0]).name.lower() in {"node", "node.exe"} and Path(command[1]).resolve() == worker


def install_process_read_only() -> None:
    """Enforce SQLite plus Python file, child-process, and network side effects."""
    install_sqlite_read_only()
    sys.dont_write_bytecode = True
    sys.addaudithook(_deny_process_side_effects)


def serve(*, port: int | None) -> int:
    """Serve the existing dashboard on an explicit, separate loopback port."""
    if port is None or not 1 <= port <= 65535 or port in {5124, 5126, 5127}:
        raise ValueError("Read-only dashboard requires an explicit port outside the normal service ports")
    install_process_read_only()
    from work_buddy.dashboard.service import app
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    return 0
