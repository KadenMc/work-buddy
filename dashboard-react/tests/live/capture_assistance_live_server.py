"""Disposable real-dashboard host for capture/proposal/assistance browser tests.

Run from the repository root with ``uv run python
dashboard-react/tests/live/capture_assistance_live_server.py --port 5187``.
The first JSON line identifies the temp root and nonce-gated control endpoints.
Point Vite's WB_DASHBOARD_PROXY_TARGET at the printed backend URL. Neither model
network calls nor the scheduler are started; all mutable stores and job files
live under a newly created temporary directory. Pass --react-dist with an
existing compiled directory for same-origin smoke without touching the normal
dashboard build. Ctrl+C stops this process.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", type=int, default=5187)
parser.add_argument("--smart", action="store_true", help="Enable Smart in the isolated fixture config")
parser.add_argument("--assistance", action="store_true", help="Enable form assistance in the isolated fixture config")
parser.add_argument("--react-dist", type=Path, help="Read-only compiled React dashboard directory")
args = parser.parse_args()
if args.port == 5127 or not 1024 <= args.port <= 65535:
    raise RuntimeError("Use a non-production unprivileged backend port, never 5127")
react_dist = args.react_dist.resolve() if args.react_dist is not None else None
if react_dist is not None and not react_dist.is_dir():
    raise RuntimeError("--react-dist must name an existing compiled dashboard directory")

root = Path(tempfile.mkdtemp(prefix="work-buddy-capture-assistance-live-")).resolve()
(root / ".capture-assistance-live-harness").write_text("capture-assistance-live/v1\n", encoding="utf-8")
data_root, config_root, vault_root = root / "data", root / "config", root / "vault"
for fixture_path in (data_root, config_root, vault_root / "journal"):
    fixture_path.mkdir(parents=True)
repo_root = Path(__file__).resolve().parents[3]
nonce = secrets.token_urlsafe(24)
os.environ.update({
    "WORK_BUDDY_CONFIG_DIR": str(config_root),
    "WORK_BUDDY_DATA_DIR": str(data_root),
    "WORK_BUDDY_ASSET_ROOT": str(repo_root),
    "WORK_BUDDY_SESSION_ID": "capture-assistance-live-fixture",
})
(config_root / "config.yaml").write_text(json.dumps({
    "vault_root": str(vault_root), "timezone": "America/New_York",
    "paths": {"data_root": str(data_root)},
    "dashboard": {"read_only": False, "cowork_allowed_roots": [str(vault_root)],
                  "assistance": {"enabled": args.assistance, "tier": "frontier_fast"}},
    "journal": {"smart_processing": {"enabled": args.smart, "tier": "frontier_fast"}},
    "sidecar": {"dashboard": {"port": args.port}},
}), encoding="utf-8")


def _deny_outbound(_socket, _address):
    raise RuntimeError("Outbound network is disabled in the capture/assistance live fixture")


# Flask still binds/accepts local requests. No dependency in this process can
# connect to a real sidecar, Obsidian bridge, model provider, or external host.
socket.socket.connect = _deny_outbound
socket.socket.connect_ex = _deny_outbound

from flask import jsonify, request

from work_buddy.dashboard import jobs_authoring_api
from work_buddy.journal_capture import smart as journal_smart
from work_buddy.journal_capture.projection import current_day
from work_buddy.llm.response import LLMResponse
from work_buddy.llm.runner_v2 import LLMRunner
from work_buddy.obsidian import vault_writer
from work_buddy.settings import get_journal_day_binding
from work_buddy.sidecar.scheduler.jobs import create_user_job_file
from work_buddy.tasks import events as task_events
from work_buddy.tasks import runtime as task_runtime
from work_buddy.tasks.store import TaskStore, default_task_db_path

state_lock = threading.RLock()
state = {"journal_provider_failed": False, "assistance_provider_failed": False, "pause_assistance": False,
         "journal_calls": 0, "task_assistance_calls": 0, "job_assistance_calls": 0}


def _fixture_write(_relative, path, content, **_kwargs):
    target = Path(path).resolve()
    if vault_root not in target.parents:
        raise RuntimeError("fixture writer refused a path outside the temporary vault")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    return True


vault_writer.vault_write = _fixture_write
task_events.publish_pending_async = lambda *_args, **_kwargs: None


def _journal_spec(cls, tier):
    with state_lock:
        if state["journal_provider_failed"]:
            raise journal_smart.JournalSmartProcessingError("fixture_provider_unavailable")
    return cls(tier=tier, provider_id="fixture", model_id="deterministic-test")


journal_smart.JournalSmartProcessorSpec.from_tier = classmethod(_journal_spec)


def _fixture_model(_self, **kwargs):
    """Journal-only inference fixture; form agents use real scoped tools."""

    if kwargs.get("tools"):
        raise AssertionError("The fixture model must not receive tools")
    properties = (kwargs.get("output_schema") or {}).get("properties", {})
    if "follow_up" in properties:
        with state_lock:
            state["journal_calls"] += 1
        title = kwargs["user"].strip().split("\n", 1)[0][:500] or "Review this Journal intention"
        output = {"target": "running_notes", "summary": "Saved intention with a task proposal for review.",
                  "effects": [], "follow_up": {"kind": "task_proposal", "task_text": title,
                                               "rationale": "A concrete intention retained from this exact capture."}}
    else:
        raise AssertionError("Assisted forms must use the interactive driver, not one-shot inference")
    return LLMResponse(structured_output=output, model="deterministic-test", backend="fixture")


LLMRunner.call = _fixture_model

from assistance_driver_fixture import install_assistance_fixture

install_assistance_fixture(state, state_lock)


def _fixture_create_job(payload):
    # This is the same validated writer used by user_job_create, but there is
    # no scheduler in this host and even the isolated job is explicitly disabled.
    values = {**dict(payload), "enabled": False, "overwrite": False}
    return create_user_job_file(data_root / "user_jobs", **values)


_jobs_blueprint = jobs_authoring_api.create_jobs_authoring_blueprint
jobs_authoring_api.create_jobs_authoring_blueprint = lambda **kwargs: _jobs_blueprint(create_job=_fixture_create_job, **kwargs)

task_path = default_task_db_path().resolve()
if data_root not in task_path.parents:
    raise RuntimeError("TaskStore escaped the temporary data directory")
tasks = TaskStore(task_path)
tasks.initialize()
old_state = tasks.system_state()
now = datetime.now(UTC).isoformat()
task_runtime.arm_native_authority_latch(task_path, cohort_id="capture-live",
    target_authority_epoch="native:capture-live", cutover_receipt_id="capture-live-cutover", armed_at=now)
tasks.set_system_state(expected_authority_epoch=old_state.authority_epoch,
    authority_epoch="native:capture-live", updated_at=now,
    cutover_receipt_id="capture-live-cutover", process_generation=1)
day = current_day()
(vault_root / "journal" / f"{day['localDate']}.md").write_bytes(
    b"# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n")

# Import routes, not service.main: no sidecar pollers, scheduler, or prewarm.
from work_buddy.dashboard import service as dashboard_service
from work_buddy.journal_capture import api as journal_api
from work_buddy.security.local_identity import (
    DEFAULT_AUDIENCE,
    LocalIdentityError,
    get_default_authority,
    normalize_loopback_origin,
)
from work_buddy.threads import store as thread_store

app = dashboard_service.app
if react_dist is not None:
    dashboard_service._react_dist_dir = lambda: react_dist


def _today_fixture():
    binding, _event = get_journal_day_binding()
    return jsonify({"status": "ok", "timezone": binding["timezone"],
        "now": {"iso": datetime.now(UTC).isoformat(), "local_hhmm": "12:00", "minutes_into_day": 720},
        "work_hours": [9, 17], "journal_day": binding, "current_contexts": [],
        "recommendations": [], "plan": [], "focused_count": 0, "calendar_event_count": 0,
        "active_contracts": [], "contract_constraints": [], "engage_count": 0, "errors": []})


# Legacy timeline data is unrelated to this test and must not inspect actual
# calendars, vaults, browser tabs, contracts, or a running sidecar.
app.view_functions["api_automation_today"] = _today_fixture


def _control_allowed():
    return request.headers.get("X-WB-Capture-Live-Control") == nonce


@app.post("/api/_capture-assistance-live/identity-bootstrap")
def _identity_bootstrap():
    if not _control_allowed():
        return jsonify({"ok": False, "error": "harness control denied"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        requested = normalize_loopback_origin(str(payload.get("origin") or ""))
        observed = normalize_loopback_origin(str(request.headers.get("Origin") or ""))
    except LocalIdentityError:
        return jsonify({"ok": False, "error": "exact loopback origin required"}), 400
    if requested != observed:
        return jsonify({"ok": False, "error": "browser origin mismatch"}), 403
    grant = get_default_authority().mint_bootstrap(origin=observed, audience=DEFAULT_AUDIENCE)
    response = jsonify({"ok": True, "token": grant.token, "origin": grant.origin, "expires_at": grant.expires_at})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/_capture-assistance-live/control")
def _control():
    if not _control_allowed():
        return jsonify({"ok": False, "error": "harness control denied"}), 403
    payload = request.get_json(silent=True)
    allowed = {"journal_provider_failed", "assistance_provider_failed", "pause_assistance", "reconcile"}
    if not isinstance(payload, dict) or set(payload) - allowed or any(not isinstance(value, bool) for value in payload.values()):
        return jsonify({"ok": False, "error": "boolean fixture flags required"}), 400
    with state_lock:
        for key in allowed - {"reconcile"}:
            if key in payload:
                state[key] = payload[key]
    if payload.get("reconcile"):
        journal_api._services()[2].reconcile_proposals()
    return jsonify({"ok": True})


@app.get("/api/_capture-assistance-live/state")
def _state():
    if not _control_allowed():
        return jsonify({"ok": False, "error": "harness control denied"}), 403
    _sources, journals, _service = journal_api._services()
    with journals._connect() as conn:
        captures = conn.execute("SELECT COUNT(*) FROM journal_captures").fetchone()[0]
        notes = [dict(row) for row in conn.execute("SELECT entry_id,resolution_state,version FROM journal_entries")]
    connection = thread_store.get_connection()
    try:
        threads = connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    finally:
        connection.close()
    with state_lock:
        snapshot = dict(state)
    return jsonify({"ok": True, **snapshot, "captures": captures, "notes": notes,
                    "threads": threads, "tasks": len(tasks.list()),
                    "jobs": len(list((data_root / "user_jobs").glob("*.md")))})


@app.after_request
def _identify_fixture(response):
    response.headers["X-WB-Capture-Live-Harness"] = nonce
    return response


print(json.dumps({"fixture": "capture-assistance-live/v1", "root": str(root), "nonce": nonce,
                  "backendUrl": f"http://127.0.0.1:{args.port}", "defaultSmart": args.smart,
                  "defaultAssistance": args.assistance,
                  "reactDist": str(react_dist) if react_dist is not None else None}), flush=True)
app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
