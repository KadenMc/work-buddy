"""Survey every dashboard GET rule on a seeded disposable live-harness root.

The writable survey records file changes. A fresh process repeats the same
requests with SQLite and filesystem protections installed before app import.
SQLite failures, denied side effects, HTTP results, and root hashes are retained.

Run via::

    uv run python -m scripts.survey_dashboard_read_only --json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from scripts.audit_dashboard_mutations import changes, route_url, snapshot


def read_disposition(record: dict) -> str:
    """Distinguish readable fixtures from refusals and untested resources."""
    payload = record.get("error_response") or {}
    error = payload.get("error")
    code = payload.get("code") or (error.get("code") if isinstance(error, dict) else None)
    if code == "assistance_requires_writer":
        return "unavailable: polling reconciles persisted sessions and agent leases"
    if code == "journal_not_initialized":
        return "untested: Journal authority is not seeded by the Co-work fixture"
    if any("disabled in the read-only" in failure["error"] or (failure["sqlite_code"] or "").startswith("SQLITE_READONLY") for failure in record["failures"]):
        return "unresolved: a read attempted a forbidden operation"
    if record["http_status"] >= 500:
        return "unresolved: inspect the recorded error response and traceback"
    if record["coverage"] == "missing_resource":
        return "untested: missing-resource probe does not exercise an existing object"
    if record["failures"]:
        return "limited: inspect the recorded unavailable storage or refused operation"
    if record["http_status"] >= 400:
        return "limited: request validation or an unavailable association refused this read"
    return "passed: fixture read completed without a recorded storage failure"


def environment(root: Path) -> dict[str, str]:
    return {
        **os.environ,
        "WORK_BUDDY_DATA_DIR": str(root / "data"),
        "WORK_BUDDY_CONFIG_DIR": str(root / "config"),
        "WORK_BUDDY_SESSION_ID": "dashboard-survey",
        "WB_LIVE_ROOT": str(root), "WB_LIVE_HOST_ROOT": str(root / "host"),
        "WB_LIVE_FIXTURE_FILE": str(root / "fixture.json"),
        "USERPROFILE": str(root / "home"), "HOME": str(root / "home"),
        "CODEX_HOME": str(root / "home" / ".codex"),
        "CLAUDE_CONFIG_DIR": str(root / "home" / ".claude"),
    }


def authoritative_state(root: Path) -> dict:
    """Hash schema and every row through a coherent SQLite read transaction.

    A WAL can hold an UPDATE without changing the main file or row counts.
    Logical hashes include that content while keeping fixture values out of
    the report. Sort encoded rows to make query-plan ordering immaterial.
    """
    result = {}
    for path in sorted(root.rglob("*.db")):
        if not path.is_file():
            continue
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            schema = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
            tables = {}
            for name in sorted(row[1] for row in schema if row[0] == "table"):
                rows = connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
                encoded = sorted(json.dumps([
                    {"blob": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value
                    for value in row
                ], ensure_ascii=True, separators=(",", ":")) for row in rows)
                tables[name] = {"rows": len(rows), "sha256": hashlib.sha256("\n".join(encoded).encode()).hexdigest()}
            result[path.relative_to(root).as_posix()] = {
                "schema_sha256": hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode()).hexdigest(),
                "tables": tables,
            }
        finally:
            connection.close()
    return result


def seed_survey(root: Path) -> None:
    """Build real domain objects before either survey process starts."""
    os.environ.update(environment(root))
    logging.disable(logging.CRITICAL)
    from work_buddy.cowork import bootstrap
    from work_buddy.document_kernel.runtime_service import shared_document_kernel
    from work_buddy.truth.registry import TruthStoreRegistry
    from work_buddy.truth.contracts import Actor
    from work_buddy.truth.identity import sha256_bytes
    fixture_path = root / "fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    store = TruthStoreRegistry().open_store(fixture["initialized"]["store_id"])
    from work_buddy.document_kernel.causality import DocumentCausalityStore
    DocumentCausalityStore(store.paths.sidecar)
    source = b"# Throwaway survey document\n\nA fixture sentence.\n"
    kernel = shared_document_kernel()
    built = kernel.request({
        "kind": "bootstrap_markdown", "sourceBase64": source,
        "sourceSha256": sha256_bytes(source), "newlineStyle": "lf",
        "utf8Bom": False, "trailingNewlineCount": 1,
    }, request_id="dashboard-survey-document")
    actor = Actor("human", "dashboard-survey-user")
    intent, _ = bootstrap.prepare_bootstrap(store, metadata={
        "mode": "create", "path": "docs/survey.md", "title": "Throwaway survey document",
        "initial_source_sha256": sha256_bytes(source), "idempotency_key": "dashboard-survey-document",
    }, source=source, actor=actor)
    receipt = bootstrap.commit_bootstrap(store, bootstrap_id=intent.id, snapshot=built.snapshot,
        source_sha256=intent.source_sha256, snapshot_sha256=sha256_bytes(built.snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA, actor=actor)
    from work_buddy.projects.store import upsert_project
    from work_buddy.threads.models import Thread
    from work_buddy.threads.store import insert_thread
    from work_buddy.conversations.store import create_conversation
    from work_buddy.entities.store import create_entity
    from work_buddy.settings.broker import get_values
    from work_buddy.tasks.store import TaskStore
    from work_buddy.tasks.service import TaskApplicationService
    from work_buddy.dashboard.service import app
    from work_buddy.security.local_identity import get_default_authority, SESSION_COOKIE_NAME
    upsert_project("dashboard-survey", "Throwaway survey project")
    thread = insert_thread(Thread(thread_id="th-survey", inciting_event_summary={"title": "Throwaway survey thread"}))
    conversation = create_conversation("Throwaway survey conversation", source="dashboard-survey")
    entity = create_entity("Throwaway survey entity", author="user")
    task_store = TaskStore()
    task_store.initialize()
    from datetime import datetime, timezone
    from work_buddy.tasks import runtime
    observed_at = datetime.now(timezone.utc).isoformat()
    runtime.arm_native_authority_latch(task_store.path, cohort_id="dashboard-survey",
        target_authority_epoch="native:dashboard-survey", cutover_receipt_id="dashboard-survey-cutover", armed_at=observed_at)
    task_store.set_system_state(expected_authority_epoch=task_store.system_state().authority_epoch,
        authority_epoch="native:dashboard-survey", updated_at=observed_at,
        cutover_receipt_id="dashboard-survey-cutover", process_generation=1)
    TaskApplicationService(task_store).create(description="Throwaway survey task", client_mutation_id="survey-create-task", actor="user", task_id="t-survey")
    get_values()
    from work_buddy.conversation_observability.db import get_connection as observability_connection
    from work_buddy.summarization.db import get_connection as summarization_connection
    observability_connection().close()
    summarization_connection().close()
    from work_buddy.summarization.queue import ensure_queue_table
    ensure_queue_table()
    from work_buddy.inference.metrics_store import get_connection as metrics_connection
    metrics_connection().close()
    grant = get_default_authority().mint_bootstrap(origin="http://localhost")
    with app.test_client() as client:
        response = client.post("/api/local-identity/bootstrap/redeem", json={"token": grant.token}, headers={"Origin": "http://localhost"})
        if response.status_code != 200:
            raise RuntimeError(f"Survey identity redemption failed: {response.status_code}")
        cookie = client.get_cookie(SESSION_COOKIE_NAME).value
    fixture["survey"] = {
        "document_id": receipt["document_id"], "thread_id": thread.thread_id,
        "conversation_id": conversation.conversation_id, "task_id": "t-survey",
        "entity_id": entity["id"], "slug": "dashboard-survey", "view_name": "cowork",
        "session_cookie": cookie,
    }
    fixture_path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")


def survey_worker(root: Path, *, readonly: bool) -> dict:
    if not (root / ".wb-live-harness").is_file():
        raise RuntimeError("A marked disposable live-harness root is required")
    os.environ.update(environment(root))
    logging.disable(logging.CRITICAL)
    startup_before = snapshot(root)
    state_before = authoritative_state(root)
    all_connections = []
    def observe_connection(frame, event, result):
        if event == "return" and frame.f_code.co_name == "_readonly_connect" and isinstance(result, sqlite3.Connection):
            all_connections.append(result.execute("PRAGMA query_only").fetchone()[0])
    previous_profile = sys.getprofile()
    previous_thread_profile = threading.getprofile()
    sys.setprofile(observe_connection)
    threading.setprofile(observe_connection)
    if readonly:
        from work_buddy.dashboard.read_only import install_process_read_only
        install_process_read_only()
    from work_buddy.dashboard.service import app
    startup_changes = changes(startup_before, snapshot(root))
    fixture = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
    values = {"store_id": fixture["initialized"]["store_id"], "local_date": "2000-01-01", **fixture["survey"]}
    records = []
    before_all = snapshot(root)
    for rule in sorted(app.url_map.iter_rules(), key=lambda item: (item.rule, item.endpoint)):
        if "GET" not in rule.methods:
            continue
        print(f"Survey {'read-only' if readonly else 'writable'} GET {rule.rule}", file=sys.stderr, flush=True)
        failures = set()
        connections = []
        def trace(frame, event, arg):
            if event == "return" and frame.f_code.co_name == "_readonly_connect" and isinstance(arg, sqlite3.Connection):
                connections.append(arg.execute("PRAGMA query_only").fetchone()[0])
            if event == "exception":
                _kind, exception, _tb = arg
                if isinstance(exception, (sqlite3.Error, PermissionError, TypeError, AttributeError)):
                    code = getattr(exception, "sqlite_errorname", None)
                    failures.add((frame.f_code.co_filename.replace("\\", "/").split("work_buddy/")[-1], frame.f_code.co_name, str(exception), code))
            return trace if "/work_buddy/" in frame.f_code.co_filename.replace("\\", "/") else None
        before = snapshot(root)
        previous = sys.gettrace()
        sys.settrace(trace)
        try:
            with app.test_client() as client:
                from work_buddy.security.local_identity import SESSION_COOKIE_NAME
                client.set_cookie(SESSION_COOKIE_NAME, values["session_cookie"])
                response = client.get(route_url(rule, values), query_string={"store_id": values["store_id"]}, buffered=False)
                if response.mimetype != "text/event-stream":
                    response.get_data()
                status = response.status_code
                error_response = response.get_json(silent=True) if status >= 400 and response.is_json else None
                if status >= 500:
                    print(f"Survey response {status} {rule.rule}: {error_response}", file=sys.stderr, flush=True)
                response.close()
        finally:
            sys.settrace(previous)
        records.append({
            "rule": rule.rule, "name": rule.endpoint, "http_status": status,
            "url": route_url(rule, values),
            "error_response": error_response,
            "parameters": {
                name: values[name] if name in values else 1 if type(rule._converters[name]).__name__ in {"IntegerConverter", "FloatConverter"} else "00000000-0000-4000-8000-000000000001" if type(rule._converters[name]).__name__ == "UUIDConverter" else "audit-missing"
                for name in sorted(rule.arguments)
            },
            "coverage": "missing_resource" if rule.arguments - values.keys() else "existing_fixture" if rule.arguments else "static_route",
            "query_only": connections,
            "failures": [dict(zip(("file", "function", "error", "sqlite_code"), failure)) for failure in sorted(failures, key=str) if failure[3] is not None or "disabled in the read-only" in failure[2] or status >= 500],
            "root_changes": changes(before, snapshot(root)),
        })
        records[-1]["disposition"] = read_disposition(records[-1])
    state_after = authoritative_state(root)
    after_all = snapshot(root)
    root_changes = changes(before_all, after_all)
    sys.setprofile(previous_profile)
    threading.setprofile(previous_thread_profile)
    return {
        "mode": "read_only" if readonly else "writable", "records": records,
        "root_changes": root_changes,
        "startup_changes": startup_changes,
        "authority_changes": [path for path in sorted(set(root_changes + startup_changes)) if not path.endswith(("-wal", "-shm"))],
        "sqlite_wal_changes": [path for path in sorted(set(root_changes + startup_changes)) if path.endswith("-wal")],
        "sqlite_coordination_changes": [path for path in sorted(set(root_changes + startup_changes)) if path.endswith("-shm")],
        "logical_state_before": state_before, "logical_state_after": state_after,
        "all_query_only": all_connections,
        "general_readiness": all(record["disposition"].startswith("passed:") for record in records),
        "unreadable_authority_paths": sorted({path for state in (startup_before, before_all, after_all) for path, digest in state.items() if digest.startswith("unreadable:") and not path.endswith(("-wal", "-shm"))}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Survey dashboard GET behavior on a disposable root.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--read-only", action="store_true", help="Survey only the protected process.")
    parser.add_argument("--seed-worker-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.seed_worker_root:
        with patch.object(socket.socket, "connect", side_effect=OSError("Survey seed has no external providers")):
            seed_survey(args.seed_worker_root.resolve())
        return 0
    if args.worker_root:
        def refuse_connect(*_args, **_kwargs):
            raise OSError("External providers and subprocesses are disabled in the disposable survey")
        class SurveyPopen(subprocess.Popen):
            def __init__(self, command, *values, **kwargs):
                from work_buddy.dashboard.read_only import _is_document_kernel
                if _is_document_kernel(command):
                    super().__init__(command, *values, **kwargs)
                else:
                    refuse_connect()
        with patch.object(socket.socket, "connect", refuse_connect), patch.object(subprocess, "Popen", SurveyPopen):
            result = survey_worker(args.worker_root.resolve(), readonly=args.read_only)
        print(json.dumps(result, indent=2))
        return 0
    reports = []
    for readonly in ((True,) if args.read_only else (False, True)):
      with tempfile.TemporaryDirectory(prefix="wb-dashboard-survey-") as directory:
        root = Path(directory)
        for child in ("data", "config", "host", "home", "repos"):
            (root / child).mkdir()
        (root / ".wb-live-harness").write_text("dashboard GET survey\n", encoding="utf-8")
        (root / "config" / "config.yaml").write_text(json.dumps({
            "vault_root": str(root / "host"), "repos_root": str(root / "repos"),
            "paths": {"data_root": str(root / "data")},
            "dashboard": {"read_only": False}, "transcripts": {"enabled": []},
        }), encoding="utf-8")
        env = environment(root)
        subprocess.run([sys.executable, "dashboard-react/tests/live/seeds/cowork.py"], env=env, capture_output=True, text=True, check=True)
        subprocess.run([sys.executable, "-m", "scripts.survey_dashboard_read_only", "--seed-worker-root", str(root)], env=env, check=True)
        command = [sys.executable, "-m", "scripts.survey_dashboard_read_only", "--worker-root", str(root)]
        if readonly:
            command.append("--read-only")
        completed = subprocess.run(command, env=env, stdout=subprocess.PIPE, text=True, timeout=600)
        if completed.returncode:
            return completed.returncode
        reports.append(json.loads(completed.stdout))
    result = {"environment": "independently seeded disposable live-harness roots", "external_providers": "outbound connections and subprocesses disabled except bundled document kernel", "reports": reports}
    print(json.dumps(result, indent=2) if args.json else json.dumps({report["mode"]: {"routes": len(report["records"]), "changed_paths": report["root_changes"]} for report in reports}, indent=2))
    readonly_report = next(report for report in reports if report["mode"] == "read_only")
    return int(bool(readonly_report["authority_changes"])
        or bool(readonly_report["unreadable_authority_paths"])
        or any(record["disposition"].startswith("unresolved:") for record in readonly_report["records"])
        or readonly_report["logical_state_before"] != readonly_report["logical_state_after"]
        or any(failure.get("sqlite_code", "").startswith("SQLITE_READONLY") for record in readonly_report["records"] for failure in record["failures"] if failure.get("sqlite_code"))
        or any(value != 1 for value in readonly_report["all_query_only"]))


if __name__ == "__main__":
    sys.exit(main())
