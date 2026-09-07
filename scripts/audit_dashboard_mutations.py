"""Audit dashboard route guards against a disposable application root.

Enumerates the runtime Flask URL map, drives each non-safe method, and records
which read-only or human-authority guards execute. No running dashboard is used.

Run via::

    uv run python -m scripts.audit_dashboard_mutations --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
GUARDS = {
    "_reject_read_only": "read_only",
    "reject_read_only": "read_only",
    "guard_dashboard_request": "chokepoint",
    "require_human_authority_request": "gesture",
    "authorize_human_mutation": "gesture",
}


def route_url(rule, values: dict | None = None) -> str:
    """Build a concrete URL using valid converter values and fixture identities."""
    values = values or {}
    def replace(match):
        spec = match.group(1)
        kind, _, name = spec.rpartition(":")
        if name in values:
            return str(values[name])
        if kind in {"int", "float"}:
            return "1"
        if kind == "uuid":
            return "00000000-0000-4000-8000-000000000001"
        return "audit-missing"
    return re.sub(r"<([^>]+)>", replace, rule.rule)


def snapshot(root: Path) -> dict[str, str]:
    """Hash files and record directories, including non-SQLite side effects."""
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative + "/"] = "directory"
        elif path.is_file():
            try:
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                result[relative] = f"unreadable:{type(exc).__name__}"
    return result


def changes(before: dict, after: dict) -> list[str]:
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def audit_rule(app, rule, method: str, *, root: Path, values: dict | None = None) -> dict:
    """Probe one route and return its observed guard status and evidence."""
    print(f"Audit {method} {rule.rule}", file=sys.stderr, flush=True)
    counters = Counter()
    def profile(frame, event, _arg):
        if event == "call" and frame.f_code.co_name in GUARDS:
            counters[GUARDS[frame.f_code.co_name]] += 1
    before = snapshot(root)
    from work_buddy.dashboard.read_only import READ_ONLY_EXCEPTIONS
    request_body = {"event_type": "audit.preview", "payload": {}} if rule.endpoint == "internal_bus" else {}
    preview_subscription = None
    preview_event_received = False
    if rule.endpoint == "internal_bus":
        from work_buddy.dashboard.events import get_bus
        preview_subscription = get_bus().subscribe(timeout=0)
        next(preview_subscription)
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        with app.test_client() as client:
            response = client.open(route_url(rule, values), method=method, json=request_body, buffered=False)
            status_code = response.status_code
            payload = response.get_json(silent=True) if response.mimetype != "text/event-stream" else None
            response.close()
            if preview_subscription is not None:
                event = next(preview_subscription)
                preview_event_received = bool(event and event["event_type"] == "audit.preview" and event["payload"] == {})
    finally:
        sys.setprofile(previous)
        if preview_subscription is not None:
            preview_subscription.close()
    root_changes = changes(before, snapshot(root))
    if rule.endpoint in READ_ONLY_EXCEPTIONS and status_code == 200 and not root_changes and preview_event_received:
        status = "pure_preview"
    elif status_code == 403 and not root_changes and counters["gesture"]:
        status = "gesture_gated"
    elif status_code == 403 and not root_changes and (counters["read_only"] or counters["chokepoint"]):
        status = "read_only_gated"
    else:
        status = "unclassified"
    return {
        "name": rule.endpoint,
        "rule": rule.rule,
        "method": method,
        "status": status,
        "http_status": status_code,
        "guards": dict(counters),
        "root_changes": root_changes,
        "response": payload,
        "preview_event_received": preview_event_received,
    }


@contextmanager
def isolated_app():
    """Import the real app only after data and configuration are redirected."""
    if "work_buddy.dashboard.service" in sys.modules:
        raise RuntimeError("The audit must run in a fresh process before importing the dashboard")
    with tempfile.TemporaryDirectory(prefix="wb-dashboard-audit-") as directory:
        root = Path(directory)
        data = root / "data"
        config = root / "config"
        data.mkdir()
        config.mkdir()
        (root / ".wb-live-harness").write_text("dashboard route audit\n", encoding="utf-8")
        (config / "config.yaml").write_text(json.dumps({
            "vault_root": str(root / "vault"), "repos_root": str(root / "repos"),
            "dashboard": {"read_only": True}, "transcripts": {"enabled": []},
            "paths": {"data_root": str(data)},
        }), encoding="utf-8")
        environment = {
            "WORK_BUDDY_DATA_DIR": str(data), "WORK_BUDDY_CONFIG_DIR": str(config),
            "WORK_BUDDY_SESSION_ID": "dashboard-audit", "WB_LIVE_ROOT": str(root),
            "USERPROFILE": str(root / "home"), "HOME": str(root / "home"),
            "CODEX_HOME": str(root / "home" / ".codex"),
            "CLAUDE_CONFIG_DIR": str(root / "home" / ".claude"),
        }
        def refuse_connect(*_args, **_kwargs):
            raise OSError("Outbound connections are disabled in the disposable route audit")
        class AuditPopen(subprocess.Popen):
            def __init__(self, *_args, **_kwargs):
                refuse_connect()
        with patch.dict(os.environ, environment), patch.object(socket.socket, "connect", refuse_connect), patch.object(subprocess, "Popen", AuditPopen):
            logging.disable(logging.CRITICAL)
            from work_buddy.dashboard.service import app
            try:
                yield app, root
            finally:
                logging.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the runtime dashboard mutation guards.")
    parser.add_argument("--json", action="store_true", help="Emit JSON records.")
    parser.add_argument("--name", help="Audit one endpoint or rule.")
    parser.add_argument("--route-guards-only", action="store_true", help="Measure route-level defense without the central guard.")
    args = parser.parse_args(argv)
    with isolated_app() as (app, root):
        from work_buddy.dashboard.read_only import validate_exception_registry
        validate_exception_registry(app)
        if args.route_guards_only:
            app.before_request_funcs[None] = [hook for hook in app.before_request_funcs.get(None, []) if hook.__name__ != "guard_dashboard_request"]
        records = [
            audit_rule(app, rule, method, root=root)
            for rule in sorted(app.url_map.iter_rules(), key=lambda item: (item.rule, item.endpoint))
            if not args.name or args.name in {rule.endpoint, rule.rule}
            for method in sorted(set(rule.methods or ()) - SAFE_METHODS)
        ]
    if args.json:
        print(json.dumps(records, indent=2))
    else:
        by_status = defaultdict(list)
        for record in records:
            by_status[record["status"]].append(record)
        print("Dashboard mutation guard audit")
        for status in ("gesture_gated", "read_only_gated", "pure_preview", "unclassified"):
            print(f"\n{status}: {len(by_status[status])}")
            for record in by_status[status]:
                print(f"  {record['method']} {record['rule']} [{record['http_status']}]")
        print(f"\nTotal: {len(records)} route methods")
    return int(any(record["status"] == "unclassified" or record["root_changes"] for record in records))


if __name__ == "__main__":
    sys.exit(main())
