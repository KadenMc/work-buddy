"""Dashboard HTTP service.

Serves:
    - ``GET /health`` — sidecar health check
    - ``GET /`` — single-page dashboard app
    - ``GET /api/state`` — aggregated system state
    - ``GET /api/tasks`` — task list from Obsidian Tasks
    - ``GET /api/sessions`` — active agent sessions
    - ``GET /api/services`` — child service health
    - ``GET /api/contracts`` — active contract summaries

Run with:  python -m work_buddy.dashboard
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

from work_buddy.config import load_config
from work_buddy.dashboard.api import (
    get_chats_summary,
    get_contracts_summary,
    get_embeddings_summary,
    get_fleet_summary,
    get_inference_activity,
    get_palette_commands,
    get_sessions_summary,
    get_system_state,
    get_tasks_summary,
)
from work_buddy.dashboard import views as workflow_views
from work_buddy.dashboard.frontend import render_page

logger = logging.getLogger(__name__)

app = Flask(__name__)

_cfg = load_config()


def _is_read_only() -> bool:
    """Check if the dashboard is in read-only mode (no mutating actions)."""
    return _cfg.get("dashboard", {}).get("read_only", False)


def _reject_read_only():
    """Return a 403 response if read-only mode is active, else None."""
    if _is_read_only():
        return jsonify({"error": "Dashboard is in read-only mode"}), 403
    return None


# ---------------------------------------------------------------------------
# Hybrid task search helper
# ---------------------------------------------------------------------------

_embed_available: bool | None = None
_embed_check_time: float = 0.0
_EMBED_CHECK_TTL = 60.0  # re-check availability every 60s


def _is_embed_available() -> bool:
    """Cached check for embedding service availability."""
    global _embed_available, _embed_check_time
    now = time.time()
    if _embed_available is not None and (now - _embed_check_time) < _EMBED_CHECK_TTL:
        return _embed_available
    try:
        from work_buddy.embedding.client import is_available
        _embed_available = is_available()
    except ImportError:
        _embed_available = False
    _embed_check_time = now
    return _embed_available


def _hybrid_task_search(
    query: str,
    tasks: list[dict],
    limit: int,
) -> list[dict] | None:
    """Score tasks using BM25 + semantic search via the embedding service.

    Returns scored task list (descending), or None if the embedding service
    is unavailable (caller should fall back to substring).
    """
    if not _is_embed_available():
        return None

    try:
        from work_buddy.embedding.client import hybrid_search
    except ImportError:
        return None

    # Build candidates: each task becomes {name: task_id, texts: [task_text]}
    candidates = []
    task_by_name: dict[str, dict] = {}
    for t in tasks:
        text = (t.get("text") or "").strip()
        tid = t.get("id") or text
        if not text:
            continue
        name = tid
        candidates.append({"name": name, "texts": [text]})
        task_by_name[name] = t

    if not candidates:
        return None

    results = hybrid_search(
        query,
        candidates,
        bm25_weight=0.3,
        embed_weight=0.7,
    )

    scored_tasks = []
    for r in results[:limit]:
        task = task_by_name.get(r["name"])
        if task is None:
            continue
        scored_task = dict(task)
        scored_task["score"] = round(r.get("score", 0.0), 4)
        scored_tasks.append(scored_task)

    return scored_tasks


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------

def _format_sse_event(event: dict) -> bytes:
    """Render an event-bus event as a single ``data:`` SSE frame."""
    return f"data: {json.dumps(event)}\n\n".encode("utf-8")


def _sse_stream(bus, idle_timeout: float = 15.0):
    """Yield SSE-framed bytes from the event bus.

    Idle ticks emit an SSE comment line (``: keepalive``) so that
    intermediaries (Tailscale Serve, mobile networks, browsers' idle
    detection) don't close the connection while the bus has nothing to
    say. Real events are emitted as ``data: <json>\\n\\n`` frames.

    Extracted from the route so it can be unit-tested without spinning up
    Flask's threaded test server.
    """
    # First-flush comment: forces some proxies to release headers and
    # confirms to the client (via EventSource readyState=OPEN) that the
    # connection is alive before the first real event arrives.
    yield b": connected\n\n"
    for event in bus.subscribe(timeout=idle_timeout):
        if event is None:
            yield b": keepalive\n\n"
            continue
        yield _format_sse_event(event)


@app.get("/api/events")
def api_events():
    """Server-Sent Events stream of dashboard events.

    Single connection per browser tab. Subscribers receive every event
    published to the in-process bus — both same-process publishers and
    cross-process publishers that POST to ``/internal/bus``. The browser's
    native ``EventSource`` reconnects automatically on disconnect; the bus
    does not replay events from before the reconnect.

    No read-only gate: this is a pure read endpoint.
    """
    from work_buddy.dashboard.events import get_bus

    bus = get_bus()
    resp = Response(_sse_stream(bus), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # nginx-friendly; harmless on Tailscale
    resp.headers["Connection"] = "keep-alive"
    return resp


@app.post("/internal/bus")
def internal_bus():
    """Localhost-only ingress for cross-process event-bus publishers.

    The sidecar process cannot reach this Flask process's in-process bus
    directly, so ``events.publish_cross_process`` POSTs ``{event_type,
    payload}`` here and this handler re-publishes it on the shared bus the
    SSE endpoint streams from. Best-effort and fire-and-forget: nothing is
    persisted, and an event that arrives while no browser is subscribed is
    simply dropped.

    Gated to loopback callers. The dashboard has no auth and can be bound to
    0.0.0.0 / published over Tailscale, so a remote caller must never be able
    to inject bus events. Exempt from the read-only gate — UI-refresh events
    must keep flowing even when the dashboard is display-only.
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "loopback only"}), 403

    data = request.get_json(silent=True) or {}
    event_type = data.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        return jsonify({"error": "event_type required"}), 400

    from work_buddy.dashboard.events import get_bus
    get_bus().publish(event_type, data.get("payload"))
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    # Pre-warm the embeddings snapshot on a background thread so the first
    # Settings › Embeddings open serves instantly instead of paying the cold aggregate.
    try:
        from work_buddy.dashboard.api import (
            _kick_embeddings_refresh,
            _kick_fleet_refresh,
        )
        _kick_embeddings_refresh()
        _kick_fleet_refresh()
    except Exception:
        pass
    resp = Response(render_page(), content_type="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/favicon.svg")
def favicon():
    from work_buddy import paths
    logo = paths.asset_root() / "docs" / "logo.svg"
    if logo.exists():
        return send_file(logo, mimetype="image/svg+xml")
    return "", 404


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    """Aggregated system state — services, jobs, uptime."""
    return jsonify(get_system_state())


@app.get("/api/registry/list")
def api_registry_list():
    """Return registered capabilities and workflows for the Add-job picker.

    Powers a ``<datalist>`` autocomplete so the user picks a real name
    instead of typing one from memory. Each entry carries a short
    description used as the dropdown's secondary text.
    """
    from work_buddy.mcp_server.registry import (
        Capability, WorkflowDefinition, get_registry,
    )

    reg = get_registry()

    capabilities = []
    workflows = []
    for name, entry in sorted(reg.items()):
        desc = (getattr(entry, "description", "") or "").split("\n", 1)[0]
        # ``slash_command`` is the user-facing name many users actually
        # remember (e.g. ``wb-morning`` for the ``morning-routine``
        # workflow). Surface it so the typeahead can match either name.
        slash = getattr(entry, "slash_command", None) or ""
        if isinstance(entry, WorkflowDefinition):
            workflows.append({
                "name": name,
                "description": desc,
                "parameters": _project_param_schema(entry.params_schema),
                "slash_command": slash,
            })
        elif isinstance(entry, Capability):
            capabilities.append({
                "name": name,
                "description": desc,
                "parameters": _project_param_schema(entry.parameters),
                "slash_command": slash,
            })
    return jsonify({"capabilities": capabilities, "workflows": workflows})


@app.get("/api/cron/describe")
def api_cron_describe():
    """Return a human-readable description of a cron expression.

    Used by the dashboard's Add-job form to give live feedback under the
    schedule input. Returns ``{"valid": false}`` when the expression can't
    be parsed so the form can stay quiet rather than showing junk.

    Also returns the typical interval between firings (``interval_seconds``)
    and a suggested jitter ceiling (``max_jitter_seconds``) so the form's
    Jitter input can clamp itself to a sensible range for the schedule.
    """
    expr = (request.args.get("expr") or "").strip()
    if not expr:
        return jsonify({
            "valid": False, "description": "",
            "interval_seconds": None, "max_jitter_seconds": 0,
        })
    from work_buddy.dashboard.api import _describe_cron
    from work_buddy.sidecar.scheduler.cron import (
        compute_max_jitter_seconds, cron_interval_seconds,
    )

    desc = _describe_cron(expr)
    # _describe_cron returns the raw expression on parse failure; treat
    # that as "couldn't humanize" so the UI suppresses the preview line.
    valid = desc != expr
    interval = cron_interval_seconds(expr) if valid else None
    return jsonify({
        "valid": valid,
        "description": desc,
        "interval_seconds": interval,
        "max_jitter_seconds": compute_max_jitter_seconds(interval),
    })


@app.post("/api/user_jobs")
def api_user_job_create():
    """Create a user-authored scheduled job from the dashboard form.

    Thin wrapper around the user_job_create capability so the form does
    not need to know capability internals. Read-only mode blocks writes.
    """
    blocked = _reject_read_only()
    if blocked is not None:
        return blocked

    payload = request.get_json(silent=True) or {}
    from work_buddy.mcp_server.registry import get_registry

    reg = get_registry()
    cap = reg.get("user_job_create")
    if cap is None:
        return jsonify({"success": False, "error": "user_job_create capability not registered."}), 500
    try:
        result = cap.callable(**payload)
    except TypeError as exc:
        return jsonify({"success": False, "error": f"Invalid arguments: {exc}"}), 400
    status = 200 if result.get("success") else 400
    if result.get("success"):
        # Tell the dashboard event bus immediately so the Jobs tab can show
        # the pending banner without waiting for the sidecar's hot-reload
        # to publish cron.hot_reload (~30s later).
        from work_buddy.dashboard.events import publish_auto
        publish_auto("user_job.created", {
            "name": result.get("name"),
            "file_path": result.get("file_path"),
        })
    return jsonify(result), status


@app.get("/api/user_jobs/<name>")
def api_user_job_get(name: str):
    """Return the parsed frontmatter + body of a single user job.

    Used by the dashboard's Edit-job flow to populate the form. Returns
    the same field shape ``user_job_create`` accepts as input, so the
    frontend can hand the dict back through ``submitAddJobForm`` with
    ``overwrite: true`` to save edits.
    """
    from work_buddy.paths import data_dir
    if not _user_job_name_safe(name):
        return jsonify({"ok": False, "error": "invalid job name"}), 400
    target = data_dir("user_jobs") / f"{name}.md"
    if not target.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        from work_buddy.sidecar.scheduler.jobs import _parse_job_file
        job = _parse_job_file(target, source="user")
    except Exception as exc:
        logger.exception("user_job parse failed for %s", name)
        return jsonify({"ok": False, "error": f"parse failed: {exc}"}), 500
    if job is None:
        return jsonify({"ok": False, "error": "file is not a valid job (no schedule)"}), 400
    return jsonify({
        "ok": True,
        "name": job.name,
        "schedule": job.schedule,
        "job_type": job.job_type,
        "capability": job.capability,
        "workflow": job.workflow,
        "params": job.params,
        "prompt": job.prompt,
        "enabled": job.enabled,
        "recurring": job.recurring,
        "jitter_seconds": job.jitter_seconds,
    })


@app.delete("/api/user_jobs/<name>")
def api_user_job_delete(name: str):
    """Delete a user-authored scheduled job by removing its .md file.

    The sidecar's filesystem watcher picks up the deletion within ~50ms
    and drops the job from the schedule. We also publish a
    ``user_job.deleted`` event so the dashboard's Jobs table refreshes
    immediately rather than waiting for the next ``cron.hot_reload``.
    """
    blocked = _reject_read_only()
    if blocked is not None:
        return blocked
    from work_buddy.paths import data_dir
    if not _user_job_name_safe(name):
        return jsonify({"ok": False, "error": "invalid job name"}), 400
    target = data_dir("user_jobs") / f"{name}.md"
    if not target.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        target.unlink()
    except OSError as exc:
        logger.exception("user_job delete failed for %s", name)
        return jsonify({"ok": False, "error": f"delete failed: {exc}"}), 500
    try:
        from work_buddy.dashboard.events import publish_auto
        publish_auto("user_job.deleted", {"name": name})
    except Exception:
        pass  # Best-effort — file is gone either way; watcher will reconcile.
    return jsonify({"ok": True, "name": name})


def _user_job_name_safe(name: str) -> bool:
    """Defensive name check matching the create-flow regex.

    Prevents path traversal in URL params: a stem like ``../../etc/passwd``
    would otherwise resolve outside the user_jobs/ directory.
    """
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name or ""))


@app.post("/api/user_jobs/help")
def api_user_jobs_help():
    """Open a chat-driven walkthrough for authoring a user job.

    Pairs with the dashboard's chat sidebar. Creates a conversation
    silently (calling the store layer directly so no CHAT toast or
    workflow-view tab spawns), seeds it with an opening agent message,
    then fire-and-forgets a headless Claude session bound to that
    conversation_id. The frontend opens its sidebar with the returned
    conversation_id.
    """
    blocked = _reject_read_only()
    if blocked is not None:
        return blocked

    from work_buddy.conversations.store import (
        add_message,
        close_conversation,
        create_conversation,
    )
    from work_buddy.dashboard.jobs_help import spawn_job_author_session

    conv = create_conversation(
        title="Help me create a job",
        source="dashboard:user_jobs_help",
    )
    # Seed as a *question* (response_type=freeform) — not a plain text
    # message. This binds the user's first reply as the answer to a
    # pending question, which the spawned agent retrieves via
    # conversation_poll. With a plain text seed, conversation_poll
    # returns 'no_pending_question' and the agent falls back to its
    # own greeting (the duplicate-greeting bug from the first run).
    add_message(
        conv.conversation_id,
        "agent",
        "Hi! I'll help you set up a scheduled job. What do you want it to do?",
        message_type="question",
        response_type="freeform",
    )

    result = spawn_job_author_session(conv.conversation_id)
    if result.get("status") != "ok":
        # Conversation exists but no driver — close it so it doesn't dangle.
        close_conversation(conv.conversation_id)
        return jsonify({
            "ok": False,
            "error": result.get("error", "Failed to spawn job-author agent."),
        }), 500

    # Register the driving pid so /api/conversations/<id> can report
    # whether the agent process is still alive — the chat sidebar uses
    # that signal to know when to stop the typing indicator and tell
    # the user the agent stopped (budget cap, crash, kill).
    pid = result.get("pid")
    if pid:
        from work_buddy.conversations.agents import register as register_agent
        register_agent(conv.conversation_id, pid)

    return jsonify({
        "ok": True,
        "conversation_id": conv.conversation_id,
        "title": "Help me create a job",
        "pid": result.get("pid"),
    })


@app.post("/api/dashboard/interact")
def api_dashboard_interact():
    """Drive a dashboard form on behalf of an agent.

    The MCP capability ``dashboard_interact`` is a thin HTTP forwarder
    that POSTs here. The actual logic — schema validation, event
    publishing, and (for ``form_submit`` / ``form_get_state``) the
    rendezvous wait for the frontend's postback — lives in this
    process so the rendezvous map and the receiving postback share
    process memory.

    Body: ``{"action": str, "form_id": str, "field": str?, "value": any?, "timeout_seconds": float?}``.
    Response: typed dict from ``dashboard.interact.dashboard_interact``.
    """
    blocked = _reject_read_only()
    if blocked is not None:
        return blocked
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400
    action = payload.get("action") or ""
    form_id = payload.get("form_id") or ""
    if not action or not form_id:
        return jsonify({
            "ok": False,
            "error": "action and form_id are required",
        }), 400
    try:
        from work_buddy.dashboard.interact import dashboard_interact
        result = dashboard_interact(
            action=action,
            form_id=form_id,
            field=payload.get("field", ""),
            value=payload.get("value"),
            timeout_seconds=float(payload.get("timeout_seconds", 10.0)),
        )
    except NotImplementedError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 501
    except Exception as exc:
        logger.exception("dashboard_interact failed for %s/%s", action, form_id)
        return jsonify({"ok": False, "error": str(exc)}), 500
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.post("/api/dashboard/interact/result/<request_id>")
def api_dashboard_interact_result(request_id: str):
    """Frontend → capability postback for rendezvous-backed actions.

    The ``dashboard_interact`` capability publishes ``form_submit`` /
    ``form_get_state`` events with a request_id and blocks on a queue
    keyed by it. The frontend bridge runs the registered handler, then
    POSTs the result here; this endpoint hands the payload to the
    capability via :func:`work_buddy.dashboard.interact.deliver_result`.

    Body: ``{"ok": bool, "error": str?, "errors_by_field": dict?, "fields": dict?, ...}``.
    """
    blocked = _reject_read_only()
    if blocked is not None:
        return blocked
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400
    try:
        from work_buddy.dashboard.interact import deliver_result
        delivered = deliver_result(request_id, payload)
    except Exception as exc:
        logger.exception("interact result delivery failed for %s", request_id)
        return jsonify({"ok": False, "error": str(exc)}), 500
    if not delivered:
        return jsonify({
            "ok": False,
            "error": "no pending rendezvous for this request_id (already delivered or expired)",
        }), 404
    return jsonify({"ok": True})


# DiagnosticRunner has no standalone HTTP endpoint — it is invoked
# in-process by control.help_briefs (the Settings "?" help button and
# the /api/investigate event brief).


@app.post("/api/reprobe/<component_id>")
def api_reprobe(component_id: str):
    """Re-run a single tool probe and return fresh component health."""
    try:
        from work_buddy.tools import reprobe_one
        entry = reprobe_one(component_id)
        if entry is None:
            return jsonify({"error": f"Unknown component: {component_id}"}), 404
        # Return fresh health view for this component from the engine
        from work_buddy.health.engine import HealthEngine
        engine = HealthEngine()
        comp = engine.get_component(component_id)
        return jsonify({
            "probe": entry,
            "health": comp.to_dict() if comp else None,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/control/preference")
def api_control_preference():
    """Set one or more component feature preferences from the Settings tab.

    Body: ``{"updates": {"<component_id>": {"wanted": bool|null, "reason": str?}, ...}}``

    Gated by read-only mode. Writes to config.local.yaml via
    ``apply_preference_updates`` (consent-gated at the capability level,
    but we auto-grant here — the user clicking the toggle IS the consent,
    same pattern as ``_launch_workflow_session``).

    Returns the fresh control graph so the UI can re-render without a
    separate round-trip.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked

    data = request.get_json(silent=True) or {}
    updates = data.get("updates")
    if not isinstance(updates, dict) or not updates:
        return jsonify({"error": "Request body must include non-empty 'updates' dict"}), 400

    try:
        from work_buddy.consent import grant_consent
        from work_buddy.health.preferences import apply_preference_updates
        from work_buddy.control.graph import build_graph, cache_info, invalidate_graph

        # Clicking the toggle IS the consent — mirrors the workflow-launch pattern.
        grant_consent("setup.write_preferences", mode="once")
        written = apply_preference_updates(updates)
        # apply_preference_updates calls _invalidate_control_graph internally,
        # but call again defensively in case the guarded import failed earlier.
        invalidate_graph()

        nodes = build_graph(force=True)
        return jsonify({
            "written": written,
            "nodes": {nid: n.to_dict() for nid, n in nodes.items()},
            "cache": cache_info(),
        })
    except Exception as exc:
        logger.exception("Failed to apply preference updates")
        return jsonify({"error": str(exc)}), 500


@app.post("/api/control/reprobe")
def api_control_reprobe():
    """Re-run every registered tool probe, then rebuild the control graph.

    The existing ``GET /api/control/graph?force=1`` only busts the 45-s
    graph cache — it doesn't touch ``tool_status.json``. So if the
    probes were stale (hadn't run since the last 60-s auto-refresh), a
    force-refresh would rebuild from the same stale data.

    This endpoint runs ``probe_all(force=True)`` (parallel where
    independent, serial for tool-probe ``depends_on`` chains),
    rewrites ``tool_status.json``, invalidates the graph cache, and
    returns the fresh graph. Worst-case latency is ~10 s (the Obsidian
    HTTP probe's timeout) — the UI should show a spinner.

    Read-only-gated because probing hits local services; the sidecar
    is fine with bursty reprobes but we still respect read-only mode
    for consistency with the rest of the mutating endpoints.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked

    try:
        from work_buddy.tools import _register_default_probes, probe_all
        from work_buddy.control.graph import build_graph, cache_info, invalidate_graph

        _register_default_probes()
        probe_all(force=True)
        invalidate_graph()
        nodes = build_graph(force=True)
        return jsonify({
            "nodes": {nid: n.to_dict() for nid, n in nodes.items()},
            "cache": cache_info(),
        })
    except Exception as exc:
        logger.exception("reprobe-all failed")
        return jsonify({"error": str(exc)}), 500


@app.post("/api/control/fix/<path:req_id>")
def api_control_fix(req_id: str):
    """Apply the registered fix for a requirement.

    Body: ``{"params": {field: value, ...}}`` for input_required
    requirements; empty/omitted for programmatic and agent_handoff.

    Returns a structured ``{ok, detail, side_effects, recheck, spawned}``
    so the UI can show what happened (success vs apply-but-recheck-failed
    vs error) without losing detail.

    Gated by read-only mode. Auto-grants the consent for the fix —
    clicking the button IS the consent, same pattern as the preference
    toggle and workflow-launch.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked

    data = request.get_json(silent=True) or {}
    params = data.get("params") if isinstance(data.get("params"), dict) else {}

    try:
        from work_buddy.consent import grant_consent
        from work_buddy.control.fix_runner import run_fix

        grant_consent(f"setup.fix_requirement", mode="once")
        result = run_fix(req_id, params=params)
        return jsonify(result)
    except Exception as exc:
        logger.exception("Fix dispatcher failed for %s", req_id)
        return jsonify({
            "ok": False,
            "detail": str(exc),
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }), 500


@app.post("/api/control/help/<path:node_id>")
def api_control_help(node_id: str):
    """Spawn a Claude Code help session focused on a specific control-graph node.

    Universal "?" button on requirements (when not ok) and components.
    Replaces the legacy Status-tab `🪄 /wb-setup diagnose` hint with a
    structured brief that bundles DiagnosticRunner output + requirement
    metadata + current state.

    Read-only-mode-gated since spawning a new agent is a side-effect.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked

    try:
        from work_buddy.control.help_briefs import spawn_help_agent
        result = spawn_help_agent(node_id)
        status = 200 if result.get("ok") else 500
        return jsonify(result), status
    except Exception as exc:
        logger.exception("Help-agent dispatcher failed for %s", node_id)
        return jsonify({"ok": False, "detail": str(exc)}), 500


@app.get("/api/control/graph")
def api_control_graph():
    """Unified control graph — domains, subsystems, components, requirements, capabilities.

    Read-only view-model fused from preferences, health, requirements,
    and the MCP registry. Frontend Settings tab consumes this.

    Query params:
        force: '1'/'true' to bypass the 45-s TTL cache and rebuild.
    """
    try:
        from work_buddy.control.graph import build_graph, cache_info
        force_raw = (request.args.get("force") or "").lower()
        force = force_raw in ("1", "true", "yes")
        nodes = build_graph(force=force)
        return jsonify({
            "nodes": {nid: n.to_dict() for nid, n in nodes.items()},
            "cache": cache_info(),
        })
    except Exception as exc:
        logger.exception("Failed to build control graph")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/dashboard/cards/<mount_point>")
def api_dashboard_cards(mount_point: str):
    """Active dashboard cards for a mount point.

    Evaluates each registered card's gate against the set of
    not-opted-out components and returns the cards that should mount,
    in render order. Powers the registry-driven Settings → Activity
    sub-view; see ``architecture/feature-cards``.
    """
    try:
        from work_buddy.dashboard.cards import cards_for_tab
        return jsonify({"cards": cards_for_tab(mount_point)})
    except Exception as exc:
        logger.exception("Failed to list dashboard cards for %s", mount_point)
        return jsonify({"error": str(exc)}), 500


# Background-refreshed cache for the full requirements snapshot. The
# requirement sweep spawns subprocesses (scheduled-task / gh / tailscale
# checks) that take 10s+, so — like /api/state — we serve a possibly-stale
# snapshot and refresh on a background thread, keeping the cost off every
# request after the first.
_REQ_CACHE_TTL = 30.0
_req_cache: dict[str, Any] | None = None
_req_cache_ts: float = 0.0
_req_cache_lock = threading.Lock()
_req_refreshing = False


def _build_requirements_snapshot() -> dict[str, Any]:
    from work_buddy.health.requirements import RequirementChecker
    checker = RequirementChecker()
    bootstrap = checker.check_bootstrap()
    all_reqs = checker.check_all(include_unwanted=False)
    return {
        "bootstrap": {
            "summary": checker.summarize(bootstrap),
            "results": [r.to_dict() for r in bootstrap],
        },
        "all": {
            "summary": checker.summarize(all_reqs),
            "results": [r.to_dict() for r in all_reqs],
        },
    }


def _kick_requirements_refresh() -> None:
    global _req_refreshing
    with _req_cache_lock:
        if _req_refreshing:
            return
        _req_refreshing = True

    def _refresh() -> None:
        global _req_cache, _req_cache_ts, _req_refreshing
        try:
            fresh = _build_requirements_snapshot()
            _req_cache = fresh
            _req_cache_ts = time.time()
        except Exception as exc:
            logger.warning("Background requirements refresh failed: %s", exc)
        finally:
            _req_refreshing = False

    threading.Thread(
        target=_refresh, name="requirements-refresh", daemon=True,
    ).start()


def get_requirements_snapshot() -> dict[str, Any]:
    """Requirements snapshot, served from a background-refreshed cache."""
    global _req_cache, _req_cache_ts
    cache = _req_cache
    if cache is not None:
        if time.time() - _req_cache_ts >= _REQ_CACHE_TTL:
            _kick_requirements_refresh()
        return cache
    with _req_cache_lock:
        if _req_cache is not None:
            return _req_cache
        _req_cache = _build_requirements_snapshot()
        _req_cache_ts = time.time()
        return _req_cache


@app.get("/api/requirements")
def api_requirements():
    """Full requirements validation results (background-cached)."""
    try:
        return jsonify(get_requirements_snapshot())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/requirements/<component_id>")
def api_requirements_component(component_id: str):
    """Requirements for a specific component."""
    try:
        from work_buddy.health.requirements import RequirementChecker
        checker = RequirementChecker()
        results = checker.check_component(component_id)
        return jsonify({
            "component": component_id,
            "summary": checker.summarize(results),
            "results": [r.to_dict() for r in results],
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/tasks")
def api_tasks():
    """Task summaries from Obsidian Tasks."""
    return jsonify(get_tasks_summary())


@app.post("/api/task_sync")
def api_task_sync():
    """Trigger a task_sync run from the dashboard's Sync button.

    The user's click is the consent boundary — wrap the underlying
    capability invocation in ``user_initiated('dashboard.task_sync')``
    so any nested ``@requires_consent`` gates inside ``task_sync`` /
    its mutations pass through without re-prompting.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    try:
        from work_buddy.consent import user_initiated
        from work_buddy.obsidian.tasks.sync import task_sync
    except Exception as exc:
        return jsonify({"ok": False, "error": f"import failed: {exc}"}), 500
    try:
        with user_initiated("dashboard.task_sync"):
            result = task_sync()
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        logger.exception("api_task_sync: task_sync failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/tasks/search")
def api_tasks_search():
    """Search tasks by text.

    Query params:
        q: search query
        state: comma-separated state filter
        limit: max results (default 20)
        method: 'substring' (default) or 'hybrid' (BM25 + semantic)
    """
    q = (request.args.get("q") or "").strip()
    q_lower = q.lower()
    state_filter = request.args.get("state", "")
    limit = request.args.get("limit", 20, type=int)
    method = request.args.get("method", "substring").strip().lower()

    data = get_tasks_summary()
    tasks = data.get("tasks", [])

    # Filter by state (comma-separated)
    if state_filter:
        allowed = {s.strip() for s in state_filter.split(",")}
        tasks = [t for t in tasks if t.get("state") in allowed]

    # Exclude done tasks by default (unless explicitly requested)
    if "done" not in (state_filter or ""):
        tasks = [t for t in tasks if not t.get("done")]

    if not q:
        return jsonify({"query": q, "count": len(tasks), "tasks": tasks[:limit], "method": "none"})

    # --- Hybrid search (BM25 + semantic via embedding service) ---
    if method == "hybrid" and len(tasks) > 0:
        scored = _hybrid_task_search(q, tasks, limit)
        if scored is not None:
            return jsonify({"query": q, "count": len(scored), "tasks": scored, "method": "hybrid"})
        # Fall through to substring if hybrid failed
        logger.debug("Hybrid search unavailable, falling back to substring")

    # --- Substring fallback ---
    tasks = [t for t in tasks if q_lower in (t.get("text") or "").lower() or q_lower in (t.get("id") or "").lower()]

    return jsonify({"query": q, "count": len(tasks), "tasks": tasks[:limit], "method": "substring"})


@app.get("/api/namespaces")
def api_namespaces():
    """Every namespacey tag in the task-tag cache, with open-task counts.

    Query params:
        recent_days: window for the ``recent_count`` column (default 14).
    """
    from work_buddy.dashboard.api import list_namespaces
    recent_days = request.args.get("recent_days", 14, type=int)
    return jsonify(list_namespaces(recent_days=recent_days))


@app.get("/api/tasks/by-namespace/<path:namespace>")
def api_tasks_by_namespace(namespace: str):
    """Tasks filtered to a namespace tag.

    Query params:
        descendants: '1' (default) includes sub-namespaces; '0' is exact match only.
    """
    from work_buddy.dashboard.api import get_tasks_by_namespace
    raw = request.args.get("descendants", "1").strip()
    include_descendants = raw not in ("0", "false", "no", "")
    return jsonify(get_tasks_by_namespace(namespace, include_descendants=include_descendants))


@app.get("/api/sessions")
def api_sessions():
    """Active agent sessions (legacy)."""
    return jsonify(get_sessions_summary())


# ---------------------------------------------------------------------------
# Chats (rich session browsing & search)
# ---------------------------------------------------------------------------

@app.get("/api/chats")
def api_chats():
    """Rich chat list from Claude Code JSONL sessions."""
    days = request.args.get("days", 14, type=int)
    return jsonify(get_chats_summary(days))


@app.get("/api/chats/<session_id>/messages")
def api_chat_messages(session_id: str):
    """Paginated message browsing for a session."""
    from work_buddy.sessions.inspector import session_get

    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 20, type=int)
    roles = request.args.get("roles")
    message_types = request.args.get("message_types")
    query = request.args.get("query")

    try:
        result = session_get(
            session_id, offset=offset, limit=limit,
            roles=roles, message_types=message_types, query=query,
        )
        if isinstance(result, str):
            return jsonify({"error": result}), 400
        return jsonify(result)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("chat messages error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/chats/<session_id>/expand/<int:message_index>")
def api_chat_expand(session_id: str, message_index: int):
    """Full untruncated text around a specific message."""
    from work_buddy.sessions.inspector import session_expand

    ctx = request.args.get("context_window", 0, type=int)
    try:
        result = session_expand(session_id, message_index, context_window=ctx)
        if isinstance(result, str):
            return jsonify({"error": result}), 400
        return jsonify(result)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("chat expand error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/chats/search")
def api_chats_search():
    """Cross-session hybrid IR search, grouped by session.

    Returns sessions ranked by top-k weighted chunk score aggregation,
    each containing their constituent chunk hits.
    """
    from collections import defaultdict

    from work_buddy.ir.engine import top_k_weighted_score
    from work_buddy.ir.search import search as ir_search

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "query parameter 'q' is required"}), 400

    method = request.args.get("method", "keyword,semantic")
    top_k = request.args.get("top_k", 20, type=int)
    project = request.args.get("project", "").strip() or None

    # eligible_sids carries the dashboard's pill-filter outcome:
    # "after applying my listing-mode filters, here are the session_ids
    # I want the search to score within." Comma-separated. When
    # present, the IR engine restricts its scoring corpus before
    # applying top-K — filter-then-rank, the correct semantics for
    # composing filters with relevance.
    eligible_sids_raw = request.args.get("eligible_sids", "").strip()
    eligible_sids: list[str] | None = None
    if eligible_sids_raw:
        eligible_sids = [s for s in eligible_sids_raw.split(",") if s]

    # Build metadata filter. Project pre-filter applies in SQLite via
    # json_extract; the eligible_sids pre-filter uses the new
    # list-valued metadata_filter (one IN clause). Both compose with AND.
    meta_filter: dict[str, Any] = {}
    if project:
        meta_filter["project_name"] = project
    if eligible_sids is not None:
        meta_filter["session_id"] = eligible_sids
    meta_filter = meta_filter or None

    try:
        hits = ir_search(q, source="conversation", method=method, top_k=top_k,
                         metadata_filter=meta_filter)
        if isinstance(hits, str):
            return jsonify({"error": hits}), 400
    except Exception as exc:
        logger.exception("chats search error")
        return jsonify({"error": str(exc)}), 500

    # Group chunks by session
    session_chunks: dict[str, list] = defaultdict(list)
    session_meta: dict[str, dict] = {}

    for hit in hits:
        sid = (hit.get("doc_id") or "").split(":")[0]
        if not sid:
            continue
        session_chunks[sid].append({
            "span_index": (hit.get("metadata") or {}).get("span_index", 0),
            "display_text": hit.get("display_text", ""),
            "score": hit.get("score", 0),
        })
        if sid not in session_meta:
            meta = hit.get("metadata") or {}
            session_meta[sid] = {
                "project_name": meta.get("project_name", ""),
                "start_time": meta.get("start_time"),
            }

    # Score and rank sessions using top-k weighted aggregation
    sessions = []
    for sid, chunks in session_chunks.items():
        chunk_scores = [c["score"] for c in chunks]
        doc_score = top_k_weighted_score(chunk_scores)
        meta = session_meta.get(sid, {})
        sessions.append({
            "session_id": sid,
            "short_id": sid[:8],
            "doc_score": round(doc_score, 6),
            "project_name": meta.get("project_name", ""),
            "start_time": meta.get("start_time"),
            "chunks": chunks,
        })

    sessions.sort(key=lambda s: s["doc_score"], reverse=True)

    return jsonify({
        "query": q,
        "method": method,
        "total_chunks": len(hits),
        "sessions": sessions,
    })


@app.get("/api/chats/<session_id>/search")
def api_chat_session_search(session_id: str):
    """Hybrid search within a single session."""
    from work_buddy.sessions.inspector import session_search

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "query parameter 'q' is required"}), 400

    method = request.args.get("method", "keyword,semantic")
    top_k = request.args.get("top_k", 5, type=int)

    try:
        result = session_search(session_id, q, method=method, top_k=top_k)
        if isinstance(result, str):
            return jsonify({"error": result}), 400
        return jsonify(result)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("session search error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/chats/<session_id>/locate/<int:span_index>")
def api_chat_locate(session_id: str, span_index: int):
    """Jump from a search hit to the conversation page."""
    from work_buddy.sessions.inspector import session_locate

    try:
        result = session_locate(session_id, span_index)
        if isinstance(result, str):
            return jsonify({"error": result}), 400
        return jsonify(result)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("chat locate error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/chats/<session_id>/commits")
def api_chat_commits(session_id: str):
    """Git commits made during a session."""
    from work_buddy.sessions.inspector import session_commits

    try:
        result = session_commits(session_id=session_id)
        if isinstance(result, str):
            return jsonify({"error": result}), 400
        return jsonify(result)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("chat commits error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/chats/<session_id>/topics")
def api_chat_topics(session_id: str):
    """Cached LLM topic summaries for a session.

    Returns ``{topics: [...], tldr: str | None}``. Empty topics list +
    ``tldr=None`` when the session hasn't been summarized (the LLM
    summary feature is gated off by default). The dashboard hides its
    topic-timeline rail entirely when topics is empty.
    """
    try:
        from work_buddy.conversation_observability.session_summary_row import (
            session_summary_row,
        )
    except ImportError:
        return jsonify({"topics": [], "tldr": None})

    try:
        row = session_summary_row(session_id)
        if row is None:
            return jsonify({"topics": [], "tldr": None})
        tldr = None
        if row.get("status") == "ok":
            tldr = row.get("tldr") or None
        return jsonify({"topics": row["topics"], "tldr": tldr})
    except Exception as exc:
        logger.exception("chat topics error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/chats/<session_id>/tasks")
def api_chat_tasks(session_id: str):
    """Per-session task provenance for the chat-detail "Tasks" rail.

    Returns ``{tasks: [{task_id, task_text, state, roles, assigned_at}]}``
    where ``roles`` ⊆ {created, assigned, developed}. Richer than the
    listing entry's assigned-only ``tasks_detail``: it also surfaces tasks
    this session *created* or *developed* (committed referencing). Empty
    list when the session touched no tasks. Bridge-independent.
    """
    try:
        from work_buddy.obsidian.tasks.provenance import build_session_task_roles

        return jsonify(build_session_task_roles(session_id))
    except Exception as exc:
        logger.exception("chat tasks error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/chats/<session_id>/uncommitted-files")
def api_chat_uncommitted_files(session_id: str):
    """Files this session wrote that are still dirty in git RIGHT NOW.

    Distinct from the chat-card's ``unfinished_count`` badge (which
    uses the stable historical signal). This endpoint answers the
    actionable question — "what work from this session can I still
    pick up?". Returns ``{files: [{path, basename, repo}], count: N}``.
    Empty list when the session has no current-dirty files attached.
    """
    try:
        from work_buddy.conversation_observability.db import get_connection
        from work_buddy.conversation_observability.writes import (
            _committed_files_for_sessions,
        )
        from work_buddy.config import load_config
    except ImportError:
        return jsonify({"files": [], "count": 0})

    try:
        cfg = load_config()
        repos_root = Path(cfg["repos_root"]).resolve()
    except Exception as exc:
        return jsonify({"files": [], "count": 0, "error": str(exc)}), 200

    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT file_path FROM session_file_writes "
                "WHERE session_id = ? AND currently_dirty = 1 "
                "ORDER BY write_timestamp DESC",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()

        # Drop files this session also committed itself — those aren't
        # actually uncommitted, just dirty-again-after-commit due to
        # later edits.
        committed = _committed_files_for_sessions({session_id}, repos_root)
        committed_set = committed.get(session_id, set())

        root_str = repos_root.as_posix().rstrip("/") + "/"
        files: list[dict[str, str]] = []
        for r in rows:
            fp = r["file_path"]
            if fp in committed_set:
                continue
            repo = ""
            rel_path = fp  # fallback: full path when not under repos_root
            if fp.startswith(root_str):
                rel = fp[len(root_str):]
                parts = rel.split("/", 1)
                if parts:
                    repo = parts[0]
                    # Path relative to the REPO root (drops both the
                    # repos_root prefix and the repo name itself).
                    rel_path = parts[1] if len(parts) > 1 else ""
            files.append({
                "path": fp,
                "basename": Path(fp).name,
                "rel_path": rel_path,
                "repo": repo,
            })
        return jsonify({"files": files, "count": len(files)})
    except Exception as exc:
        logger.exception("chat uncommitted-files error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/chats/search/commits")
def api_chats_search_commits():
    """Search commits across all recent sessions via hybrid ranking.

    Supports hash-prefix matching (short hex strings) and hybrid BM25+semantic
    search over commit messages.  Returns results grouped by session, ranked
    by best commit score.
    """
    import re
    from collections import defaultdict

    from work_buddy.sessions.inspector import session_commits

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "query parameter 'q' is required"}), 400

    days = request.args.get("days", 30, type=int)
    project = request.args.get("project", "").strip() or None

    try:
        result = session_commits(days=days, project=project)
        all_commits = result.get("commits", [])
    except Exception as exc:
        logger.exception("commit search: gather error")
        return jsonify({"error": str(exc)}), 500

    if not all_commits:
        return jsonify({"query": q, "total_commits": 0, "sessions": []})

    # --- Hash-prefix search (all hex, 4+ chars) ---
    is_hash = bool(re.fullmatch(r"[0-9a-fA-F]{4,40}", q))
    if is_hash:
        q_lower = q.lower()
        scored = [
            (c, 1.0 if c.get("hash", "").lower().startswith(q_lower) else 0.0)
            for c in all_commits
        ]
        scored = [(c, s) for c, s in scored if s > 0]
    else:
        # --- Hybrid search over commit messages ---
        try:
            from work_buddy.embedding.client import hybrid_search

            # Build candidates: one per commit, keyed by index
            candidates = []
            for i, c in enumerate(all_commits):
                candidates.append({
                    "name": str(i),
                    "texts": [c.get("message", "")],
                })
            ranked = hybrid_search(q, candidates)
            score_map = {r["name"]: r["score"] for r in ranked}

            scored = []
            for i, c in enumerate(all_commits):
                s = score_map.get(str(i), 0.0)
                if s > 0:
                    scored.append((c, s))

            # Keep only top results to avoid flooding the UI
            scored.sort(key=lambda x: x[1], reverse=True)
            scored = scored[:20]
        except Exception as exc:
            logger.exception("commit search: hybrid error")
            return jsonify({"error": str(exc)}), 500

    # --- Group by session, rank sessions by best commit score ---
    session_commits_map: dict[str, list] = defaultdict(list)
    for commit, score in scored:
        sid = commit.get("session_id", "")
        session_commits_map[sid].append({
            "hash": commit.get("hash", ""),
            "message": commit.get("message", ""),
            "branch": commit.get("branch", ""),
            "files_changed": commit.get("files_changed"),
            "timestamp": commit.get("timestamp", ""),
            "message_index": commit.get("message_index"),
            "score": round(score, 4),
        })

    sessions = []
    for sid, commits_list in session_commits_map.items():
        commits_list.sort(key=lambda c: c["score"], reverse=True)
        best_score = commits_list[0]["score"] if commits_list else 0
        sessions.append({
            "session_id": sid,
            "short_id": sid[:8],
            "doc_score": round(best_score, 6),
            "commits": commits_list,
        })

    sessions.sort(key=lambda s: s["doc_score"], reverse=True)

    return jsonify({
        "query": q,
        "total_commits": len(scored),
        "sessions": sessions,
    })


@app.post("/api/chats/commits/prepare")
def api_chats_commits_prepare():
    """Pre-embed all recent commit messages so search is fast.

    Called when the user switches the search dropdown to 'Commit' mode.
    Fires a dummy search to warm the embedding service's candidate cache.
    """
    from work_buddy.sessions.inspector import session_commits

    days = request.args.get("days", 30, type=int)
    project = request.args.get("project", "").strip() or None
    try:
        result = session_commits(days=days, project=project)
        all_commits = result.get("commits", [])
        if not all_commits:
            return jsonify({"status": "ok", "commit_count": 0})

        from work_buddy.embedding.client import hybrid_search

        candidates = [
            {"name": str(i), "texts": [c.get("message", "")]}
            for i, c in enumerate(all_commits)
        ]
        # Warm the cache with a throwaway query
        hybrid_search("warmup", candidates)

        return jsonify({"status": "ok", "commit_count": len(all_commits)})
    except Exception as exc:
        logger.exception("commit prepare error")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/contracts")
def api_contracts():
    """Active contract summaries."""
    return jsonify(get_contracts_summary())


@app.get("/api/embeddings")
def api_embeddings():
    """System (IR/knowledge) + User (vaults) status for Settings › Embeddings."""
    return jsonify(get_embeddings_summary())


@app.get("/api/inference-activity")
def api_inference_activity():
    """Cross-provider inference-call provenance feed for Settings › Inference (cached)."""
    return jsonify(get_inference_activity())


@app.get("/api/fleet")
def api_fleet():
    """Local model fleet snapshot for Settings › Inference (per-machine, cached)."""
    return jsonify(get_fleet_summary())


@app.post("/api/fleet/roster")
def api_fleet_roster():
    """Add/update or clear a machine's inference.fleet roster entry (Settings › Inference).

    Thin wrapper around the ``fleet_roster`` capability (mirrors ``/api/embeddings/vault``).
    The user clicking Save IS the consent; read-only mode blocks the write. On success
    the fleet snapshot is busted and ``fleet.changed`` is published so the cards update
    immediately.
    """
    blocked = _reject_read_only()
    if blocked is not None:
        return blocked

    payload = request.get_json(silent=True) or {}
    from work_buddy.mcp_server.registry import get_registry

    cap = get_registry().get("fleet_roster")
    if cap is None:
        return jsonify({"success": False, "error": "fleet_roster capability not registered "
                        "(reload MCP / rebuild the knowledge store)."}), 500
    try:
        result = cap.callable(**payload)
    except TypeError as exc:
        return jsonify({"success": False, "error": f"Invalid arguments: {exc}"}), 400

    if result.get("success"):
        from work_buddy.dashboard.api import bust_fleet_cache
        bust_fleet_cache()
        from work_buddy.dashboard.events import publish_auto
        publish_auto("fleet.changed",
                     {"reason": "roster_changed", "device_id": result.get("device_id")})
    return jsonify(result), (200 if result.get("success") else 400)


@app.post("/api/embeddings/vault")
def api_embeddings_vault():
    """Add/update or remove a vault config (Settings › Embeddings editor).

    Thin wrapper around the ``vault_config`` capability (mirrors ``/api/user_jobs``).
    The user clicking Save IS the consent; read-only mode blocks the write. On
    success the embeddings snapshot is busted so the new row shows immediately
    (counts won't change until the next build).
    """
    blocked = _reject_read_only()
    if blocked is not None:
        return blocked

    payload = request.get_json(silent=True) or {}
    from work_buddy.mcp_server.registry import get_registry

    cap = get_registry().get("vault_config")
    if cap is None:
        return jsonify({"success": False, "error": "vault_config capability not registered "
                        "(reload MCP / rebuild the knowledge store)."}), 500
    try:
        result = cap.callable(**payload)
    except TypeError as exc:
        return jsonify({"success": False, "error": f"Invalid arguments: {exc}"}), 400

    if result.get("success"):
        from work_buddy.dashboard.api import bust_embeddings_cache
        bust_embeddings_cache()
        from work_buddy.dashboard.events import publish_auto
        publish_auto("embeddings.vault_changed",
                     {"id": result.get("id"), "action": result.get("action")})
    return jsonify(result), (200 if result.get("success") else 400)


# ---------------------------------------------------------------------------
# Costs tab
# ---------------------------------------------------------------------------
#
# Aggregates first-party LLM cost log files written by ``work_buddy.llm.cost``
# at ``<data_root>/agents/<session>/llm_costs.jsonl``. Phase 2 adds Claude Code
# transcript-derived usage as a second source through the same endpoint.


@app.get("/api/costs")
def api_costs():
    """Aggregated LLM cost / usage summary across all agent sessions.

    Optional query params:
        source: ``internal`` (default), ``claude_code``, or ``all``.
        project: substring match on project name / cwd. When set, every
            aggregate is computed only over matching sessions/turns.
    """
    source = (request.args.get("source") or "internal").lower()
    project = request.args.get("project") or None
    execution_mode = (request.args.get("execution_mode") or "").lower() or None
    # Date range — frontend passes ``YYYY-MM-DD`` strings derived from the
    # range pill. The backend filters every aggregate by this window so
    # cards / tables / charts agree.
    start_date = request.args.get("start_date") or None
    end_date = request.args.get("end_date") or None
    # Comma-separated list of model names from the chip filter.
    #   missing      → ``None``  (no filter)
    #   ``models=``  → ``[]``    (match nothing; user de-selected every chip)
    #   ``models=a,b`` → ``["a","b"]``
    # The missing-vs-empty distinction matters: without it, de-selecting
    # every chip silently falls back to all-time data.
    if "models" in request.args:
        models_raw = request.args.get("models") or ""
        models: list[str] | None = [
            m for m in (s.strip() for s in models_raw.split(",")) if m
        ]
    else:
        models = None
    # Backwards-compat: the old ``transcripts`` source name still routes
    # to claude_code so any external bookmarks / scripts keep working.
    if source == "transcripts":
        source = "claude_code"
    try:
        from work_buddy.dashboard.costs import get_costs_summary
        internal = get_costs_summary(project=project,
                                      execution_mode=execution_mode,
                                      start_date=start_date,
                                      end_date=end_date,
                                      models=models)
        if source == "internal":
            return jsonify(internal)

        claude_code: dict | None = None
        try:
            from work_buddy.dashboard.costs_claude_code_usage import (
                get_claude_code_usage_summary,
            )
            claude_code = get_claude_code_usage_summary(
                project=project,
                start_date=start_date,
                end_date=end_date,
                models=models,
            )
        except ImportError:
            claude_code = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("claude_code_usage source failed: %s", exc)
            claude_code = {"error": str(exc), "source": "claude_code"}

        if source == "claude_code":
            return jsonify(claude_code or {"source": "claude_code",
                                            "available": False})

        return jsonify({
            "internal": internal,
            "claude_code": claude_code,
            "source": "all",
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("Cost aggregation failed")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/costs/projects")
def api_costs_projects():
    """List of projects that have cost data, with counts and recency.

    Pinning order in the response:
      1. ``__all__`` placeholder ("All projects" pseudo-project).
      2. ``work-buddy`` if it has any data.
      3. Other projects, sorted by ``last_seen`` desc.

    Each entry has::

        {
          "name": "work-buddy",
          "session_count": 42,
          "last_seen": "2026-04-25T20:14:00",
          "in_internal": true,    # has rows in the per-call log
          "in_claude_code": true, # has rows in the transcripts cache
        }
    """
    try:
        projects: dict[str, dict] = {}

        # The same canonical resolver the Chats tab uses — collapses
        # worktrees/feature-dirs back to their parent project.
        from work_buddy.dashboard.costs import _resolve_project_name

        # Internal source (per-call log) — collect from session manifests.
        try:
            from work_buddy.dashboard.costs import (
                _iter_session_dirs, _read_session_manifest,
            )
            for sd in _iter_session_dirs():
                if not (sd / "llm_costs.jsonl").exists():
                    continue
                m = _read_session_manifest(sd)
                proj_path = m.get("project") or ""
                if not proj_path:
                    continue
                name = _resolve_project_name(proj_path)
                if not name:
                    continue
                p = projects.setdefault(name, {
                    "name": name, "session_count": 0,
                    "last_seen": "", "in_internal": False,
                    "in_claude_code": False,
                })
                p["session_count"] += 1
                p["in_internal"] = True
                # Use the manifest's created_at as the recency proxy.
                created = m.get("created_at") or ""
                if created > p["last_seen"]:
                    p["last_seen"] = created
        except Exception as exc:  # noqa: BLE001
            logger.debug("Internal-source project scan failed: %s", exc)

        # Claude Code source — query the cache DB. We resolve names from
        # the per-row ``cwd`` (rather than the stored ``project_name`` on
        # the sessions table) so the canonical resolver applies even when
        # the scanner stamped a stale name pre-fix.
        try:
            import sqlite3
            from work_buddy.llm.claude_code_usage import scanner as _scanner
            db = _scanner.get_db_path()
            if db.exists():
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                try:
                    # One representative cwd per session, with row counts.
                    for s in conn.execute("""
                        SELECT t.session_id, t.cwd, COUNT(*) AS n,
                               MAX(t.timestamp) AS last_seen
                        FROM turns t
                        WHERE t.cwd IS NOT NULL AND t.cwd != ''
                        GROUP BY t.session_id
                    """):
                        cwd = s["cwd"] or ""
                        name = _resolve_project_name(cwd)
                        if not name:
                            continue
                        p = projects.setdefault(name, {
                            "name": name, "session_count": 0,
                            "last_seen": "", "in_internal": False,
                            "in_claude_code": False,
                        })
                        # +1 session per row in the GROUP BY result
                        p["session_count"] += 1
                        p["in_claude_code"] = True
                        ls = s["last_seen"] or ""
                        if ls > p["last_seen"]:
                            p["last_seen"] = ls
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Claude-Code-source project scan failed: %s", exc)

        # Pin "work-buddy" first, then sort rest by recency.
        rest = [p for p in projects.values() if p["name"].lower() != "work-buddy"]
        rest.sort(key=lambda p: p["last_seen"], reverse=True)
        ordered: list[dict] = []
        wb = projects.get("work-buddy")
        if wb:
            ordered.append(wb)
        ordered.extend(rest)

        return jsonify({"projects": ordered, "count": len(ordered)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Project list failed")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/costs/rate-limits")
def api_costs_rate_limits():
    """Return the most-recent Anthropic rate-limit observations per model.

    Read-only view of ``<data_root>/runtime/rate_limits.json``, populated by
    the runner whenever it makes a successful Anthropic API call.
    Empty ``observations`` when no calls have been recorded yet.
    """
    try:
        from work_buddy.llm.rate_limits import read_observations
        return jsonify({"observations": read_observations()})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rate-limit fetch failed")
        return jsonify({"error": str(exc)}), 500


@app.post("/api/costs/rescan")
def api_costs_rescan():
    """Re-scan Claude Code transcripts to refresh the claude_code source."""
    if reject := _reject_read_only():
        return reject
    try:
        from work_buddy.dashboard.costs_claude_code_usage import (
            rescan_claude_code_usage,
        )
    except ImportError:
        return jsonify({"available": False,
                        "message": "Claude Code usage scanner not available."})
    try:
        result = rescan_claude_code_usage()
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Cost rescan failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Vendored static assets (Chart.js)
# ---------------------------------------------------------------------------


@app.get("/vendor/<path:filename>")
def static_vendor(filename: str):
    """Serve vendored frontend assets (Chart.js, etc.) from ``frontend/vendor/``.

    Path is ``/vendor/...`` rather than ``/static/...`` because Flask's
    default static endpoint is registered at ``/static/`` and would
    shadow this route (first-registered wins on collision).
    """
    safe = filename.replace("\\", "/").lstrip("/")
    if ".." in safe.split("/"):
        return "", 404
    vendor_dir = Path(__file__).parent / "frontend" / "vendor"
    target = vendor_dir / safe
    if not target.exists() or not target.is_file():
        return "", 404
    if safe.endswith(".js"):
        mime = "application/javascript"
    elif safe.endswith(".css"):
        mime = "text/css"
    elif safe.endswith(".map"):
        mime = "application/json"
    else:
        mime = "application/octet-stream"
    return send_file(target, mimetype=mime)


@app.get("/assets/<path:filename>")
def static_assets(filename: str):
    """Serve the content-hashed frontend bundle (``app.<hash>.js|css``).

    Built in-memory by ``work_buddy.dashboard.frontend`` (no files on disk).
    The filename carries a content hash, so the response is safe to cache
    forever: any change to the frontend produces a new hash and therefore a
    new URL. The ``GET /`` document stays no-store and always points at the
    current hashed names, so cache-busting is automatic.
    """
    from work_buddy.dashboard.frontend import get_asset
    asset = get_asset(filename)
    if asset is None:
        return "", 404
    data, ctype = asset
    resp = Response(data, content_type=ctype)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------------------
# Review tab + Resolution Surface endpoints removed
# ---------------------------------------------------------------------------
#
# The /api/review, /api/review/execute, /api/triage/defer, and
# /api/triage/redirect endpoints + their Review-tab UI were retired
# during the clarify -> Threads migration. Triage now flows through
# the unified source pipeline (``run_source_pipeline``); per-cluster
# actions surface on the Threads tab via group sub-threads.


# ---------------------------------------------------------------------------
# Engage view payload
# ---------------------------------------------------------------------------
#
# Originally drove the legacy Engage tab; that surface was removed once
# the Threads tab became the canonical "what should I act on" UI. The
# helper survives because ``work_buddy.task_me.load_context_for_task_me``
# still composes it into the Today tab's payload (focus list filtered
# by who-can-act + user-current contexts). No HTTP route is mounted —
# this is a private collaborator of the Today builder below.

def _build_engage_view_payload(*, current_contexts: list[str] | None = None) -> dict:
    """Per-task tier × who_can_act × user-current snapshot.

    Returns every open task with:
    - The Slice-4 operating-tier decision (so the engage view can show
      the Auto column).
    - The Slice-5a who-can-act decision (so the engage view can filter
      and render handoff badges).
    - Whether the task is currently blocked given the user's declared
      ``current_contexts`` (subset of declared user contexts that
      matter for the user-side check).

    No mutations; safe to call on every render.  ``current_contexts``
    is forwarded to :func:`user_satisfies_against` per-task.
    """
    from work_buddy.threads.models import Task
    from work_buddy.automation.risk import resolve_operating_tier
    from work_buddy.automation.contexts import (
        parse_context_list,
        resolve_who_can_act,
        user_satisfies_against,
        list_known_context_tokens,
    )
    from work_buddy.clarify.resolution import PIPELINE_BLOCKER_PRESENTATION

    cfg = load_config()
    rows = [t.row for t in Task.query(include_archived=False)]
    items: list[dict] = []

    for row in rows:
        if row.get("state") in {"done", "archived"}:
            continue

        decision = resolve_operating_tier(row, config=cfg)
        who = resolve_who_can_act(
            row.get("agent_required_contexts"),
            row.get("user_required_contexts"),
        )
        user_now_satisfied, user_now_unmet = user_satisfies_against(
            row.get("user_required_contexts"),
            current_contexts,
        )

        blocker_view = None
        if decision.pipeline_blocker is not None:
            base = PIPELINE_BLOCKER_PRESENTATION.get(
                decision.pipeline_blocker, {},
            )
            blocker_view = {
                "kind": decision.pipeline_blocker,
                "label": base.get("label", decision.pipeline_blocker),
                "tone": base.get("tone", "info"),
                "deep_link": base.get("deep_link"),
                "deep_link_label": base.get("deep_link_label"),
                "detail": "; ".join(decision.reasons) or None,
            }

        items.append({
            "task_id": row.get("task_id"),
            "text": row.get("description") or row.get("task_id"),
            "state": row.get("state"),
            "urgency": row.get("urgency"),
            "contract": row.get("contract"),
            "auto": {
                "achievable": decision.achievable,
                "operating": decision.operating,
                "pipeline_blocker": blocker_view,
                "last_actor": row.get("last_actor"),
            },
            "who_can_act": {
                "agent": who.agent,
                "user": who.user,
                "blocked": who.blocked,
                "agent_unmet": list(who.agent_unmet),
                "user_unmet": list(who.user_unmet),
                "agent_handoff_eligible": who.agent_handoff_eligible,
                "agent_required_contexts": parse_context_list(
                    row.get("agent_required_contexts"),
                ),
                "user_required_contexts": parse_context_list(
                    row.get("user_required_contexts"),
                ),
                "source": row.get("required_contexts_source"),
            },
            "user_now": {
                "satisfied": user_now_satisfied,
                "unmet": list(user_now_unmet),
            },
        })

    return {
        "status": "ok",
        "count": len(items),
        "current_contexts": list(current_contexts or []),
        "known_tokens": list_known_context_tokens(),
        "items": items,
    }


def _build_today_payload(*, current_contexts: list[str] | None = None) -> dict:
    """Build the Today tab payload — read-only, no mutations.

    Composes:
    - The engage view (filtered by ``current_contexts``).
    - The clamp-to-now plan from ``work_buddy.task_me.build_now_plan``.
    - The top 1-2 recommendations heuristic from ``task_me.top_recommendations``.
    - A current-time indicator + work-hour bounds from config.

    Write-back happens via the ``/wb-task-me`` slash command's optional
    consent-gated reasoning step, never from this read path.
    """
    from work_buddy.task_me import (
        build_now_plan,
        load_context_for_task_me,
        top_recommendations,
    )
    from datetime import datetime, timezone

    context = load_context_for_task_me(user_current_contexts=current_contexts)
    plan = build_now_plan(context=context)
    engage = context.get("engage") or {}
    recs = top_recommendations(engage, limit=2)

    cfg = load_config() or {}
    work_hours = (
        cfg.get("morning", {}).get("day_planner", {}).get("work_hours", [9, 17])
    )

    now_local = datetime.now()
    now_minutes = now_local.hour * 60 + now_local.minute

    contracts = (context.get("contract_constraints") or {}).get("active") or []
    constraints = (context.get("contract_constraints") or {}).get("constraints") or []

    return {
        "status": context.get("status", "ok"),
        "now": {
            "iso": now_local.astimezone(timezone.utc).isoformat(),
            "local_hhmm": now_local.strftime("%H:%M"),
            "minutes_into_day": now_minutes,
        },
        "work_hours": work_hours,
        "current_contexts": list(current_contexts or []),
        "recommendations": recs,
        "plan": plan.get("plan") or [],
        "plan_status": plan.get("status"),
        "focused_count": plan.get("focused_count", 0),
        "calendar_event_count": plan.get("calendar_event_count", 0),
        "active_contracts": contracts,
        "contract_constraints": constraints,
        "engage_count": engage.get("count", 0),
        "errors": context.get("errors") or [],
    }


@app.get("/api/automation/today")
def api_automation_today():
    """Today tab payload — re-runnable view backed by the task-me orchestration.

    Query params:
        contexts: comma-separated tokens (forwarded to engage filter).
    """
    raw = (request.args.get("contexts") or "").strip()
    current = [t.strip() for t in raw.split(",") if t.strip()] if raw else []
    try:
        return jsonify(_build_today_payload(current_contexts=current))
    except Exception as exc:
        logger.exception("api_automation_today: failed")
        return jsonify({"status": "error", "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.get("/api/projects/_schema")
def api_projects_schema():
    """Return enum metadata for the projects store.

    Source of truth for the frontend's status grouping and ``<select>``
    options. Derived from constants in ``work_buddy.projects.store`` so
    schema additions (new lifecycle state, new origin value) propagate
    to the dashboard without a frontend code change.
    """
    try:
        from work_buddy.projects.store import (
            STATUS_DISPLAY_ORDER,
            VALID_ORIGINS,
            VALID_AUTHORS,
        )
        return jsonify({
            "statuses_display_order": list(STATUS_DISPLAY_ORDER),
            "origins": sorted(VALID_ORIGINS),
            "authors": sorted(VALID_AUTHORS),
        })
    except Exception as e:
        logger.exception("Failed to read projects schema")
        return jsonify({"error": str(e)}), 500


@app.get("/api/projects")
def api_projects():
    """List all projects with activity-score-sorted active group.

    Active projects are reordered by an exponentially-decayed activity
    score (folder mtimes + git commits + project revisions, half-life
    14 days). Non-active rows keep their canonical order from
    ``list_projects``. The ``activity_score`` field is added to each
    active row for downstream UI use.
    """
    try:
        from work_buddy.projects.store import list_projects
        from work_buddy.projects.activity import sort_active_by_activity
        projects = list_projects()
        projects = sort_active_by_activity(projects)
        return jsonify({"projects": projects})
    except Exception as e:
        logger.exception("Failed to list projects")
        return jsonify({"projects": [], "error": str(e)})


@app.get("/api/projects/<slug>")
def api_project_detail(slug: str):
    """Get a single project + folder existence flags. Fast path — no Hindsight.

    Memory is loaded async from ``/api/projects/<slug>/memory_items`` so the
    detail pane renders immediately while the (potentially slow) Hindsight
    call resolves in the background.
    """
    try:
        from pathlib import Path
        from work_buddy.projects.store import get_project
        project = get_project(slug)
        if not project:
            return jsonify({"error": f"Project '{slug}' not found"}), 404

        # Strip SQLite observations (legacy) — memory comes from Hindsight
        project.pop("observations", None)

        # Enrich folders with disk-existence flag for the dashboard UI.
        for f in project.get("folders", []):
            try:
                f["exists"] = bool(Path(f["path"]).exists())
            except OSError:
                f["exists"] = False

        return jsonify(project)
    except Exception as e:
        logger.exception("Failed to get project %s", slug)
        return jsonify({"error": str(e)}), 500


@app.post("/api/projects/<slug>")
def api_project_update(slug: str):
    """Update project identity fields via the markdown-canonical path.

    Routes the edit through :class:`ProjectMarkdownDB.apply_mutation`,
    which writes BOTH surfaces — the project's markdown note (the
    canonical store) and the projects SQLite registry — atomically. A
    dashboard edit therefore survives the next drift reconciliation;
    writing the registry alone would be overwritten by the note on the
    next reconcile pass.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        from work_buddy.markdown_db import WriteProvenance
        from work_buddy.projects.markdown_db import ProjectMarkdownDB
        from work_buddy.projects.store import VALID_STATUSES, get_project

        fields: dict[str, Any] = {}
        for key in ("name", "status", "description"):
            if key in data:
                fields[key] = data[key]
        if not fields:
            return jsonify({"error": "No fields to update"}), 400

        # Pre-validate status. apply_mutation writes the markdown note
        # BEFORE the store, so an enum failure surfacing mid-write would
        # leave the note ahead of a rejecting registry. Catch it before
        # any surface is touched.
        if "status" in fields and fields["status"] not in VALID_STATUSES:
            return jsonify({
                "error": f"Invalid status: {fields['status']!r}. "
                         f"Must be one of {sorted(VALID_STATUSES)}"
            }), 400

        if get_project(slug) is None:
            return jsonify({"error": f"Project '{slug}' not found"}), 404

        ProjectMarkdownDB().apply_mutation(
            slug, fields,
            provenance=WriteProvenance.mutation(
                frozenset({"user"}), "dashboard",
            ),
        )
        return jsonify(get_project(slug))
    except ValueError as e:
        # CHECK-constraint or enum-validation failure from the store.
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Failed to update project %s", slug)
        return jsonify({"error": str(e)}), 500


@app.post("/api/projects/<slug>/observe")
def api_project_observe(slug: str):
    """Add an observation to a project via Hindsight."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400

    try:
        from work_buddy.memory.ingest import retain_project_observation
        from work_buddy.projects.store import touch_project, upsert_project, get_project

        # Ensure project exists in registry. If not, register as a
        # dashboard-authored manual project — this is the soft-create
        # path the dashboard has always offered. Origin is 'manual'
        # (it's not a vault-detected canonical), status is 'active'
        # (the user is recording an observation, so it's clearly
        # an in-flight project).
        if not get_project(slug):
            upsert_project(
                slug, slug,
                status="active",
                origin="manual",
                author="user",
                change_summary="auto-created via dashboard observation",
            )

        touch_project(slug)
        result = retain_project_observation(
            project_slug=slug,
            content=content,
            source="dashboard",
        )
        return jsonify({"retained": result is not None, "slug": slug})
    except Exception as e:
        logger.exception("Failed to observe project %s", slug)
        return jsonify({"error": str(e)}), 500


# ── Folder mutations ───────────────────────────────────────────────


def _resolve_pid_or_404(slug: str):
    """Resolve a slug (or alias) to a project_id. Returns (pid, error_response).

    On found: ``(pid, None)``. On missing: ``(None, (jsonify, 404))``.
    """
    from work_buddy.projects.store import resolve_slug
    pid = resolve_slug(slug)
    if pid is None:
        return None, (jsonify({"error": f"Project '{slug}' not found"}), 404)
    return pid, None


@app.post("/api/projects/<slug>/folders")
def api_project_add_folder(slug: str):
    """Attach a folder to a project. Body: {path, archived?}."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    archived = bool(data.get("archived", False))
    if not path:
        return jsonify({"error": "path is required"}), 400
    pid, err = _resolve_pid_or_404(slug)
    if err:
        return err
    try:
        from work_buddy.projects.store import add_folder
        result = add_folder(pid, path, archived=archived, author="user",
                            change_summary="add folder (dashboard)")
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Failed to add folder for %s", slug)
        return jsonify({"error": str(e)}), 500


@app.delete("/api/projects/<slug>/folders")
def api_project_remove_folder(slug: str):
    """Detach a folder. Body: {path}."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    pid, err = _resolve_pid_or_404(slug)
    if err:
        return err
    try:
        from work_buddy.projects.store import remove_folder
        result = remove_folder(pid, path, author="user",
                               change_summary="remove folder (dashboard)")
        return jsonify(result)
    except Exception as e:
        logger.exception("Failed to remove folder for %s", slug)
        return jsonify({"error": str(e)}), 500


@app.post("/api/projects/<slug>/folders/archived")
def api_project_folder_set_archived(slug: str):
    """Flip a folder's archived flag. Body: {path, archived}."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    if "archived" not in data:
        return jsonify({"error": "archived flag is required"}), 400
    archived = bool(data["archived"])
    pid, err = _resolve_pid_or_404(slug)
    if err:
        return err
    try:
        from work_buddy.projects.store import set_folder_archived
        result = set_folder_archived(
            pid, path, archived, author="user",
            change_summary=("archive folder" if archived else "unarchive folder")
                + " (dashboard)",
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("Failed to set archived flag for %s", slug)
        return jsonify({"error": str(e)}), 500


@app.patch("/api/projects/<slug>/folders")
def api_project_rename_folder(slug: str):
    """Rename a folder path (inline edit). Body: {old_path, new_path}.

    Implemented as remove-then-add inside a single audit pair so the
    revision history records the intent as two related entries. The
    archived flag is preserved.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    old_path = (data.get("old_path") or "").strip()
    new_path = (data.get("new_path") or "").strip()
    if not old_path or not new_path:
        return jsonify({"error": "old_path and new_path are required"}), 400
    if old_path == new_path:
        return jsonify({"error": "old_path and new_path are identical"}), 400
    pid, err = _resolve_pid_or_404(slug)
    if err:
        return err
    try:
        from work_buddy.projects.store import (
            add_folder, list_folders, remove_folder,
        )
        # Preserve the archived flag on the old folder so the rename
        # doesn't silently flip it.
        existing = next(
            (f for f in list_folders(pid) if f["path"] == old_path),
            None,
        )
        if existing is None:
            return jsonify({"error": f"Folder {old_path!r} not attached"}), 404
        archived = bool(existing["archived"])
        remove_folder(pid, old_path, author="user",
                      change_summary="rename folder — step 1/2 (dashboard)")
        result = add_folder(pid, new_path, archived=archived, author="user",
                            change_summary="rename folder — step 2/2 (dashboard)")
        return jsonify(result)
    except Exception as e:
        logger.exception("Failed to rename folder for %s", slug)
        return jsonify({"error": str(e)}), 500


# ── Alias mutations ────────────────────────────────────────────────


@app.post("/api/projects/<slug>/aliases")
def api_project_add_alias(slug: str):
    """Attach an alias. Body: {alias}."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    alias = (data.get("alias") or "").strip()
    if not alias:
        return jsonify({"error": "alias is required"}), 400
    pid, err = _resolve_pid_or_404(slug)
    if err:
        return err
    try:
        from work_buddy.projects.store import add_alias
        result = add_alias(pid, alias, author="user",
                           change_summary="add alias (dashboard)")
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Failed to add alias for %s", slug)
        return jsonify({"error": str(e)}), 500


@app.delete("/api/projects/<slug>/aliases")
def api_project_remove_alias(slug: str):
    """Detach an alias. Body: {alias}."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    alias = (data.get("alias") or "").strip()
    if not alias:
        return jsonify({"error": "alias is required"}), 400
    pid, err = _resolve_pid_or_404(slug)
    if err:
        return err
    try:
        from work_buddy.projects.store import remove_alias
        result = remove_alias(pid, alias, author="user",
                              change_summary="remove alias (dashboard)")
        return jsonify(result)
    except Exception as e:
        logger.exception("Failed to remove alias for %s", slug)
        return jsonify({"error": str(e)}), 500


@app.patch("/api/projects/<slug>/aliases")
def api_project_rename_alias(slug: str):
    """Rename an alias (inline edit). Body: {old_alias, new_alias}.

    Same remove-then-add pattern as folder rename.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    old_alias = (data.get("old_alias") or "").strip()
    new_alias = (data.get("new_alias") or "").strip()
    if not old_alias or not new_alias:
        return jsonify({"error": "old_alias and new_alias are required"}), 400
    if old_alias == new_alias:
        return jsonify({"error": "old_alias and new_alias are identical"}), 400
    pid, err = _resolve_pid_or_404(slug)
    if err:
        return err
    try:
        from work_buddy.projects.store import add_alias, remove_alias
        remove_alias(pid, old_alias, author="user",
                     change_summary="rename alias — step 1/2 (dashboard)")
        result = add_alias(pid, new_alias, author="user",
                           change_summary="rename alias — step 2/2 (dashboard)")
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Failed to rename alias for %s", slug)
        return jsonify({"error": str(e)}), 500



@app.get("/api/projects/<slug>/memory_items")
def api_project_memory_items(slug: str):
    """Chronological list of project memories for the detail pane.

    Uses ``list_recent_project_memories`` — a plain Hindsight list
    call with alias-union tag filtering. No embedding query, no
    relevance scoring; we're showing "what's been recorded here,"
    not "what's most relevant to a prompt." This costs ~0 on the
    Hindsight side and produces a stable newest-first ordering with
    real timestamps for the UI.

    Decoupled from ``/api/projects/<slug>`` so the (still non-trivial)
    Hindsight round-trip doesn't block detail-pane render.
    """
    limit = request.args.get("limit", 50, type=int)
    try:
        from work_buddy.memory.query import list_recent_project_memories
        memory_items = list_recent_project_memories(
            project=slug, limit=limit,
        )
        # Hindsight returns newest first; ensure that's preserved even
        # if a future change reorders, and tolerate missing dates by
        # sinking blanks to the bottom.
        memory_items.sort(
            key=lambda m: m.get("date") or "", reverse=True,
        )
        return jsonify({"memory_items": memory_items, "slug": slug})
    except Exception as e:
        logger.exception("Failed to list project memories for %s", slug)
        return jsonify({"memory_items": [], "error": str(e)})


# ---------------------------------------------------------------------------
# Entity registry (Memory tab)
# ---------------------------------------------------------------------------


@app.get("/api/entities/_schema")
def api_entities_schema():
    """Return enum metadata for the entity store.

    Source of truth for any future frontend selects (source-kind
    chips, author chips). Derived from constants in
    ``work_buddy.entities.store`` so schema additions propagate without
    a frontend code change.
    """
    try:
        from work_buddy.entities.store import (
            VALID_AUTHORS, VALID_SOURCE_KINDS,
        )
        return jsonify({
            "authors": sorted(VALID_AUTHORS),
            "source_kinds": sorted(VALID_SOURCE_KINDS),
        })
    except Exception as e:
        logger.exception("Failed to read entities schema")
        return jsonify({"error": str(e)}), 500


@app.get("/api/entities")
def api_entities_list():
    """List entities, optionally filtered by a hierarchical tag.

    ``?tag=person`` matches every entity tagged ``person`` plus
    ``person/family``, ``person/colleague``, etc. ``?limit=N`` caps
    the result set.
    """
    try:
        from work_buddy.entities.store import list_entities
        tag = request.args.get("tag")
        limit = request.args.get("limit", type=int)
        entities = list_entities(tag=tag or None, limit=limit)
        return jsonify({"entities": entities})
    except Exception as e:
        logger.exception("Failed to list entities")
        return jsonify({"entities": [], "error": str(e)})


@app.get("/api/entities/tags")
def api_entity_tags():
    """Hierarchical tag nodes with aggregate usage counts.

    Powers the Memory tab's tag autocomplete. Each node carries the
    subtree-summed usage of every stored tag at or below it, so an
    intermediate segment (``person``) ranks by the combined
    popularity of its children. Declared before the ``<int:entity_id>``
    routes is unnecessary (``tags`` never matches the int converter)
    but kept adjacent to the other collection-level entity routes.
    """
    try:
        from work_buddy.entities.store import tag_autocomplete_nodes
        return jsonify({"tags": tag_autocomplete_nodes()})
    except Exception as e:
        logger.exception("Failed to read entity tag stats")
        return jsonify({"tags": [], "error": str(e)}), 500


@app.get("/api/entities/<int:entity_id>")
def api_entity_detail(entity_id: int):
    """Return a single entity with tags, aliases, recent references,
    and a total reference count."""
    try:
        from work_buddy.entities.store import (
            get_entity, list_references, count_references,
        )
        e = get_entity(entity_id)
        if not e:
            return jsonify({"error": f"Entity id={entity_id} not found"}), 404
        e["recent_references"] = list_references(entity_id, limit=10)
        e["reference_count"] = count_references(entity_id)
        return jsonify(e)
    except Exception as exc:
        logger.exception("Failed to get entity %d", entity_id)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/entities")
def api_entity_create():
    """Create a new entity. User-author write (the dashboard click
    IS the consent)."""
    try:
        from work_buddy.entities.store import create_entity
        body = request.get_json(silent=True) or {}
        canonical_name = (body.get("canonical_name") or "").strip()
        if not canonical_name:
            return jsonify({"error": "canonical_name is required"}), 400
        entity = create_entity(
            canonical_name,
            description=body.get("description") or None,
            tags=body.get("tags") or None,
            aliases=body.get("aliases") or None,
            author="user",
        )
        return jsonify(entity), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Failed to create entity")
        return jsonify({"error": str(exc)}), 500


@app.patch("/api/entities/<int:entity_id>")
def api_entity_update(entity_id: int):
    """Update an entity's canonical name and/or description.

    Empty-string description clears it. Omitted fields are left
    untouched. Tags and aliases are managed through their own routes.
    """
    try:
        from work_buddy.entities.store import update_entity
        body = request.get_json(silent=True) or {}
        kwargs = {"author": "user"}
        if "canonical_name" in body and body["canonical_name"] is not None:
            kwargs["canonical_name"] = body["canonical_name"]
        if "description" in body:
            desc = body["description"]
            kwargs["description"] = desc if desc != "" else None
        updated = update_entity(entity_id, **kwargs)
        if updated is None:
            return jsonify({"error": f"Entity id={entity_id} not found"}), 404
        return jsonify(updated)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Failed to update entity %d", entity_id)
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/entities/<int:entity_id>")
def api_entity_delete(entity_id: int):
    """Hard-delete an entity (cascades tags, aliases, references).

    The dashboard click confirms — no separate consent prompt is
    needed at this layer (the wrapper's consent gate exists for
    programmatic agent callers).
    """
    try:
        from work_buddy.entities.store import delete_entity
        if not delete_entity(entity_id, author="user"):
            return jsonify({"error": f"Entity id={entity_id} not found"}), 404
        return jsonify({"deleted": True, "entity_id": entity_id})
    except Exception as exc:
        logger.exception("Failed to delete entity %d", entity_id)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/entities/<int:entity_id>/tags")
def api_entity_set_tags(entity_id: int):
    """Replace the full tag set on an entity. Body: ``{tags: [...]}``."""
    try:
        from work_buddy.entities.store import set_tags
        body = request.get_json(silent=True) or {}
        tags = body.get("tags") or []
        if not isinstance(tags, list):
            return jsonify({"error": "tags must be a list"}), 400
        updated = set_tags(entity_id, tags, author="user")
        if updated is None:
            return jsonify({"error": f"Entity id={entity_id} not found"}), 404
        return jsonify(updated)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Failed to set tags on entity %d", entity_id)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/entities/<int:entity_id>/aliases")
def api_entity_add_alias(entity_id: int):
    """Add an alias to an entity. Body: ``{alias: \"...\"}``."""
    try:
        from work_buddy.entities.store import add_alias
        body = request.get_json(silent=True) or {}
        alias = (body.get("alias") or "").strip()
        if not alias:
            return jsonify({"error": "alias is required"}), 400
        updated = add_alias(entity_id, alias, author="user")
        if updated is None:
            return jsonify({"error": f"Entity id={entity_id} not found"}), 404
        return jsonify(updated)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Failed to add alias on entity %d", entity_id)
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/entities/<int:entity_id>/aliases")
def api_entity_remove_alias(entity_id: int):
    """Remove an alias from an entity. Body: ``{alias: \"...\"}``."""
    try:
        from work_buddy.entities.store import remove_alias
        body = request.get_json(silent=True) or {}
        alias = (body.get("alias") or "").strip()
        if not alias:
            return jsonify({"error": "alias is required"}), 400
        updated = remove_alias(entity_id, alias, author="user")
        if updated is None:
            return jsonify({"error": f"Entity id={entity_id} not found"}), 404
        return jsonify(updated)
    except Exception as exc:
        logger.exception("Failed to remove alias on entity %d", entity_id)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/entities/<int:entity_id>/references")
def api_entity_list_references(entity_id: int):
    """List references for an entity. ``?limit=N`` caps; default 50."""
    try:
        from work_buddy.entities.store import list_references, count_references
        limit = request.args.get("limit", 50, type=int)
        refs = list_references(entity_id, limit=limit)
        total = count_references(entity_id)
        return jsonify({
            "entity_id": entity_id,
            "references": refs,
            "count": len(refs),
            "total": total,
        })
    except Exception as exc:
        logger.exception("Failed to list references for entity %d", entity_id)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/entities/<int:entity_id>/references")
def api_entity_add_reference(entity_id: int):
    """Explicitly append a reference row.

    Body: ``{source_path, source_kind, snippet?}``. De-dup window
    applies (same store default as the side-effect path).
    """
    try:
        from work_buddy.entities.store import record_reference
        body = request.get_json(silent=True) or {}
        source_path = (body.get("source_path") or "").strip()
        source_kind = (body.get("source_kind") or "").strip()
        if not source_path or not source_kind:
            return jsonify({
                "error": "source_path and source_kind are required",
            }), 400
        rid = record_reference(
            entity_id=entity_id,
            source_path=source_path,
            source_kind=source_kind,
            snippet=body.get("snippet"),
        )
        if rid is None:
            return jsonify({"error": f"Entity id={entity_id} not found"}), 404
        return jsonify({
            "reference_id": rid, "entity_id": entity_id,
        }), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Failed to add reference for entity %d", entity_id)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Workflow views
# ---------------------------------------------------------------------------

@app.get("/api/workflow-views")
def api_workflow_views_list():
    """Active workflow views (browser polls this)."""
    return jsonify({"views": workflow_views.list_views()})


@app.post("/api/workflow-views")
def api_workflow_views_create():
    """Create a workflow view (called by DashboardSurface)."""
    data = request.get_json(silent=True) or {}
    view_id = data.get("view_id")
    if not view_id:
        return jsonify({"created": False, "error": "view_id required"}), 400

    view = workflow_views.create_view(
        view_id=view_id,
        title=data.get("title", "Workflow View"),
        view_type=data.get("view_type", "generic"),
        payload=data.get("payload", {}),
        body=data.get("body", ""),
        response_type=data.get("response_type", "none"),
        short_id=data.get("short_id"),
        choices=data.get("choices"),
        expandable=data.get("expandable"),
    )
    return jsonify({"created": True, "view": view})


@app.get("/api/workflow-views/<view_id>")
def api_workflow_view_get(view_id: str):
    """Full view payload."""
    view = workflow_views.get_view(view_id)
    if not view:
        return jsonify({"error": "View not found"}), 404
    return jsonify(view)


@app.post("/api/workflow-views/<view_id>/respond")
def api_workflow_view_respond(view_id: str):
    """Browser submits user response.

    After recording in the workflow view store, also bridges the response
    to the persistent notification store (if the view corresponds to a
    notification) and dismisses other surfaces.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    ok = workflow_views.submit_response(view_id, data)
    if not ok:
        return jsonify({"error": "View not found"}), 404

    # Handle consent view responses — grant + re-execute on approval
    view = workflow_views.get_view(view_id)
    view_type = view.get("view_type", "") if view else ""
    response_value = data.get("value", "")
    payload = (view.get("payload") or {}) if view else {}

    if view_type == "workflow_consent":
        wf_name = payload.get("workflow_name", "")
        user_prompt = data.get("user_prompt", "")
        if response_value == "launch" and wf_name:
            import threading
            def _do_launch():
                try:
                    from work_buddy.mcp_server.registry import get_registry
                    entry = get_registry().get(wf_name)
                    if entry:
                        _launch_workflow_session(wf_name, entry, user_prompt=user_prompt)
                except Exception as exc:
                    logger.error("Deferred workflow launch failed (%s): %s", wf_name, exc)
            threading.Thread(target=_do_launch, daemon=True).start()

    elif view_type == "capability_consent" and response_value != "deny":
        operation = payload.get("operation", "")
        cmd_name = payload.get("command_name", "")
        cmd_params = payload.get("params", {})
        default_ttl = payload.get("default_ttl", 5)
        if operation and cmd_name:
            # Grant consent
            from work_buddy.consent import grant_consent
            if response_value == "always":
                grant_consent(operation, mode="always")
            elif response_value == "temporary":
                grant_consent(operation, mode="temporary", ttl_minutes=default_ttl)
            elif response_value == "once":
                grant_consent(operation, mode="once")

            # Re-execute the command in a thread, creating a result view
            import threading
            def _do_retry():
                try:
                    with app.test_request_context(json={"command_id": f"work-buddy::{cmd_name}", "params": cmd_params}):
                        _execute_workbuddy(cmd_name, cmd_params)
                except Exception as exc:
                    logger.error("Consent retry failed (%s): %s", cmd_name, exc)
            threading.Thread(target=_do_retry, daemon=True).start()

    # Bridge to notification store — view_id == notification_id for
    # notifications delivered via DashboardSurface.
    try:
        from work_buddy.notifications.store import (
            get_notification,
            respond_to_notification,
            dispatch_callback,
        )
        from work_buddy.notifications.models import StandardResponse, NotificationStatus

        notif = get_notification(view_id)
        if notif and notif.status in (
            NotificationStatus.PENDING.value,
            NotificationStatus.DELIVERED.value,
        ):
            response = StandardResponse(
                response_type=notif.response_type,
                value=data.get("value"),
                raw=data,
                surface="dashboard",
            )
            notif = respond_to_notification(view_id, response)
            dispatch_callback(notif)

            # Dismiss other surfaces (first-response-wins)
            try:
                from work_buddy.notifications.dispatcher import SurfaceDispatcher
                dispatcher = SurfaceDispatcher.from_config()
                dispatcher.dismiss_others(
                    view_id, "dashboard", notif.delivered_surfaces,
                )
            except Exception as exc:
                logger.debug("dismiss_others from dashboard failed: %s", exc)
    except Exception as exc:
        # Non-fatal: some views aren't notifications
        logger.debug("Dashboard→notification bridge skipped for %s: %s", view_id, exc)

    return jsonify({"submitted": True})


@app.get("/api/workflow-views/<view_id>/response")
def api_workflow_view_response(view_id: str):
    """MCP surface polls for response."""
    resp = workflow_views.get_response(view_id)
    if not resp:
        return jsonify({"status": "pending"})
    return jsonify(resp)


@app.post("/api/workflow-views/<view_id>/dismiss")
def api_workflow_view_dismiss(view_id: str):
    """Dismiss a workflow view."""
    ok = workflow_views.dismiss_view(view_id)
    if not ok:
        return jsonify({"error": "View not found"}), 404
    return jsonify({"dismissed": True})


@app.post("/api/notifications/<notification_id>/acknowledge")
def api_notification_acknowledge(notification_id: str):
    """Acknowledge a notification (user saw it) and dismiss on all surfaces.

    Called by the Obsidian plugin when a non-expandable toast is clicked.
    Triggers cross-surface dismiss so the notification disappears everywhere.
    """
    data = request.get_json(silent=True) or {}
    responded_via = data.get("responded_via", "unknown")

    try:
        # Dismiss the dashboard view locally (no HTTP self-call)
        dismissed_locally = workflow_views.dismiss_view(notification_id)
        logger.info(
            "ACK %s from %s — local dismiss: %s",
            notification_id, responded_via, dismissed_locally,
        )

        # Dismiss on other surfaces. Use "dashboard" as responding_surface
        # to skip the DashboardSurface — calling it would deadlock since
        # this endpoint IS on the dashboard (single-threaded Flask).
        from work_buddy.notifications.dispatcher import SurfaceDispatcher
        dispatcher = SurfaceDispatcher.from_config()
        results = dispatcher.dismiss_others(
            notification_id,
            responding_surface="dashboard",
        )
        logger.info("ACK %s — dismiss_others results: %s", notification_id, results)
        return jsonify({"acknowledged": True, "dismissed": results})
    except Exception as exc:
        logger.debug("Acknowledge dismiss failed for %s: %s", notification_id, exc)
        return jsonify({"acknowledged": True, "dismissed": {}})


# ---------------------------------------------------------------------------
# Threads API
#
# Read endpoints + commit endpoints for the unified Threads tab. All
# routed under /api/threads/.
# ---------------------------------------------------------------------------


@app.get("/api/threads")
def api_threads_list():
    """List top-level Threads (those with no parent_id).

    Query params:
        ?show_later=1         — include Threads with future resurface_at.
        ?q=...                — substring search over search_blob.
        ?state=...            — filter by FSM state.
        ?subtype=...          — 'task' for Tasks-only.
        ?urgency=...          — 'surface_now' | 'defer' (post-query).
        ?has_cleanup=1        — only Threads where the cleanup
                                adapter is applicable (post-query).
        ?limit=N              — page size (default 100).
        ?offset=N             — skip the first N matches (default 0).
        ?show_all=1           — include non-actionable states
                                (PROPOSED, terminal, …). Default:
                                actionable wait states only.
        ?include_mid_process=1 — also include the in-flight
                                inferring/executing/monitoring
                                states. Layered on top of the
                                default "actionable only" filter so
                                the user can see "what's the agent
                                doing right now?" without dropping
                                the actionable filter entirely.

    Response shape::

        {
          "threads": [...],   # page of matches, already render-shaped
          "total":   int,     # total matches for the same filters
          "offset":  int,     # echo of the offset parameter
          "limit":   int      # echo of the limit parameter
        }

    Note: when ``urgency`` or ``has_cleanup`` is set, those filters
    are applied in Python after the SQL count, so ``total`` reflects
    the SQL-level match count (an upper bound) rather than the
    post-filtered list length. Both filters are post-query because
    they derive from ``build_render_data`` output; neither has a SQL
    column to index. The dashboard pager treats ``total`` as advisory
    when those filters are active.
    """
    try:
        from work_buddy.threads.render import build_render_data
        from work_buddy.threads.search import count_threads, search_threads
        q = request.args.get("q") or ""
        state = request.args.get("state") or None
        subtype = request.args.get("subtype") or None
        urgency = request.args.get("urgency") or None
        has_cleanup_only = request.args.get("has_cleanup") == "1"
        include_future = request.args.get("show_later") == "1"
        actionable_only = request.args.get("show_all") != "1"
        include_mid_process = request.args.get("include_mid_process") == "1"
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        filter_kwargs = dict(
            parent_id=None,
            state=state,
            subtype=subtype,
            show_later=include_future,
            actionable_only=actionable_only,
            include_mid_process=include_mid_process,
        )
        threads_models = search_threads(
            q, limit=limit, offset=offset, **filter_kwargs,
        )
        total = count_threads(q, **filter_kwargs)
        threads = []
        # Share one DB connection across the whole render batch — each
        # build_render_data otherwise opens + closes its own (×3 reads),
        # and that connection churn was the dominant cost after the
        # config-parse fix.
        from work_buddy.threads import store as _threads_store
        _conn = _threads_store.get_connection()
        try:
            for t in threads_models:
                data = build_render_data(t.thread_id, conn=_conn)
                if data is None:
                    continue
                # Post-query filters — neither has a SQL index, but the
                # cardinality at this point (post search) is small.
                if urgency and data.get("urgency") != urgency:
                    continue
                if has_cleanup_only and not data.get("can_clean_up"):
                    continue
                threads.append(data)
        finally:
            _conn.close()
        return jsonify({
            "threads": threads,
            "total": total,
            "offset": offset,
            "limit": limit,
        })
    except Exception as exc:
        logger.exception("threads list failed: %s", exc)
        return jsonify({
            "threads": [], "total": 0, "offset": 0, "limit": 0,
            "error": str(exc),
        }), 500


# A small allowlist of dashboard-triggerable capabilities. We
# intentionally don't expose the full registry — the user's
# workflow is "MCP from agent for power", "dashboard buttons for
# common nudges." Adding a capability here is a deliberate UX
# decision (each appears as a button somewhere in the UI).
_DASHBOARD_RUNNABLE_CAPABILITIES: dict[str, dict] = {
    "run_source_pipeline": {
        "description": (
            "Run a source pipeline end-to-end (Chrome triage / "
            "journal backlog) → produces a group umbrella + group "
            "sub-threads with per-cluster action proposals. Wired to "
            "the empty-state CTA on the Threads tab."
        ),
        "mutates_state": True,
    },
}


@app.post("/api/run/<capability_name>")
def api_run_capability(capability_name: str):
    """Bridge endpoint that lets the dashboard trigger a small
    allowlist of capabilities directly.

    The MCP gateway is the canonical way to invoke capabilities
    from agents; this endpoint lets the *user* trigger a known
    set of "common nudge" capabilities from dashboard buttons
    (e.g. the empty-state "Scan today's journal" CTA).

    Why an allowlist: we don't want a generic "call any capability"
    surface from the unauthenticated dashboard. Each entry is a
    deliberate UX choice.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    if capability_name not in _DASHBOARD_RUNNABLE_CAPABILITIES:
        return jsonify({
            "error": f"Capability {capability_name!r} is not exposed to the "
                     "dashboard. Use the MCP gateway (wb_run) for full "
                     "registry access, or add it to "
                     "_DASHBOARD_RUNNABLE_CAPABILITIES if it should be a "
                     "user-triggerable button.",
        }), 403
    body = request.get_json(silent=True) or {}
    try:
        from work_buddy.mcp_server.registry import get_registry
        reg = get_registry()
        cap = reg.get(capability_name)
        if cap is None:
            return jsonify({
                "error": f"Capability {capability_name!r} not in registry "
                         "(probably a dependency probe is failing). Try "
                         "the MCP gateway for diagnostics.",
            }), 503
        # Capabilities are callables in the registry — invoke
        # directly. The argument shape mirrors wb_run's params dict.
        result = cap.callable(**body)
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        logger.exception("dashboard /api/run/%s failed: %s",
                         capability_name, exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/threads/<thread_id>")
def api_thread_get(thread_id: str):
    """Fetch one Thread + its render data."""
    try:
        from work_buddy.threads.render import build_render_data
        data = build_render_data(thread_id)
        if data is None:
            return jsonify({"error": "Thread not found"}), 404
        return jsonify(data)
    except Exception as exc:
        logger.exception("thread get failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/threads/<thread_id>/sub")
def api_thread_sub_list(thread_id: str):
    """List sub-threads under a parent."""
    try:
        from work_buddy.threads.render import list_render_data
        threads = list_render_data(parent_id=thread_id, limit=200)
        return jsonify({"threads": threads, "parent_id": thread_id})
    except Exception as exc:
        logger.exception(
            "sub-thread list failed for %s: %s", thread_id, exc,
        )
        return jsonify({"threads": [], "error": str(exc)}), 500


@app.get("/api/threads/<thread_id>/events")
def api_thread_events(thread_id: str):
    """Return the full event log for a thread.

    Wave C (2026-05-03): backs the dashboard's event-log inspector
    modal. Lightweight serialization — only the fields the UI
    needs. Full event data is available via the SQLite DB if
    deeper inspection is required.
    """
    try:
        from work_buddy.threads import store
        events = store.list_events(thread_id)
        out = []
        for e in events:
            out.append({
                "id": e.id,
                "kind": e.kind,
                "actor": e.actor,
                "timestamp": e.timestamp,
                "data": e.data,
                "parent_event_id": e.parent_event_id,
                "inference_tier": e.inference_tier,
            })
        return jsonify({"thread_id": thread_id, "events": out})
    except Exception as exc:
        logger.exception(
            "thread events fetch failed for %s: %s", thread_id, exc,
        )
        return jsonify({"events": [], "error": str(exc)}), 500


_THREAD_USER_INITIATED_TRIGGERS = {
    # User clicked Approve on a thread's action chip / confirmation
    # card → fires the consent gate's "execute" trigger which calls
    # the action capability synchronously via the EXECUTING side-effect
    # handler. The click IS the consent boundary; without wrapping in
    # ``user_initiated``, capabilities re-prompt for moderate-risk
    # consent the user already gave by clicking Approve, dumping the
    # thread into AWAITING_REDIRECT with a ConsentRequired error. See
    # ``notifications/consent`` (UI-click bypass) for policy.
    "execute",
    # Other user-initiated triggers that may downstream-invoke
    # @requires_consent-gated code via side effects:
    "confirmed",
    "review_accepted",
    "provided",
    "redirected",
    "retry_cleanup",
    "accept_cleanup_failure",
}


def _post_thread_action(
    thread_id: str, *, trigger: str, data_extras=None,
):
    """Common POST handler — fires an FSM transition through engine.

    Wraps the transition in ``consent.user_initiated`` when the trigger
    is a user-click action (Approve, Confirm, Review-accept, etc.) so
    capabilities invoked via state-entry side effects don't re-prompt
    for consent the user already gave by clicking. The trigger
    allowlist lives in ``_THREAD_USER_INITIATED_TRIGGERS``; see
    ``notifications/consent`` (UI-click bypass) for policy.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    payload = request.get_json(silent=True) or {}
    try:
        from work_buddy.threads import engine
        from work_buddy.consent import user_initiated
        merged = dict(payload)
        if data_extras:
            merged.update(data_extras)
        if trigger in _THREAD_USER_INITIATED_TRIGGERS:
            with user_initiated(f"dashboard.thread.{trigger}"):
                result = engine.transition(
                    thread_id, trigger, data=merged, fire_side_effects=True,
                )
        else:
            result = engine.transition(
                thread_id, trigger, data=merged, fire_side_effects=True,
            )
        return jsonify({
            "ok": True,
            "thread_id": thread_id,
            "prev_state": result.prev_state.value,
            "next_state": result.next_state.value,
        })
    except engine.ThreadNotFound:
        return jsonify({"error": "Thread not found"}), 404
    except engine.InvalidTransition as e:
        return jsonify({"error": str(e)}), 400
    except Exception as exc:
        logger.exception("thread action failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


_ACCEPT_TRIGGER_BY_STATE = {
    # Confirmation states → confirmed
    "awaiting_intent_confirmation": "confirmed",
    "awaiting_context_confirmation": "confirmed",
    # Consent state → execute (action gate approval)
    "awaiting_confirmation": "execute",
    # Clarification states → provided
    "awaiting_intent_clarification": "provided",
    "awaiting_context_clarification": "provided",
    "awaiting_action_clarification": "provided",
    # Review state → review_accepted
    "awaiting_review": "review_accepted",
    # Redirect states → redirected (when user submits feedback)
    "awaiting_redirect": "redirected",
}


def _write_action_proposal_event(
    thread_id: str, thread, *, payload: dict, confidence: float,
    cleared: bool = False,
) -> None:
    """Append a synthetic user-override ``action_inferred`` event.

    Shared by ``set_action_proposal`` (chip override / clear) and the
    accept path (commit the user's filled / switched action before
    execution). The caller bumps ``parent_event_id`` and handles any FSM
    promotion; this only writes the event using the thread's current
    ``parent_event_id``.
    """
    from work_buddy.threads import store
    from work_buddy.threads.events import (
        ACTOR_USER, KIND_ACTION_INFERRED, ThreadEvent,
    )
    data = {
        "target": "action",
        "payload": payload,
        "confidence": float(confidence),
        "synthetic": True,
        "from_user_override": True,
    }
    if cleared:
        data["cleared"] = True
    store.append_event(ThreadEvent(
        thread_id=thread_id,
        kind=KIND_ACTION_INFERRED,
        actor=ACTOR_USER,
        data=data,
        parent_event_id=thread.parent_event_id,
    ))


def _current_action_payload(thread_id: str) -> dict | None:
    """The latest non-cleared ``action_inferred`` payload for a thread,
    or ``None``. Mirrors what the executor reads at dispatch."""
    from work_buddy.threads import store
    from work_buddy.threads.events import KIND_ACTION_INFERRED
    for e in reversed(store.list_events(thread_id=thread_id)):
        if e.kind != KIND_ACTION_INFERRED:
            continue
        if e.data.get("cleared"):
            continue
        payload = e.data.get("payload") or {}
        if payload.get("name"):
            return payload
    return None


def _apply_action_edits_for_execute(thread_id: str, thread) -> None:
    """Fold user-supplied action edits into a fresh ``action_inferred``
    event before an Approve commits to execution, so the executor
    dispatches the action the user actually approved.

    Honors two body shapes:
      - ``action``: ``{capability_name, parameters}`` — the resolved
        action to run (a switch, or the current action with filled
        params). The canonical shape the resolution UI sends.
      - ``action_overrides``: ``{action_id: {param: value}}`` — per-field
        edits from the right-pane editor, merged into the current
        proposal's parameters.

    No-op (a plain Approve) when neither is present. When the action is
    unchanged, the current proposal's risk metadata is preserved so a
    param edit can't silently downgrade the consent posture.
    """
    from work_buddy.threads import store
    body = request.get_json(silent=True) or {}
    override = body.get("action") if isinstance(body.get("action"), dict) else None
    overrides_map = body.get("action_overrides")
    current = _current_action_payload(thread_id)

    payload: dict | None = None
    if override and override.get("capability_name"):
        name = str(override["capability_name"])
        params = dict(override.get("parameters") or {})
        if current and current.get("name") == name:
            payload = {**current, "name": name, "parameters": params}
        else:
            payload = {
                "kind": "standard",
                "name": name,
                "parameters": params,
                "rationale": override.get("rationale")
                or (current or {}).get("rationale"),
                "irreversibility": "low",
                "regret_potential": "low",
                "risk_amplifier": False,
            }
    elif isinstance(overrides_map, dict) and overrides_map and current:
        merged = dict(current.get("parameters") or {})
        for _aid, kv in overrides_map.items():
            if isinstance(kv, dict):
                merged.update(kv)
        if merged != (current.get("parameters") or {}):
            payload = {**current, "parameters": merged}

    if payload is None:
        return  # plain Approve; nothing to fold in

    _write_action_proposal_event(
        thread_id, thread, payload=payload, confidence=1.0,
    )
    store.update_thread_state(
        thread_id, parent_event_id=store.latest_event_id(thread_id),
    )


@app.post("/api/threads/<thread_id>/accept")
def api_thread_accept(thread_id: str):
    """Smart accept: dispatches the right trigger based on FSM state.

    Confirmation → confirmed. Consent → execute. Clarification →
    provided. Review → review_accepted. Redirect → redirected.
    UX.md §4.2 + §5.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    try:
        from work_buddy.threads import store
        thread = store.get_thread(thread_id)
        if thread is None:
            return jsonify({"error": "Thread not found"}), 404
        trigger = _ACCEPT_TRIGGER_BY_STATE.get(thread.fsm_state.value)
        if trigger is None:
            return jsonify({
                "error": f"Accept not valid in state {thread.fsm_state.value!r}",
            }), 400
        # Approve commits the user's resolved action: fold any filled /
        # switched params into a fresh action_inferred before execution.
        if trigger == "execute":
            _apply_action_edits_for_execute(thread_id, thread)
        return _post_thread_action(thread_id, trigger=trigger)
    except Exception as exc:
        logger.exception("thread accept failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/retry-cleanup")
def api_thread_retry_cleanup(thread_id: str):
    """Retry a failed cleanup. UX.md §6.5."""
    return _post_thread_action(thread_id, trigger="retry_cleanup")


@app.post("/api/threads/<thread_id>/context/<item_id>/migrate")
def api_thread_context_migrate(thread_id: str, item_id: str):
    """Move a context item from one Thread to another. UX.md §9.

    Body: {"to_thread_id": "th-abc"}.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    to_thread_id = body.get("to_thread_id")
    if not to_thread_id:
        return jsonify({"error": "missing to_thread_id"}), 400
    try:
        from work_buddy.threads.migration_context import (
            ContextMigrationError,
            migrate_context,
        )
        mig_id = migrate_context(
            item_id=item_id,
            from_thread_id=thread_id,
            to_thread_id=to_thread_id,
        )
        return jsonify({
            "ok": True,
            "migration_id": mig_id,
            "from_thread_id": thread_id,
            "to_thread_id": to_thread_id,
            "item_id": item_id,
        })
    except ContextMigrationError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as exc:
        logger.exception(
            "thread context migrate failed for %s → %s: %s",
            thread_id, to_thread_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/accept-cleanup-failure")
def api_thread_accept_cleanup_failure(thread_id: str):
    """Accept a failed cleanup; thread → done. UX.md §6.5."""
    return _post_thread_action(thread_id, trigger="accept_cleanup_failure")


@app.post("/api/threads/<thread_id>/dismiss")
def api_thread_dismiss(thread_id: str):
    """Trash the Thread. Transition to DISMISSED."""
    return _post_thread_action(thread_id, trigger="dismissed_by_user")


@app.post("/api/threads/<thread_id>/redirect")
def api_thread_redirect(thread_id: str):
    """Re-direct: push back to inference with feedback. UX.md §5.3."""
    return _post_thread_action(thread_id, trigger="redirected")


@app.post("/api/threads/<thread_id>/redirect_action")
def api_thread_redirect_action(thread_id: str):
    """Hand an action back to the agent for refinement, on any thread.

    Body (all optional)::

        {
          "feedback": "<free-text steering note>",
          "params": {...},              # seeds: fields the user filled
          "target_action": "<capability_name>"   # the switched-to action
        }

    Re-infers JUST the action layer, without rerunning intent / context
    inference. The prior ``action_inferred`` event stays in history;
    ``render._latest()`` surfaces the newest as the active proposal.

    Path: AWAITING_CONFIRMATION → AWAITING_INFERENCE (TRIG_REDIRECTED,
    data carries ``target='action'`` so the inference worker enqueues
    only the action target). Feedback is optional — a bare redirect is a
    valid "try this again". The feedback, seed params, and target action
    are recorded on a ``KIND_ACTION_REDIRECTED`` event; the inference
    runner reads them so it keeps what the user filled and completes only
    the gaps.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    payload = request.get_json(silent=True) or {}
    feedback = (payload.get("feedback") or "").strip()
    seed_params = payload.get("params")
    seed_params = dict(seed_params) if isinstance(seed_params, dict) else {}
    target_action = (payload.get("target_action") or "").strip() or None
    try:
        from work_buddy.threads import engine, store
        from work_buddy.threads.events import (
            ACTOR_USER,
            KIND_ACTION_INFERRED,
            KIND_ACTION_REDIRECTED,
            ThreadEvent,
        )

        thread = store.get_thread(thread_id)
        if thread is None:
            return jsonify({"error": "Thread not found"}), 404

        # Find the action_inferred event being superseded (newest, if any)
        events = store.list_events(thread_id, kinds=[KIND_ACTION_INFERRED])
        superseded_event_id = events[-1].id if events else None

        # Record the user redirect BEFORE the transition, so it's in the
        # log when the inference worker builds the prompt. Seeds (the
        # params the user filled) + target_action (the switched-to
        # capability) let re-inference keep what the user provided and
        # fill only the missing required fields.
        redirect_data = {
            "feedback": feedback,
            "superseded_event_id": superseded_event_id,
        }
        if seed_params:
            redirect_data["seed_params"] = seed_params
        if target_action:
            redirect_data["target_action"] = target_action
        store.append_event(ThreadEvent(
            thread_id=thread_id,
            kind=KIND_ACTION_REDIRECTED,
            actor=ACTOR_USER,
            data=redirect_data,
        ))

        # ``append_event`` writes the event row but does NOT bump the
        # ``threads.parent_event_id`` cache. ``engine.transition`` reads
        # that cache for the optimistic-lock target, so without an
        # explicit refresh it would compare a stale ID against the
        # newly-landed feedback event and reject with
        # OptimisticLockConflict. Pass the fresh latest_event_id
        # explicitly — same pattern as decompose.cascade_terminal_to_parent.
        fresh_parent = store.latest_event_id(thread_id)

        # Transition: TRIG_REDIRECTED + target='action' so the
        # AWAITING_INFERENCE state-entry handler enqueues ONLY the
        # action target (no walk back through intent/context).
        result = engine.transition(
            thread_id, "redirected",
            data={
                "target": "action",
                "redirect_feedback": feedback,
            },
            parent_event_id=fresh_parent,
            fire_side_effects=True,
        )
        return jsonify({
            "ok": True,
            "thread_id": thread_id,
            "prev_state": result.prev_state.value,
            "next_state": result.next_state.value,
            "superseded_event_id": superseded_event_id,
        })
    except engine.InvalidTransition as e:
        return jsonify({"error": str(e)}), 400
    except Exception as exc:
        logger.exception(
            "redirect_action failed for %s: %s", thread_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/cleanup")
def api_thread_cleanup(thread_id: str):
    """Clean Up: invoke registered cleanup adapter, mutate the source.

    Stage 4.3: transitions FSM to CLEANING_UP. Stage 4.4 wires the
    adapter call + fires cleanup_succeeded / cleanup_failed.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    try:
        from work_buddy.threads import cleanup as _cleanup_mod
        from work_buddy.threads import engine, store
        thread = store.get_thread(thread_id)
        if thread is None:
            return jsonify({"error": "Thread not found"}), 404
        if not _cleanup_mod.can_clean_up(thread):
            return jsonify({
                "error": "no cleanup adapter registered for this Thread's source",
            }), 400
        # Transition to CLEANING_UP — Stage 4.4 will register a state-
        # entry handler that runs the adapter and fires the result trigger.
        engine.transition(
            thread_id, "cleanup_requested", fire_side_effects=True,
        )
        return jsonify({"ok": True, "thread_id": thread_id, "state": "cleaning_up"})
    except Exception as exc:
        logger.exception("thread cleanup failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/later")
def api_thread_later(thread_id: str):
    """Defer: set resurface_at to now + duration. UX.md §13.

    Body (optional): {"hours": 6}  default 6h.
    Stage 4.10 polishes with the hover popup; this endpoint ships
    in 4.3 because the button is on every card.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    try:
        import json
        from datetime import datetime, timedelta, timezone
        from work_buddy.threads import store
        from work_buddy.threads.events import KIND_LATER, ThreadEvent
        body = request.get_json(silent=True) or {}
        hours = float(body.get("hours") or 6.0)
        thread = store.get_thread(thread_id)
        if thread is None:
            return jsonify({"error": "Thread not found"}), 404
        when = datetime.now(timezone.utc) + timedelta(hours=hours)
        resurface_iso = when.isoformat()
        store.update_thread_state(
            thread_id,
            resurface_at=resurface_iso,
        )
        store.append_event(ThreadEvent(
            thread_id=thread_id,
            kind=KIND_LATER,
            actor="user",
            data={"hours": hours, "resurface_at": resurface_iso},
            parent_event_id=store.latest_event_id(thread_id),
        ))
        return jsonify({
            "ok": True,
            "thread_id": thread_id,
            "resurface_at": resurface_iso,
            "hours": hours,
        })
    except Exception as exc:
        logger.exception("thread later failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<src_id>/move_item")
def api_thread_move_item(src_id: str):
    """Move a single ContextItem from one group child to
    another sibling group child.

    Body: ``{"item_id": "<context_item_id>", "dest_thread_id":
    "<sibling_thread_id>"}``

    Both ``src_id`` and ``dest_thread_id`` must share the same
    umbrella parent (``parent_id``), and that umbrella must have
    ``parent_relationship == 'group'``. Enforced by
    ``threads.group.move_item``.

    Returns ``{"migration_id": str, "item": {ContextItemDict}}`` on
    success, 422 on validation failure (cross-umbrella, item not
    present, etc.), 500 on server error.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    item_id = body.get("item_id")
    dest_thread_id = body.get("dest_thread_id")
    if not item_id or not dest_thread_id:
        return jsonify(
            {"error": "item_id and dest_thread_id required"},
        ), 400
    try:
        from work_buddy.threads.group import GroupRefused, move_item
        result = move_item(item_id, src_id, dest_thread_id)
        return jsonify(result)
    except GroupRefused as e:
        return jsonify({"error": str(e), "reason": "validation"}), 422
    except Exception as exc:
        logger.exception(
            "thread move_item failed for %s/%s -> %s: %s",
            src_id, item_id, dest_thread_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<umbrella_id>/spawn_empty_group")
def api_thread_spawn_empty_group(umbrella_id: str):
    """Add an empty group child under ``umbrella_id``.

    Drives the "+ New group" drop zone in the column UI — drop
    selected items onto the zone → the frontend posts here to spawn
    an empty child, then immediately fires :func:`move_item` for
    each selected item.

    Body (optional): ``{"label": "New group"}``.

    Returns ``{"new_thread_id": str, "umbrella_id": str, "label":
    str}``.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    label = (body.get("label") or "").strip() or "New group"
    try:
        from work_buddy.threads.group import GroupRefused, spawn_empty_group
        new_id = spawn_empty_group(umbrella_id, label)
        return jsonify({
            "new_thread_id": new_id,
            "umbrella_id": umbrella_id,
            "label": label,
        })
    except GroupRefused as e:
        return jsonify({"error": str(e), "reason": "validation"}), 422
    except Exception as exc:
        logger.exception(
            "thread spawn_empty_group failed for %s: %s", umbrella_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/set_action_proposal")
def api_thread_set_action_proposal(thread_id: str):
    """Override or clear the per-thread proposed action.

    Driven by the dashboard's column-header action chip dropdown:
    when the user picks a different action than the LLM proposed,
    the frontend POSTs here with the new ``capability_name`` (or
    null to clear). We append a fresh ``action_inferred`` event
    flagged ``synthetic=True, from_user_override=True``; the
    standard FSM dispatch picks it up at approval time.

    Body::

        {
          "capability_name": "<name>",        # null to clear
          "parameters": {...},                # optional, default {}
          "rationale": "<text>",              # optional
          "confidence": 1.0                   # default 1.0 for user overrides
        }
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    capability_name = body.get("capability_name")
    try:
        from work_buddy.threads import store
        from work_buddy.threads.events import ThreadEvent
        thread = store.get_thread(thread_id)
        if thread is None:
            return jsonify({"error": "thread not found"}), 404

        if capability_name is None:
            # Clear: record an event with payload.name = "" and
            # synthetic.cleared = True. The card renderer treats
            # empty name as "no proposed action" without needing a
            # separate event kind.
            _write_action_proposal_event(
                thread_id, thread,
                payload={"kind": "standard", "name": ""},
                confidence=0.0, cleared=True,
            )
        else:
            _write_action_proposal_event(
                thread_id, thread,
                payload={
                    "kind": "standard",
                    "name": str(capability_name),
                    "parameters": dict(body.get("parameters") or {}),
                    "rationale": body.get("rationale"),
                    "irreversibility": "low",
                    "regret_potential": "low",
                    "risk_amplifier": False,
                },
                confidence=float(body.get("confidence") or 1.0),
            )
        # If the thread was stuck in AWAITING_INFERENCE (e.g. a
        # pipeline-spawned child whose worker hasn't picked it up
        # yet), the user picking an action via the chip is itself a
        # decision — promote to the action gate so Accept becomes
        # valid. Threads already at AWAITING_CONFIRMATION (or any
        # other state) are left as-is; the new event flows through
        # the standard FSM dispatch path.
        from work_buddy.threads.enums import FSMState as _FSMState
        from work_buddy.threads.events import (
            ACTOR_FSM_ENGINE, KIND_STATE_TRANSITION,
        )
        promote = (
            capability_name is not None
            and thread.fsm_state == _FSMState.AWAITING_INFERENCE
        )
        if promote:
            store.update_thread_state(
                thread_id,
                fsm_state=_FSMState.AWAITING_CONFIRMATION.value,
                parent_event_id=store.latest_event_id(thread_id),
            )
            store.append_event(ThreadEvent(
                thread_id=thread_id,
                kind=KIND_STATE_TRANSITION,
                actor=ACTOR_FSM_ENGINE,
                data={
                    "from": _FSMState.AWAITING_INFERENCE.value,
                    "to": _FSMState.AWAITING_CONFIRMATION.value,
                    "reason": "user_picked_action_via_chip",
                },
                parent_event_id=store.latest_event_id(thread_id),
            ))
        store.update_thread_state(
            thread_id,
            parent_event_id=store.latest_event_id(thread_id),
        )
        return jsonify({
            "thread_id": thread_id,
            "capability_name": capability_name,
        })
    except Exception as exc:
        logger.exception(
            "thread set_action_proposal failed for %s: %s", thread_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/delete_group_subthread")
def api_thread_delete_group_subthread(thread_id: str):
    """Dismiss a group child via the column-header X
    button. Empty children stay visible by default — user explicitly
    deletes them once they're sure.

    Returns ``{"dismissed": <thread_id>, "umbrella_id": <umbrella_id>}``.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    try:
        from work_buddy.threads.group import (
            GroupRefused, delete_group_subthread,
        )
        result = delete_group_subthread(thread_id)
        return jsonify(result)
    except GroupRefused as e:
        return jsonify({"error": str(e), "reason": "validation"}), 422
    except Exception as exc:
        logger.exception(
            "thread delete_group_subthread failed for %s: %s", thread_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<umbrella_id>/approve_all")
def api_thread_approve_all(umbrella_id: str):
    """Cascade Accept to every non-terminal child of the
    umbrella. Children execute their proposed actions.

    Continues on per-child failure — returns ``{approved: [...],
    failed: [{child_thread_id, error}, ...], skipped_terminal: [...]}``
    so the frontend can surface "Approved N/M; K failed" once.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    actor = body.get("actor") or "user"
    try:
        from work_buddy.threads.group import (
            GroupRefused, cascade_approve_umbrella,
        )
        result = cascade_approve_umbrella(umbrella_id, actor=actor)
        return jsonify(result)
    except GroupRefused as e:
        return jsonify({"error": str(e), "reason": "validation"}), 422
    except Exception as exc:
        logger.exception(
            "thread approve_all failed for %s: %s", umbrella_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


# Suggestions endpoint — stubbed for now. The earlier
# thread-granularity implementation isn't directly portable to the
# umbrella+items model. Returning an empty list keeps the panel
# rendering cleanly until cross-group suggestions are designed.
@app.get("/api/threads/<thread_id>/group_suggestions")
def api_thread_group_suggestions(thread_id: str):
    """Stub — cross-group item-level suggestions are not
    yet implemented in the new model."""
    return jsonify({"suggestions": []})


def _linearize_children_for_display(children: list) -> list:
    """Reorder a column's children so visually-similar items sit
    adjacent (Stage 5 polish).

    Uses :func:`work_buddy.journal_backlog.clustering.linearize_threads`
    — Jaccard tag-similarity seriation. Each child's "tags" are the
    union of ``namespace_tags`` and inline ``#tag`` tokens extracted
    from the inciting summary's description / label.

    Returns a new list with the same items in linearized order. On
    any failure (missing optional dependency, etc.) returns the
    input unchanged — display order is a polish concern, not a
    correctness one.
    """
    if not children or len(children) < 3:
        # 1-2 items: nothing to linearize.
        return children
    try:
        from work_buddy.journal_backlog.clustering import linearize_threads
        from work_buddy.journal_backlog.similarity import extract_inline_tags
    except Exception:
        return children
    entries = []
    for ch in children:
        tags: list[str] = list(ch.get("namespace_tags") or [])
        # Pull inline tags from any text field that might carry them.
        inciting = ch.get("inciting_event_summary") or {}
        text_blob = " ".join(filter(None, [
            inciting.get("description"),
            inciting.get("label"),
            inciting.get("title"),
            ch.get("title"),
        ]))
        try:
            tags.extend(extract_inline_tags(text_blob))
        except Exception:
            pass
        # Dedupe lower-cased.
        seen: set[str] = set()
        clean: list[str] = []
        for t in tags:
            tl = (t or "").lower()
            if tl and tl not in seen:
                seen.add(tl)
                clean.append(tl)
        entries.append({
            "id": ch.get("thread_id"),
            "tags": clean,
        })
    try:
        clusters = linearize_threads(entries, break_threshold=0.15)
    except Exception:
        return children
    by_id = {ch.get("thread_id"): ch for ch in children}
    out: list = []
    for cluster in clusters:
        for entry in cluster:
            ch = by_id.get(entry["id"])
            if ch is not None:
                out.append(ch)
    # Fall through anything missing in case of bug.
    seen_ids = {ch.get("thread_id") for ch in out}
    for ch in children:
        if ch.get("thread_id") not in seen_ids:
            out.append(ch)
    return out


@app.get("/api/threads/<umbrella_id>/groups")
def api_thread_groups(umbrella_id: str):
    """List the children of a group umbrella, with each child's
    ``context_items`` rendered inline AND the umbrella's per-source
    ``action_options`` so the dashboard can paint the multi-column
    grid + the per-column action chip in one fetch.

    Returns::

        {
          "umbrella_id": str,
          "source": str | None,    # umbrella's source pipeline name
          "groups": [
            {<standard thread render dict>,
             "context_items": [{ContextItemDict}, ...]},
            ...
          ],
          "action_options": [
            {capability_name, label, description, cardinality, icon},
            ...
          ]
        }

    ``action_options`` covers BOTH per-source (Chrome / journal / …)
    AND universal actions (dismiss / defer / rename) so the chip
    dropdown can show the union without the frontend needing to
    figure out which library applies.

    404 if the thread isn't a group umbrella.
    """
    try:
        from work_buddy.threads import store
        from work_buddy.threads.render import build_render_data
        umbrella = store.get_thread(umbrella_id)
        if umbrella is None:
            return jsonify({"error": "umbrella not found"}), 404
        if umbrella.parent_relationship != "group":
            return jsonify({
                "error": "not a group umbrella",
                "reason": "wrong_relationship",
            }), 404
        children = store.list_threads(parent_id=umbrella_id)
        groups_out: list[dict] = []
        for c in children:
            rendered = build_render_data(c.thread_id)
            if rendered is None:
                continue
            groups_out.append(rendered)

        action_options, source_name = _resolve_action_library_for_thread(
            umbrella,
        )

        return jsonify({
            "umbrella_id": umbrella_id,
            "source": source_name,
            "groups": groups_out,
            "action_options": action_options,
        })
    except Exception as exc:
        logger.exception(
            "thread groups failed for %s: %s", umbrella_id, exc,
        )
        return jsonify({"groups": [], "error": str(exc)}), 500


def _project_param_schema(raw) -> list[dict[str, Any]]:
    """Flatten a ``{name: {type, description, required}}`` params schema
    into an ordered ``[{name, type, description, required}]`` list for the
    frontend."""
    out: list[dict[str, Any]] = []
    for pname, pinfo in (raw or {}).items():
        if not isinstance(pinfo, dict):
            continue
        out.append({
            "name": pname,
            "type": pinfo.get("type", ""),
            "description": pinfo.get("description", ""),
            "required": bool(pinfo.get("required", False)),
        })
    return out


def _attach_param_schemas(descriptors: list[dict]) -> list[dict]:
    """Add each action descriptor's parameter schema (from the registry)
    so the resolution UI can render blank required fields and gate Approve
    on the required ones. Unknown capabilities get an empty schema.

    Runtime-bound params (``thread_id``, ``tab_ids``) are excluded — the
    executor injects them, so the user must not see them as fields.
    """
    from work_buddy.mcp_server.registry import get_registry
    from work_buddy.threads.execution_runner import RUNTIME_BOUND_PARAMS
    reg = get_registry()
    for d in descriptors:
        entry = reg.get(d.get("capability_name"))
        schema = _project_param_schema(
            getattr(entry, "parameters", None) if entry is not None else None
        )
        d["parameters"] = [
            p for p in schema if p["name"] not in RUNTIME_BOUND_PARAMS
        ]
    return descriptors


def _resolve_action_library_for_thread(thread) -> tuple[list[dict], str | None]:
    """Resolve a thread's source name and merge per-source + universal
    actions into a single ordered descriptor list for the frontend.

    Works on any thread, not just group umbrellas: group children carry
    their own ``source_pipeline`` in ``inciting_event_summary``, so a
    child resolves to its real pipeline library directly.

    Returns ``(descriptor_list, source_name)``. Empty list if the
    thread's source isn't registered or pipelines/universal-actions
    fail to import (defensive).
    """
    inciting = thread.inciting_event_summary or {}
    source_name = (
        inciting.get("source_pipeline")
        or inciting.get("source")
    )
    try:
        from work_buddy.pipelines.capability import PIPELINES
        from work_buddy.pipelines.universal_actions import (
            UNIVERSAL_ACTION_LIBRARY,
        )
    except Exception as e:
        logger.warning(
            "action library: pipeline imports failed: %s", e,
        )
        return [], source_name

    factory = PIPELINES.get(source_name)
    if factory is None:
        # Unknown source — surface universal actions only so the chip
        # still works for threads without a registered pipeline.
        return UNIVERSAL_ACTION_LIBRARY.to_list(), source_name

    try:
        pipeline_lib = factory().action_library
    except Exception as e:
        logger.warning(
            "action library: %s.action_library failed: %s",
            source_name, e,
        )
        return UNIVERSAL_ACTION_LIBRARY.to_list(), source_name

    merged = UNIVERSAL_ACTION_LIBRARY.merged_with(pipeline_lib)
    return merged.to_list(), source_name


@app.get("/api/threads/<thread_id>/action_options")
def api_thread_action_options(thread_id: str):
    """Return the action library (per-source + universal) for a single
    thread, regardless of whether it is a group umbrella.

    The ``/groups`` endpoint only serves group umbrellas, so a child
    thread opened directly has no way to populate the action-switcher
    options. This endpoint resolves the library for any thread from its
    own ``inciting_event_summary`` source, letting the inner thread
    pane fill the switcher without fetching the parent's group grid.

    Returns ``{"thread_id", "source", "action_options"}``. Pure read.
    """
    try:
        from work_buddy.threads import store
        thread = store.get_thread(thread_id)
        if thread is None:
            return jsonify({"error": "thread not found"}), 404
        action_options, source_name = _resolve_action_library_for_thread(
            thread,
        )
        _attach_param_schemas(action_options)
        return jsonify({
            "thread_id": thread_id,
            "source": source_name,
            "action_options": action_options,
        })
    except Exception as exc:
        logger.exception(
            "thread action_options failed for %s: %s", thread_id, exc,
        )
        return jsonify({"action_options": [], "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Conversation API (renamed from Thread chat to free the Thread name)
# ---------------------------------------------------------------------------


@app.get("/api/conversations")
def api_conversations_list():
    """List conversations, optionally filtered by status."""
    status = request.args.get("status")
    try:
        from work_buddy.conversations.store import list_conversations
        conversations = list_conversations(status=status)
        return jsonify({"conversations": conversations})
    except Exception as exc:
        logger.error("Conversation list failed: %s", exc)
        return jsonify({"conversations": [], "error": str(exc)})


@app.get("/api/conversations/<conversation_id>")
def api_conversation_get(conversation_id: str):
    """Get a conversation with all messages in chronological order.

    Includes ``conversation.agent_alive``: ``true``/``false`` if a
    driving agent pid was registered for this conversation, ``null``
    if no agent was ever registered (e.g. user-driven conversations
    without a spawned driver). The chat sidebar uses this to drive
    the typing indicator and surface a clear "agent stopped" state
    when the process exits (budget cap, crash, kill).
    """
    try:
        from work_buddy.conversations.store import get_conversation_with_messages
        from work_buddy.conversations.agents import is_alive
        result = get_conversation_with_messages(conversation_id)
        if result is None:
            return jsonify({"error": "Conversation not found"}), 404
        if isinstance(result, dict) and isinstance(result.get("conversation"), dict):
            result["conversation"]["agent_alive"] = is_alive(conversation_id)
        return jsonify(result)
    except Exception as exc:
        logger.error("Conversation get failed for %s: %s", conversation_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/conversations/<conversation_id>/respond")
def api_conversation_respond(conversation_id: str):
    """User sends a message or responds to a pending question.

    Expects: {"value": "user's text"}

    If there's a pending question, answers it. Otherwise adds a general
    user message to the conversation.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    value = data.get("value", "")
    if not value and value is not False:
        return jsonify({"error": "Missing 'value' in request body"}), 400

    try:
        from work_buddy.conversations.store import (
            respond_to_conversation,
            add_message,
        )
        # Try to answer a pending question first
        msg = respond_to_conversation(conversation_id, str(value))
        if msg is not None:
            return jsonify({"responded": True, "message_id": msg.message_id})
        # No pending question — add as a general user message
        msg = add_message(conversation_id, "user", str(value))
        if msg is None:
            return jsonify({"error": "Conversation not found or closed"}), 404
        return jsonify({"sent": True, "message_id": msg.message_id})
    except Exception as exc:
        logger.error(
            "Conversation respond failed for %s: %s", conversation_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/conversations/<conversation_id>/close")
def api_conversation_close(conversation_id: str):
    """Close a conversation."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    try:
        from work_buddy.conversations.store import close_conversation
        from work_buddy.conversations.agents import unregister as unregister_agent
        ok = close_conversation(conversation_id)
        # Drop the agent pid registration even on close failure — if
        # the conversation is gone, the registration is orphaned.
        unregister_agent(conversation_id)
        if not ok:
            return jsonify({"error": "Conversation not found"}), 404
        return jsonify({"closed": True})
    except Exception as exc:
        logger.error(
            "Conversation close failed for %s: %s", conversation_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Deep-link navigation (Obsidian → dashboard tab focus)
# ---------------------------------------------------------------------------

_pending_deeplink: dict | None = None


@app.post("/api/deeplink")
def api_deeplink_set():
    """Set a pending deep-link. The dashboard poll cycle will navigate to it."""
    global _pending_deeplink
    data = request.get_json(silent=True) or {}
    view_id = data.get("view_id")
    if not view_id:
        return jsonify({"error": "view_id required"}), 400
    _pending_deeplink = {"view_id": view_id}
    logger.info("Deep-link set: %s", view_id)
    return jsonify({"set": True})


@app.get("/api/deeplink")
def api_deeplink_get():
    """Check for a pending deep-link. Clears it after reading (one-shot)."""
    global _pending_deeplink
    if _pending_deeplink is None:
        return jsonify({"pending": False})
    result = _pending_deeplink
    _pending_deeplink = None
    return jsonify({"pending": True, **result})


@app.post("/api/open-dashboard")
def api_open_dashboard():
    """Focus or create a dashboard browser tab via the Chrome extension.

    Called by the Obsidian plugin when the user clicks "Open in Dashboard".
    Uses the Chrome extension's focus_or_create_tab mutation to reuse
    an existing tab or create a new one, with deep-link navigation.
    """
    data = request.get_json(silent=True) or {}
    view_id = data.get("view_id", "dashboard")
    target_hash = f"#view/{view_id}" if view_id and view_id != "dashboard" else ""

    try:
        from work_buddy.collectors.chrome_collector import focus_or_create_tab
        result = focus_or_create_tab(
            url="http://127.0.0.1:5127",
            target_hash=target_hash,
            timeout_seconds=10,
        )
        if result is None:
            return jsonify({"error": "Chrome extension did not respond"}), 504
        return jsonify({"opened": True, **result})
    except Exception as exc:
        logger.warning("open-dashboard failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Notification log
# ---------------------------------------------------------------------------

@app.post("/api/notification-log")
def api_notification_log_add():
    """Log a notification/request event for dashboard display."""
    data = request.get_json(silent=True) or {}
    workflow_views.log_notification(data)
    return jsonify({"ok": True})


@app.get("/api/notification-log")
def api_notification_log_list():
    """Get recent notification events."""
    return jsonify({"entries": workflow_views.get_notification_log()})


# ---------------------------------------------------------------------------
# Inline commands (Obsidian right-click menu + #wb/cmd/* tag triggers)
# ---------------------------------------------------------------------------

@app.get("/inline/menu-manifest")
def api_inline_menu_manifest():
    """Manifest of inline commands that expose a right-click menu entry."""
    try:
        from work_buddy.inline import registry as _ireg
        commands = []
        for c in _ireg.list_for_surface("menu"):
            commands.append({
                "command": c.name,
                "label": c.menu_label or c.name,
                "description": c.description,
                "icon": getattr(c, "icon", None),
            })
        logger.debug("inline menu-manifest: %d commands", len(commands))
        return jsonify({"commands": commands})
    except Exception as exc:
        logger.exception("inline menu-manifest failed")
        return jsonify({"error": str(exc)}), 500


@app.post("/inline/invoke")
def api_inline_invoke():
    """Dispatch an inline command from the Obsidian plugin.

    Body: {command, surface, payload} where surface is 'menu' or 'tag'
    and payload matches what inline.context.build_context expects.
    """
    data = request.get_json(silent=True) or {}
    command = data.get("command", "")
    surface = data.get("surface", "")
    payload = data.get("payload") or {}
    if not command or not surface:
        return jsonify({"error": "command and surface are required"}), 400

    try:
        from work_buddy.inline import dispatcher as _disp
        merged = {**payload, "command": command}
        logger.info("inline invoke: %s via %s", command, surface)
        result = _disp.dispatch_sync(surface, merged)
        return jsonify(result)
    except Exception as exc:
        logger.exception("inline invoke failed (%s)", command)
        return jsonify({"error": str(exc)}), 500


@app.post("/inline/tag-removed")
def api_inline_tag_removed():
    """Cancel persistent watchers whose tag was removed from a note.

    Body: {file_path, tag}
    """
    data = request.get_json(silent=True) or {}
    file_path = data.get("file_path", "")
    tag = data.get("tag", "")
    if not file_path or not tag:
        return jsonify({"error": "file_path and tag are required"}), 400

    try:
        from work_buddy.inline import store as _istore
        cleaned = tag.lstrip("#")
        removed = []
        for w in _istore.list_watchers(file_path=file_path):
            if w.tag == cleaned or w.tag == tag:
                if _istore.delete_watcher(w.watcher_id):
                    removed.append(w.watcher_id)
        logger.info("inline tag-removed: %s / %s — %d watcher(s)", file_path, tag, len(removed))
        return jsonify({"removed": len(removed), "watcher_ids": removed})
    except Exception as exc:
        logger.exception("inline tag-removed failed")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Command palette
# ---------------------------------------------------------------------------

@app.get("/api/palette/commands")
def api_palette_commands():
    """List all commands, optionally filtered by hybrid search query."""
    data = get_palette_commands(_cfg)
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(data)

    # Hybrid search: BM25 + semantic via embedding service
    scored = _hybrid_palette_search(q, data["commands"], limit=30)
    if scored is not None:
        data["commands"] = scored
        data["search_method"] = "hybrid"
    else:
        # Fallback: substring matching on name + description + category
        ql = q.lower()
        filtered = [
            c for c in data["commands"]
            if ql in c["name"].lower()
            or ql in (c.get("description") or "").lower()
            or ql in (c.get("category") or "").lower()
        ]
        data["commands"] = filtered[:30]
        data["search_method"] = "substring"
    return jsonify(data)


def _hybrid_palette_search(
    query: str,
    commands: list[dict],
    limit: int,
) -> list[dict] | None:
    """Score palette commands using BM25 + semantic search.

    Returns scored command list (descending), or None to fall back to substring.
    """
    if not _is_embed_available():
        return None

    try:
        from work_buddy.embedding.client import hybrid_search
    except ImportError:
        return None

    # Build candidates: each command → {name: id, texts: [name + description]}
    candidates = []
    cmd_by_id: dict[str, dict] = {}
    for cmd in commands:
        cid = cmd["id"]
        text = cmd["name"]
        if cmd.get("description"):
            text += " " + cmd["description"]
        if cmd.get("category"):
            text += " " + cmd["category"]
        candidates.append({"name": cid, "texts": [text]})
        cmd_by_id[cid] = cmd

    if not candidates:
        return None

    results = hybrid_search(
        query,
        candidates,
        bm25_weight=0.3,
        embed_weight=0.7,
    )

    scored = []
    for r in results[:limit]:
        cmd = cmd_by_id.get(r["name"])
        if cmd is None:
            continue
        out = dict(cmd)
        out["score"] = round(r.get("score", 0.0), 4)
        scored.append(out)
    return scored


@app.post("/api/palette/execute")
def api_palette_execute():
    """Execute a command from the palette."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    command_id = data.get("command_id", "")
    params = data.get("params", {})

    if not command_id:
        return jsonify({"success": False, "error": "No command_id provided."}), 400

    if command_id.startswith("obsidian::"):
        return _execute_obsidian(command_id[len("obsidian::"):])
    elif command_id.startswith("work-buddy::"):
        return _execute_workbuddy(command_id[len("work-buddy::"):], params)
    else:
        return jsonify({"success": False, "error": f"Unknown provider prefix in: {command_id}"}), 400


def _execute_obsidian(raw_id: str):
    """Execute an Obsidian command via the Local REST API."""
    try:
        from work_buddy.obsidian.commands import ObsidianCommands

        vault_root = Path(_cfg.get("vault_root", ""))
        cmds = ObsidianCommands(vault_root)
        cmds.execute(raw_id)
        return jsonify({"success": True, "result": f"Executed: {raw_id}", "provider": "obsidian"})
    except Exception as exc:
        logger.error("Obsidian command failed (%s): %s", raw_id, exc)
        return jsonify({"success": False, "error": str(exc)[:500], "provider": "obsidian"}), 500


def _execute_workbuddy(name: str, params: dict):
    """Execute a work-buddy capability or launch a workflow agent session."""
    try:
        from work_buddy.consent import ConsentRequired
        from work_buddy.mcp_server.registry import (
            Capability,
            WorkflowDefinition,
            get_registry,
        )

        registry = get_registry()
        entry = registry.get(name)
        if entry is None:
            return jsonify({"success": False, "error": f"Unknown capability: {name}"}), 404

        if isinstance(entry, WorkflowDefinition):
            return _request_workflow_consent(name, entry)

        assert isinstance(entry, Capability)
        try:
            result = entry.callable(**params)
        except ConsentRequired as exc:
            return _request_capability_consent(name, params, exc)

        # Serialize result to a displayable string
        if result is None:
            display = "Done (no output)"
        elif isinstance(result, str):
            display = result
        else:
            display = json.dumps(result, default=str, indent=2)

        # Short results → inline toast; long results → dashboard tab
        if len(display) <= 120:
            return jsonify({"success": True, "result": display[:500], "provider": "work-buddy"})

        # Create a palette_result view for rich display
        import time as _time
        view_id = f"cp-{name}-{int(_time.time())}"
        from work_buddy.dashboard.views import create_view
        create_view(
            view_id=view_id,
            title=name,
            view_type="palette_result",
            payload={
                "type": "palette_result",
                "command": name,
                "result": display,
                "is_error": False,
                "timestamp": _time.time(),
            },
        )
        return jsonify({
            "success": True,
            "result": display[:120] + "...",
            "provider": "work-buddy",
            "view_id": view_id,
        })
    except Exception as exc:
        logger.error("work-buddy command failed (%s): %s", name, exc)
        return jsonify({"success": False, "error": str(exc)[:500], "provider": "work-buddy"}), 500


def _request_capability_consent(name: str, params: dict, exc) -> Response:
    """Create a consent view for a consent-gated capability, with auto-retry on grant."""
    import time as _time
    from work_buddy.dashboard.views import create_view

    view_id = f"cp-consent-{name}-{int(_time.time())}"
    create_view(
        view_id=view_id,
        title=f"Consent: {name}",
        body=exc.reason,
        view_type="capability_consent",
        payload={
            "type": "capability_consent",
            "command_name": name,
            "command_id": f"work-buddy::{name}",
            "params": params,
            "operation": exc.operation,
            "risk": exc.risk,
            "default_ttl": exc.default_ttl,
        },
        response_type="choice",
        choices=[
            {"key": "always", "label": "Allow always", "description": "For this session"},
            {"key": "temporary", "label": f"Allow for {exc.default_ttl} min", "description": "Temporary"},
            {"key": "once", "label": "Allow once", "description": "Single use"},
            {"key": "deny", "label": "Deny", "description": "Do not proceed"},
        ],
    )
    return jsonify({
        "success": True,
        "result": f"Consent required for: {name}",
        "provider": "work-buddy",
        "view_id": view_id,
        "awaiting_consent": True,
    })


def _request_workflow_consent(name: str, entry) -> Response:
    """Create a consent view for a workflow, return the view_id for polling."""
    import time as _time
    from work_buddy.dashboard.views import create_view

    view_id = f"wf-consent-{name}-{int(_time.time())}"
    slash_cmd = getattr(entry, "slash_command", None)
    create_view(
        view_id=view_id,
        title=f"Launch workflow: {name}",
        body=entry.description or f"This will open a new agent session to run the '{name}' workflow.",
        view_type="workflow_consent",
        payload={
            "type": "workflow_consent",
            "workflow_name": name,
            "slash_command": slash_cmd,
        },
        response_type="choice",
        choices=[
            {"key": "launch", "label": "Launch", "description": "Open agent session"},
            {"key": "cancel", "label": "Cancel", "description": "Do not launch"},
        ],
    )
    return jsonify({
        "success": True,
        "result": f"Consent requested for workflow: {name}",
        "provider": "work-buddy",
        "view_id": view_id,
        "awaiting_consent": True,
    })


def _launch_workflow_session(name: str, entry, user_prompt: str = "") -> dict:
    """Launch a remote Claude Code session to run a workflow.

    Returns a plain dict (not a Flask Response) so it can be called from
    background threads without a request context.
    """
    try:
        from work_buddy.consent import grant_consent
        from work_buddy.session_launcher import begin_session

        # Grant consent for remote launch (same pattern as Telegram /remote)
        grant_consent("sidecar:remote_session_launch", mode="always")

        # Build prompt: slash command invocation (agent picks up the skill)
        # + optional user context
        slash_stem = getattr(entry, "slash_command", None)
        if slash_stem:
            prompt = f"/{slash_stem}"
        else:
            prompt = f'mcp__work-buddy__wb_run("{name}")\n\nExecute the workflow and follow each step.'

        if user_prompt:
            prompt = prompt + "\n\n" + user_prompt

        result = begin_session(prompt=prompt)
        if result.get("status") != "ok":
            logger.error("begin_session returned non-ok for %s: %s", name, result)
            return {"success": False, "error": result.get("reason", result.get("error", "Session launch failed"))}

        logger.info("Workflow session launched: %s (pid=%s)", name, result.get("pid"))
        return {"success": True, "pid": result.get("pid"), "message": result.get("message", "Session started.")}
    except Exception as exc:
        logger.error("Failed to launch workflow session (%s): %s", name, exc)
        return {"success": False, "error": str(exc)[:500]}


# ---------------------------------------------------------------------------
# Investigate
# ---------------------------------------------------------------------------

@app.post("/api/investigate")
def api_investigate():
    """Spawn a persistent interactive agent session to investigate an event.

    The launch brief is built by
    ``control.help_briefs.build_help_brief_for_event``: event metadata +
    the resolved sidecar log path, plus — when the request carries a
    ``component_id`` (sent by the per-component event chip) — the full
    control-graph diagnostic context for that component.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    event = data.get("event", {})
    if not event:
        return jsonify({"success": False, "error": "No event provided."}), 400
    component_id = data.get("component_id") or None

    from work_buddy.control.help_briefs import build_help_brief_for_event
    prompt = build_help_brief_for_event(event, component_id)

    try:
        from work_buddy.session_launcher import begin_session

        result = begin_session(prompt=prompt)
        return jsonify({
            "success": True,
            "session_id": result.get("session_id", ""),
            "message": result.get("message", "Session launched."),
        })
    except Exception as exc:
        logger.error("Failed to launch investigate session: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Agent launch
# ---------------------------------------------------------------------------

@app.post("/api/chats/<session_id>/resume")
def api_chat_resume(session_id: str):
    """Resume a Claude Code session in a new local terminal.

    No prompt is sent; remote-control is off. The terminal opens into the
    session's recorded working directory, ready for the user to type.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    if not session_id:
        return jsonify({"success": False, "error": "session_id required."}), 400

    try:
        from work_buddy.consent import grant_consent
        from work_buddy.session_launcher import begin_session
        from work_buddy.sessions.inspector import resolve_session_id

        # Verify the session exists before spending a terminal spawn on it —
        # begin_session falls back to a bare `claude --resume` if resolution
        # fails, which opens a useless window.
        try:
            resolved_id = resolve_session_id(session_id)
        except FileNotFoundError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404

        # Clicking the dashboard button IS the user's consent, matching the
        # pattern in api_launch_agent.
        grant_consent("sidecar:remote_session_launch", mode="always")

        result = begin_session(
            session_id=resolved_id,
            remote_control=False,
            bypass_permissions=True,
        )
        if result.get("status") != "ok":
            return jsonify({
                "success": False,
                "error": result.get("error", "Resume failed."),
            }), 500

        return jsonify({
            "success": True,
            "pid": result.get("pid"),
            "session_id": result.get("session_id"),
            "cwd": result.get("cwd"),
            "message": result.get("message", "Session resumed."),
        })
    except Exception as exc:
        logger.error("Failed to resume chat session %s: %s", session_id, exc)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.post("/api/launch-agent")
def api_launch_agent():
    """Launch an agent session — desktop (no remote) or mobile (remote control).

    Accepts:
        prompt (str, required): Initial prompt for the session.
        mode (str): "desktop" (default, no --remote-control) or
            "mobile" (with --remote-control for phone app connection).
        context (dict, optional): Tracking metadata (source, component_id).
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"success": False, "error": "No prompt provided."}), 400

    mode = data.get("mode", "desktop")
    if mode not in ("desktop", "mobile"):
        return jsonify({"success": False, "error": f"Unknown mode: {mode}"}), 400

    # TODO: For desktop mode, try claude-cli:// deep link approach —
    # would open terminal directly via URL scheme without server-side
    # subprocess launch. See claude-cli://open?cwd=<path>&q=<prompt>.

    remote_control = (mode == "mobile")

    try:
        from work_buddy.consent import grant_consent
        from work_buddy.session_launcher import begin_session

        # Clicking the dashboard button IS the user's consent.
        # Same pattern as _launch_workflow_session and Telegram /remote.
        grant_consent("sidecar:remote_session_launch", mode="always")

        result = begin_session(prompt=prompt, remote_control=remote_control)
        if result.get("status") != "ok":
            return jsonify({
                "success": False,
                "error": result.get("error", "Launch failed."),
            }), 500

        return jsonify({
            "success": True,
            "mode": mode,
            "pid": result.get("pid"),
            "message": result.get("message", "Session launched."),
        })
    except Exception as exc:
        logger.error("Failed to launch agent session: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _start_acknowledge_poller():
    """Background thread: poll messaging service for notification_acknowledge
    messages sent by the Obsidian plugin, and process them.

    This bridges the gap between Obsidian's sandbox (can only reach the
    messaging service on port 5123) and the dashboard's dismiss logic.
    """
    import threading

    def _poll_loop():
        import time as _time
        from urllib.request import Request as _Req, urlopen as _urlopen
        from urllib.error import URLError

        while True:
            _time.sleep(3)
            try:
                req = _Req(
                    "http://127.0.0.1:5123/messages?recipient=dashboard&status=pending",
                    method="GET",
                )
                resp = _urlopen(req, timeout=5)
                data = json.loads(resp.read().decode("utf-8"))
                messages = data.get("messages", []) if isinstance(data, dict) else data

                for msg in messages:
                    if msg.get("subject") != "notification_acknowledge":
                        continue

                    msg_id = msg.get("id")
                    try:
                        body = json.loads(msg.get("body", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        continue

                    notification_id = body.get("notification_id")
                    if not notification_id:
                        continue

                    logger.info("ACK via messaging: %s (msg %s)", notification_id, msg_id)

                    # Dismiss locally
                    workflow_views.dismiss_view(notification_id)

                    # Dismiss on other surfaces (skip dashboard to avoid deadlock)
                    try:
                        from work_buddy.notifications.dispatcher import SurfaceDispatcher
                        dispatcher = SurfaceDispatcher.from_config()
                        results = dispatcher.dismiss_others(
                            notification_id,
                            responding_surface="dashboard",
                        )
                        logger.info("ACK %s — dismiss results: %s", notification_id, results)
                    except Exception as exc:
                        logger.debug("ACK dismiss_others failed: %s", exc)

                    # Mark message as resolved
                    try:
                        ack_req = _Req(
                            f"http://127.0.0.1:5123/messages/{msg_id}/status",
                            data=json.dumps({"status": "resolved"}).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            method="PATCH",
                        )
                        _urlopen(ack_req, timeout=3)
                    except Exception:
                        pass

            except (URLError, OSError, TimeoutError, json.JSONDecodeError):
                pass  # messaging service unavailable — retry next cycle

    t = threading.Thread(target=_poll_loop, daemon=True, name="ack-poller")
    t.start()
    logger.info("Acknowledge poller started")


def _prewarm_control_graph() -> None:
    """Build the control-graph snapshot once so the first Settings load
    doesn't block on the cold health + requirement sweep."""
    from work_buddy.control.graph import build_graph
    build_graph()


def _prewarm_projects_activity() -> None:
    """Warm the per-folder git-activity cache so the first Projects-tab
    load doesn't eat the cold git walk. Scores are discarded; the
    populated ``_GIT_CACHE`` side effect is the point."""
    from work_buddy.projects.activity import sort_active_by_activity
    from work_buddy.projects.store import list_projects
    sort_active_by_activity(list_projects())


def _prewarm_costs() -> None:
    """Warm the default Claude-Code-usage summary so the first Costs-tab
    load doesn't eat the multi-second aggregation over all usage turns."""
    from work_buddy.dashboard.costs_claude_code_usage import (
        get_claude_code_usage_summary,
    )
    get_claude_code_usage_summary()


def main():
    import sys

    cfg = load_config()
    dashboard_cfg = cfg.get("sidecar", {}).get("services", {}).get("dashboard", {})
    port = dashboard_cfg.get("port", 5127)
    host = dashboard_cfg.get("host", "127.0.0.1")
    dev = "--dev" in sys.argv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    _start_acknowledge_poller()

    # Bootstrap the Thread system in the dashboard's process. Each
    # subprocess has its own module-level state, so
    # each needs its own bootstrap call to get the FSM handlers +
    # cleanup adapters registered. The shared helper centralizes
    # the try/except + logging so each call site is one line.
    from work_buddy.threads.bootstrap import bootstrap_for_subprocess
    bootstrap_for_subprocess(subprocess_name="dashboard")

    # Mark this process so that ``events.publish_auto`` (used by the
    # cross-cutting mutators in clarify/, tasks/, health/, etc.) routes
    # publishes to the in-process bus rather than the messaging service.
    from work_buddy.dashboard.events import mark_dashboard_process, start_heartbeat
    mark_dashboard_process()

    # Start the event-bus heartbeat (publishes ``bus.heartbeat`` every
    # 10s to keep SSE connections lively and give the browser a
    # liveness signal). The SSE endpoint also emits its own keepalive
    # comments; both are complementary.
    start_heartbeat(interval=10.0)

    # Start the fleet poller: refreshes the local model fleet snapshot and
    # publishes ``fleet.changed`` when a machine's reachability or loaded-model
    # set changes (external LM Studio loads/unloads have no internal event, so
    # the fleet section can only live-update via polling).
    try:
        from work_buddy.dashboard.api import start_fleet_poller
        start_fleet_poller(interval=25.0)
    except Exception as exc:
        logger.warning("Fleet poller failed to start: %s", exc)

    from work_buddy.web.access_log_filter import install_probe_log_filter
    install_probe_log_filter(["/health", "/internal/bus"])

    # Pre-warm the /api/state cache on a background thread. The first
    # build runs a requirement sweep + probe reads and takes 10s+; doing
    # it at startup (rather than lazily on the first Jobs-tab load) means
    # the user never eats that cold-build stall. get_system_state's build
    # lock makes this single-flight — a request that races the warm-up
    # blocks on the lock, then gets the cached result.
    #
    # Skip in the dev reloader's watcher process: with debug=True Werkzeug
    # runs main() in both the watcher and the reloaded child, and the
    # watcher's cache is discarded on every reload — warming it there is a
    # wasted 10s+ build. WERKZEUG_RUN_MAIN is set only in the child; in
    # prod (debug=False) it's unset but there's no watcher, so `not dev`
    # lets the single process warm normally.
    import os as _os

    if not dev or _os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        def _prewarm() -> None:
            # Each warm-up is independent and best-effort: a failure in one
            # must not skip the others, and none should crash startup.
            for label, fn in (
                ("system-state", get_system_state),
                ("requirements", get_requirements_snapshot),
                ("control-graph", _prewarm_control_graph),
                ("projects-activity", _prewarm_projects_activity),
                ("costs", _prewarm_costs),
            ):
                try:
                    fn()
                except Exception as exc:  # pragma: no cover - best-effort
                    logger.warning("%s pre-warm failed: %s", label, exc)

        threading.Thread(
            target=_prewarm, name="dashboard-prewarm", daemon=True,
        ).start()

    logger.info("Dashboard starting on http://%s:%d%s", host, port, " (dev mode)" if dev else "")
    app.run(host=host, port=port, debug=dev)


if __name__ == "__main__":
    main()
