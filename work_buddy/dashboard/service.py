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
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

from work_buddy.config import load_config
from work_buddy.dashboard.api import (
    get_chats_summary,
    get_contracts_summary,
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
    published to the in-process bus (and, via the messaging bridge,
    every cross-process event). The browser's native ``EventSource``
    reconnects automatically on disconnect; the bus does not replay
    events from before the reconnect.

    No read-only gate: this is a pure read endpoint.
    """
    from work_buddy.dashboard.events import get_bus

    bus = get_bus()
    resp = Response(_sse_stream(bus), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # nginx-friendly; harmless on Tailscale
    resp.headers["Connection"] = "keep-alive"
    return resp


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    resp = Response(render_page(), content_type="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/favicon.svg")
def favicon():
    logo = Path(__file__).parent.parent.parent / "docs" / "logo.svg"
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


@app.get("/api/diagnose/<component_id>")
def api_diagnose(component_id: str):
    """Run diagnostic checks on a component and return root cause + fix."""
    try:
        from work_buddy.health.diagnostics import DiagnosticRunner
        runner = DiagnosticRunner()
        result = runner.diagnose(component_id)
        return jsonify(result.to_dict())
    except Exception as exc:
        return jsonify({"component_id": component_id, "status": "error",
                        "root_cause": str(exc)}), 500


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


@app.get("/api/requirements")
def api_requirements():
    """Full requirements validation results."""
    try:
        from work_buddy.health.requirements import RequirementChecker
        checker = RequirementChecker()
        bootstrap = checker.check_bootstrap()
        all_reqs = checker.check_all(include_unwanted=False)
        return jsonify({
            "bootstrap": {
                "summary": checker.summarize(bootstrap),
                "results": [r.to_dict() for r in bootstrap],
            },
            "all": {
                "summary": checker.summarize(all_reqs),
                "results": [r.to_dict() for r in all_reqs],
            },
        })
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

    # Build metadata filter for project scoping — applied at the SQLite
    # level in load_documents via json_extract, so BM25 only scores
    # matching docs and results aren't starved by other-project dominance.
    meta_filter = {"project_name": project} if project else None

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


# ---------------------------------------------------------------------------
# Costs tab
# ---------------------------------------------------------------------------
#
# Aggregates first-party LLM cost log files written by ``work_buddy.llm.cost``
# at ``data/agents/<session>/llm_costs.jsonl``. Phase 2 adds Claude Code
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

    Read-only view of ``data/runtime/rate_limits.json``, populated by
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


# ---------------------------------------------------------------------------
# Background-triage Review tab
# ---------------------------------------------------------------------------
#
# The Review tab is the on-demand review surface for the
# pending-review pool populated by background-triage producers
# (journal hourly triage today; more sources later). It fetches the
# composed presentation without opening the legacy modal, and posts
# back approved decisions through the existing triage execute path.
# Same action taxonomy as the Chrome triage modal; source-aware note
# headers come for free via ``work_buddy.clarify.execute``.


@app.get("/api/review")
def api_review_pool():
    """Return the composed pending-review presentation.

    Query params:
        source: optional source filter (e.g. 'journal_thread').
        adapter: optional adapter-name filter.
        max_items: cap on pending entries (default 100).
    """
    from work_buddy.clarify.capabilities.triage_review_pool import triage_review_pool

    source = request.args.get("source") or None
    adapter = request.args.get("adapter") or None
    try:
        max_items = int(request.args.get("max_items", "100"))
    except ValueError:
        max_items = 100

    try:
        result = triage_review_pool(
            source=source, adapter=adapter,
            max_items=max_items, dispatch=False,
        )
    except Exception as exc:
        logger.exception("api_review: triage_review_pool failed")
        return jsonify({"status": "error", "error": str(exc)}), 500

    return jsonify(result)


@app.post("/api/review/execute")
def api_review_execute():
    """Execute user decisions against a presentation + mark pool reviewed.

    Request body: ``{presentation: {...}, decisions: {group_decisions: [...]}}``.
    Mirrors what the legacy modal flow sends back; we reuse the same
    ``triage_execute`` capability for side effects, then stamp the
    pool entries as reviewed so they drop out of the next render.
    """
    rejected = _reject_read_only()
    if rejected is not None:
        return rejected

    data = request.get_json(silent=True) or {}
    presentation = data.get("presentation") or {}
    decisions = data.get("decisions") or {}

    if not presentation.get("groups_by_action"):
        return jsonify({
            "status": "error",
            "error": "presentation.groups_by_action is required",
        }), 400

    from work_buddy.clarify.execute import execute_triage_decisions
    from work_buddy.clarify.background import get_pool
    from work_buddy.consent import user_initiated

    # The user clicked Submit on a Review-tab card. That click IS the
    # consent — pre-emptively prompting for ``tasks.create_task`` /
    # ``obsidian.write_file`` would be redundant ceremony. Wrap the
    # execute in a user_initiated context so nested @requires_consent
    # gates pass through, with an audit-log entry distinguishing
    # UI-driven actions from autonomous ones.
    try:
        with user_initiated("dashboard.review_submit"):
            executed = execute_triage_decisions(decisions, presentation)
    except Exception as exc:
        logger.exception("api_review_execute: execute failed")
        return jsonify({"status": "error", "error": str(exc)}), 500

    # Slice 1 fix (data-loss bug): only mark reviewed entries that
    # were (a) decided on by this submit AND (b) whose op actually
    # succeeded. The original code walked the entire presentation
    # and stamped every entry — so submitting one card via the
    # per-group-submit frontend marked all cards reviewed.
    #
    # The first fix narrowed by ``group_index in decided_indices``,
    # but missed a second case: an op can FAIL (bridge timeout,
    # consent denial, EditorConflict) and still get stamped, so the
    # user sees the card disappear with no task created. The second
    # filter — ``item_ids appears in a successful-op bucket`` —
    # closes that gap. Failed entries stay pending so the user can
    # retry.
    decided_indices: set[int] = set()
    for gd in (decisions.get("group_decisions") or []):
        idx = gd.get("group_index")
        if isinstance(idx, int):
            decided_indices.add(idx)

    # Walk the executor's per-action success buckets and collect every
    # item_id that landed in one. Failed ops only live in
    # ``details.errors`` (not in any success bucket), so they're
    # naturally excluded.
    #
    # The executor's bucket shapes are historically inconsistent: some
    # buckets carry an ``item_ids`` list (tasks_created, tasks_recorded,
    # grouped — naturally multi-item) and others carry a singular
    # ``item_id`` (closed, left — naturally per-item). Handle both.
    # ``skipped_stale`` is excluded so the user can re-decide stale
    # entries instead of having them silently disappear.
    succeeded_item_ids: set[str] = set()
    details = (executed or {}).get("details", {}) or {}
    for bucket_name in (
        # Legacy buckets (Slice 1 actions).
        "tasks_created", "tasks_recorded", "grouped",
        "closed", "left",
        # Slice 3 buckets — multi-record execution feeds these for
        # destinations that aren't yet wired (reference, calendar) and
        # for the Slice 3-only ``delete`` destination. Each entry carries
        # ``item_id`` scalar (per-record) so the singular branch below
        # picks them up. ``records_executed`` carries ``item_ids`` list
        # (per-group rollup, in addition to the per-record entries).
        "deleted", "references_logged", "calendar_logged",
        "records_executed",
    ):
        for entry in details.get(bucket_name, []) or []:
            # Plural form: item_ids list
            for iid in entry.get("item_ids", []) or []:
                if iid:
                    succeeded_item_ids.add(iid)
            # Singular form: item_id scalar
            single = entry.get("item_id")
            if single:
                succeeded_item_ids.add(single)

    keys: list[tuple[str, str]] = []
    for action_groups in presentation.get("groups_by_action", {}).values():
        for group in action_groups:
            if group.get("index") not in decided_indices:
                continue
            run_id = group.get("pool_run_id")
            if not run_id:
                continue
            for item in group.get("items", []) or []:
                iid = item.get("id")
                if not iid:
                    continue
                if iid not in succeeded_item_ids:
                    continue  # op failed for this item — keep pending
                keys.append((run_id, iid))

    stamped = 0
    if keys:
        try:
            stamped = get_pool().mark_reviewed(keys, outcome="reviewed")
        except Exception as exc:
            # Non-fatal: execution already succeeded; pool-cleanup
            # failure just means the entries will re-appear on next
            # load. Surface to the caller so they know.
            logger.warning("api_review_execute: mark_reviewed failed: %s", exc)
            return jsonify({
                "status": "partial",
                "executed": executed,
                "pool_updates": 0,
                "pool_error": str(exc),
            })

    # Slice 1 fix (silent-failure bug): surface per-operation errors
    # at the top level so the frontend can show "Action failed:
    # consent required" rather than swallowing them. ``executed``
    # comes from ``triage_execute.execute_triage_decisions`` which
    # catches per-op exceptions into ``details.errors``; the user
    # had no way to see those before this surfacing.
    op_errors = (executed or {}).get("details", {}).get("errors") or []
    response_status = "partial" if op_errors else "ok"

    return jsonify({
        "status": response_status,
        "executed": executed,
        "pool_updates": stamped,
        "operation_errors": op_errors,  # explicit top-level surfacing
    })


# ---------------------------------------------------------------------------
# Resolution Surface (Slice 1.5) — defer + redirect surfaces
# ---------------------------------------------------------------------------
#
# The Resolution Surface complements ``/api/review/execute`` with two
# user-initiated micro-actions that don't go through the triage executor:
#
#   POST /api/triage/defer    — "Later" path; bumps ``attraction_passes``
#   POST /api/triage/redirect — Re-direct mode; persists ``forced_context``
#                                + quarantines so the entry doesn't keep
#                                rendering with stale agent reasoning
#
# Both wrap ``user_initiated`` so nested @requires_consent gates pass
# through (see ``notifications/consent`` knowledge unit). Both narrow
# entry targeting to ``(pool_run_id, item_id)`` — never bulk-touch the
# whole presentation.

@app.post("/api/triage/defer")
def api_triage_defer():
    """One-click "Later" defer for a pool entry (Slice 1.5).

    Bumps ``attraction_passes`` on the entry. Does NOT mark the entry
    reviewed — the user said "not now," not "I acted on it." Slice 8
    will read ``attraction_passes`` for resurfacing priority.

    Request body: ``{"pool_run_id": "...", "item_id": "..."}``.
    Response: ``{"status": "ok", "bumped": 0|1, "attraction_passes": N}``.
    """
    rejected = _reject_read_only()
    if rejected is not None:
        return rejected

    data = request.get_json(silent=True) or {}
    run_id = (data.get("pool_run_id") or "").strip()
    item_id = (data.get("item_id") or "").strip()
    if not run_id or not item_id:
        return jsonify({
            "status": "error",
            "error": "pool_run_id and item_id are required",
        }), 400

    from work_buddy.clarify.background import get_pool
    from work_buddy.consent import user_initiated

    try:
        # The user clicked Later. That click IS the consent for the
        # increment; consent-gated dependencies (e.g. future audit-log
        # writes) pass through.
        with user_initiated("dashboard.resolution_defer"):
            bumped = get_pool().increment_attraction_pass([(run_id, item_id)])
    except Exception as exc:
        logger.exception("api_triage_defer: increment failed")
        return jsonify({"status": "error", "error": str(exc)}), 500

    # Re-read the entry so the client can render the updated count.
    new_count: int | None = None
    try:
        for pe in get_pool().all_entries():
            if pe.run_id == run_id and pe.item_id == item_id:
                new_count = int(pe.attraction_passes or 0)
                break
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "bumped": bumped,
        "attraction_passes": new_count,
    })


@app.post("/api/triage/redirect")
def api_triage_redirect():
    """Re-direct mode for a pool entry (Slice 1.5).

    Persists user-supplied ``forced_context`` on the entry and
    quarantines the current entry so the stale verdict drops out of
    the active surface. The actual pipeline re-run with the forced
    context is wired in by Slice 3 (Clarify rewrite); Slice 1.5 lays
    the surface and the persistence so Slice 3 has the data when it
    lands.

    Request body::

        {
          "pool_run_id": "...",
          "item_id": "...",
          "forced_context": {
              "freeform": "...",            # user's free-text reroute
              "project": "personal/finance" # optional structured field
              # ... extensible, opaque to Slice 1.5
          },
          "target_step": "clarify"          # default; reserved for Slice 3
        }

    Response::

        {"status": "ok", "stored": true, "quarantined": 1,
         "target_step": "clarify"}
    """
    rejected = _reject_read_only()
    if rejected is not None:
        return rejected

    data = request.get_json(silent=True) or {}
    run_id = (data.get("pool_run_id") or "").strip()
    item_id = (data.get("item_id") or "").strip()
    forced_context = data.get("forced_context") or {}
    target_step = (data.get("target_step") or "clarify").strip()

    if not run_id or not item_id:
        return jsonify({
            "status": "error",
            "error": "pool_run_id and item_id are required",
        }), 400
    if not isinstance(forced_context, dict):
        return jsonify({
            "status": "error",
            "error": "forced_context must be an object",
        }), 400
    # Reject empty redirects — they'd just be a fancy stale-mark with
    # no signal for Slice 3 to act on. Free-text or any structured
    # field qualifies.
    has_signal = any(
        bool(v) if not isinstance(v, str) else bool(v.strip())
        for v in forced_context.values()
    )
    if not has_signal:
        return jsonify({
            "status": "error",
            "error": "forced_context must include at least one non-empty value",
        }), 400

    # Stamp target_step inside the persisted payload so Slice 3 doesn't
    # have to re-derive it. Default ``clarify`` matches the canonical
    # re-queue path; explicit values from the frontend are preserved.
    payload = dict(forced_context)
    payload.setdefault("target_step", target_step)

    from work_buddy.clarify.background import get_pool, STATE_QUARANTINED
    from work_buddy.consent import user_initiated

    pool = get_pool()
    try:
        with user_initiated("dashboard.resolution_redirect"):
            stored = pool.store_forced_context(run_id, item_id, payload)
            if not stored:
                return jsonify({
                    "status": "error",
                    "error": (
                        f"Pool entry not found "
                        f"(run_id={run_id!r}, item_id={item_id!r})."
                    ),
                }), 404
            # Quarantine the current entry — its agent-side verdict is
            # premised on context the user just rejected. Fresh output
            # will arrive when Slice 3's re-queue path runs. Reason
            # ``user_redirected`` is a new value but ``mark_state``
            # accepts arbitrary reason strings; quarantine_reason is
            # free-form metadata.
            quarantined = pool.mark_state(
                [(run_id, item_id)],
                state=STATE_QUARANTINED,
                reason="user_redirected",
            )
    except Exception as exc:
        logger.exception("api_triage_redirect: persist failed")
        return jsonify({"status": "error", "error": str(exc)}), 500

    return jsonify({
        "status": "ok",
        "stored": True,
        "quarantined": quarantined,
        "target_step": target_step,
    })


# ---------------------------------------------------------------------------
# Engage view payload
# ---------------------------------------------------------------------------
#
# Originally drove the v4 Engage tab; that surface was removed once
# v5 Threads became the canonical "what should I act on" UI. The
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
    from work_buddy.obsidian.tasks import store as tasks_store
    from work_buddy.automation.risk import resolve_operating_tier
    from work_buddy.automation.contexts import (
        parse_context_list,
        resolve_who_can_act,
        user_satisfies_against,
        list_known_context_tokens,
    )
    from work_buddy.clarify.resolution import PIPELINE_BLOCKER_PRESENTATION

    cfg = load_config()
    rows = tasks_store.query(include_archived=False)
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
    """Slice 5b Today tab payload.

    Composes:
    - The Slice-5a engage view (filtered by ``current_contexts``).
    - The clamp-to-now plan from ``work_buddy.task_me.build_now_plan``.
    - The top 1-2 recommendations heuristic from ``task_me.top_recommendations``.
    - A current-time indicator + work-hour bounds from config.

    No mutations.  This is the read-only side of the Today surface;
    write-back happens via the ``/wb-task-me`` slash command's optional
    consent-gated reasoning step.
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
    """Slice 5b Today tab payload.

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

@app.get("/api/projects")
def api_projects():
    """List all projects with observation counts."""
    try:
        from work_buddy.projects.store import list_projects
        projects = list_projects()
        return jsonify({"projects": projects})
    except Exception as e:
        logger.exception("Failed to list projects")
        return jsonify({"projects": [], "error": str(e)})


@app.get("/api/projects/<slug>")
def api_project_detail(slug: str):
    """Get a single project with Hindsight memory recall."""
    try:
        from work_buddy.projects.store import get_project
        project = get_project(slug)
        if not project:
            return jsonify({"error": f"Project '{slug}' not found"}), 404

        # Strip SQLite observations (legacy) — memory comes from Hindsight
        project.pop("observations", None)

        # Recall from Hindsight project bank (cheap embedding search)
        memory = ""
        try:
            from work_buddy.memory.query import recall_project_context
            memory = recall_project_context(
                query=f"Current state, recent decisions, and trajectory for {slug}",
                project_slug=slug,
                budget="low",
                max_tokens=2048,
            )
        except Exception:
            logger.debug("Hindsight recall unavailable for %s", slug)

        project["memory"] = memory or None
        return jsonify(project)
    except Exception as e:
        logger.exception("Failed to get project %s", slug)
        return jsonify({"error": str(e)}), 500


@app.post("/api/projects/<slug>")
def api_project_update(slug: str):
    """Update project identity fields."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        from work_buddy.projects.store import update_project
        kwargs = {}
        if "name" in data:
            kwargs["name"] = data["name"]
        if "status" in data:
            kwargs["status"] = data["status"]
        if "description" in data:
            kwargs["description"] = data["description"]

        if not kwargs:
            return jsonify({"error": "No fields to update"}), 400

        result = update_project(slug, **kwargs)
        if result is None:
            return jsonify({"error": f"Project '{slug}' not found"}), 404
        return jsonify(result)
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

        # Ensure project exists in registry
        if not get_project(slug):
            upsert_project(slug, slug, status="inferred")

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


@app.get("/api/projects/<slug>/memories")
def api_project_memories(slug: str):
    """List Hindsight memories for a project (chronological log)."""
    limit = request.args.get("limit", 30, type=int)
    try:
        from work_buddy.memory.query import list_recent_project_memories
        items = list_recent_project_memories(limit=limit, project=slug)
        # Normalize to plain dicts for JSON serialization
        memories = []
        for m in items:
            mem = dict(m) if isinstance(m, dict) else {}
            memories.append({
                "id": mem.get("id", ""),
                "text": mem.get("text", ""),
                "fact_type": mem.get("fact_type", ""),
                "date": mem.get("date", ""),
                "context": mem.get("context", ""),
                "entities": mem.get("entities", ""),
                "tags": mem.get("tags", []),
            })
        return jsonify({"memories": memories, "slug": slug})
    except Exception as e:
        logger.exception("Failed to list memories for project %s", slug)
        return jsonify({"memories": [], "error": str(e)})


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
# v5 Threads API (Stage 4.3)
#
# Read endpoints + commit endpoints for the unified Threads tab. All
# routed under /api/threads/. UX.md §15 Stage 4.3 spec.
# ---------------------------------------------------------------------------


@app.get("/api/threads")
def api_v5_threads_list():
    """List top-level v5 Threads (those with no parent_id).

    Query params:
        ?show_later=1         — include Threads with future resurface_at.
        ?q=...                — substring search over search_blob.
        ?state=...            — filter by FSM state.
        ?subtype=...          — 'task' for Tasks-only.
        ?urgency=...          — 'surface_now' | 'defer' (post-query).
        ?has_cleanup=1        — only Threads where the cleanup
                                adapter is applicable (post-query).
        ?limit=N              — page size (default 100).
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
    """
    try:
        from work_buddy.threads.render import build_render_data
        from work_buddy.threads.search import search_threads
        q = request.args.get("q") or ""
        state = request.args.get("state") or None
        subtype = request.args.get("subtype") or None
        urgency = request.args.get("urgency") or None
        has_cleanup_only = request.args.get("has_cleanup") == "1"
        include_future = request.args.get("show_later") == "1"
        actionable_only = request.args.get("show_all") != "1"
        include_mid_process = request.args.get("include_mid_process") == "1"
        limit = int(request.args.get("limit", 100))
        threads_models = search_threads(
            q,
            parent_id=None,
            state=state,
            subtype=subtype,
            show_later=include_future,
            actionable_only=actionable_only,
            include_mid_process=include_mid_process,
            limit=limit,
        )
        threads = []
        for t in threads_models:
            data = build_render_data(t.thread_id)
            if data is None:
                continue
            # Post-query filters — neither has a SQL index, but the
            # cardinality at this point (post search) is small.
            if urgency and data.get("urgency") != urgency:
                continue
            if has_cleanup_only and not data.get("can_clean_up"):
                continue
            threads.append(data)
        return jsonify({"threads": threads})
    except Exception as exc:
        logger.exception("v5 threads list failed: %s", exc)
        return jsonify({"threads": [], "error": str(exc)}), 500


# A small allowlist of v5 dashboard-triggerable capabilities. We
# intentionally don't expose the full registry — the user's
# workflow is "MCP from agent for power", "dashboard buttons for
# common nudges." Adding a capability here is a deliberate UX
# decision (each appears as a button somewhere in the v5 UI).
_DASHBOARD_RUNNABLE_CAPABILITIES: dict[str, dict] = {
    "journal_v5_scan": {
        "description": (
            "Segment today's journal Running Notes into v5 Threads. "
            "Wired to the empty-state CTA on the Threads tab."
        ),
        "mutates_state": True,
    },
}


@app.post("/api/run/<capability_name>")
def api_v5_run_capability(capability_name: str):
    """Bridge endpoint that lets the dashboard trigger a small
    allowlist of v5 capabilities directly.

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
def api_v5_thread_get(thread_id: str):
    """Fetch one v5 Thread + its render data."""
    try:
        from work_buddy.threads.render import build_render_data
        data = build_render_data(thread_id)
        if data is None:
            return jsonify({"error": "Thread not found"}), 404
        return jsonify(data)
    except Exception as exc:
        logger.exception("v5 thread get failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/threads/<thread_id>/sub")
def api_v5_thread_sub_list(thread_id: str):
    """List sub-threads under a parent."""
    try:
        from work_buddy.threads.render import list_render_data
        threads = list_render_data(parent_id=thread_id, limit=200)
        return jsonify({"threads": threads, "parent_id": thread_id})
    except Exception as exc:
        logger.exception(
            "v5 sub-thread list failed for %s: %s", thread_id, exc,
        )
        return jsonify({"threads": [], "error": str(exc)}), 500


@app.get("/api/threads/<thread_id>/events")
def api_v5_thread_events(thread_id: str):
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
            "v5 thread events fetch failed for %s: %s", thread_id, exc,
        )
        return jsonify({"events": [], "error": str(exc)}), 500


def _v5_post_action(
    thread_id: str, *, trigger: str, data_extras=None,
):
    """Common POST handler — fires an FSM transition through engine."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    payload = request.get_json(silent=True) or {}
    try:
        from work_buddy.threads import engine
        merged = dict(payload)
        if data_extras:
            merged.update(data_extras)
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
        logger.exception("v5 thread action failed for %s: %s", thread_id, exc)
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


@app.post("/api/threads/<thread_id>/accept")
def api_v5_thread_accept(thread_id: str):
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
        return _v5_post_action(thread_id, trigger=trigger)
    except Exception as exc:
        logger.exception("v5 accept failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/retry-cleanup")
def api_v5_thread_retry_cleanup(thread_id: str):
    """Retry a failed cleanup. UX.md §6.5."""
    return _v5_post_action(thread_id, trigger="retry_cleanup")


@app.post("/api/threads/<thread_id>/context/<item_id>/migrate")
def api_v5_context_migrate(thread_id: str, item_id: str):
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
            "v5 context migrate failed for %s → %s: %s",
            thread_id, to_thread_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/accept-cleanup-failure")
def api_v5_thread_accept_cleanup_failure(thread_id: str):
    """Accept a failed cleanup; thread → done. UX.md §6.5."""
    return _v5_post_action(thread_id, trigger="accept_cleanup_failure")


@app.post("/api/threads/<thread_id>/dismiss")
def api_v5_thread_dismiss(thread_id: str):
    """Trash the Thread. Transition to DISMISSED."""
    return _v5_post_action(thread_id, trigger="dismissed_by_user")


@app.post("/api/threads/<thread_id>/redirect")
def api_v5_thread_redirect(thread_id: str):
    """Re-direct: push back to inference with feedback. UX.md §5.3."""
    return _v5_post_action(thread_id, trigger="redirected")


@app.post("/api/threads/<thread_id>/cleanup")
def api_v5_thread_cleanup(thread_id: str):
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
        logger.exception("v5 cleanup failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/later")
def api_v5_thread_later(thread_id: str):
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
        logger.exception("v5 later failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<src_id>/move_item")
def api_v5_thread_move_item(src_id: str):
    """Stage 5 v2: move a single ContextItem from one group child to
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
            "v5 move_item failed for %s/%s -> %s: %s",
            src_id, item_id, dest_thread_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<umbrella_id>/spawn_empty_group")
def api_v5_thread_spawn_empty_group(umbrella_id: str):
    """Stage 5 v2: add an empty group child under ``umbrella_id``.

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
            "v5 spawn_empty_group failed for %s: %s", umbrella_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/delete_group_subthread")
def api_v5_thread_delete_group_subthread(thread_id: str):
    """Stage 5 v2: dismiss a group child via the column-header X
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
            "v5 delete_group_subthread failed for %s: %s", thread_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<umbrella_id>/approve_all")
def api_v5_thread_approve_all(umbrella_id: str):
    """Stage 5 v2: cascade Accept to every non-terminal child of the
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
            "v5 approve_all failed for %s: %s", umbrella_id, exc,
        )
        return jsonify({"error": str(exc)}), 500


# Suggestions endpoint — stubbed for now. The pre-v2 implementation
# operated at thread granularity and isn't directly portable to the
# new umbrella+items model. Returning an empty list keeps the panel
# rendering cleanly until v2 suggestions are designed.
@app.get("/api/threads/<thread_id>/group_suggestions")
def api_v5_thread_group_suggestions(thread_id: str):
    """Stage 5 v2: stub — cross-group item-level suggestions are not
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
def api_v5_thread_groups(umbrella_id: str):
    """Stage 5 v2: list the children of a group umbrella, with each
    child's ``context_items`` (the actual items inside that group:
    Chrome tabs, journal lines, etc.) rendered inline so the frontend
    can paint the whole multi-column grid in one fetch.

    Returns::

        {
          "umbrella_id": str,
          "groups": [
            {<standard thread render dict>,
             "context_items": [{ContextItemDict}, ...]},
            ...
          ]
        }

    The standard render shape includes thread_id, title, fsm_state,
    intent, actions, etc. — same as ``GET /api/threads/<id>``. The
    ``context_items`` field is **already** part of the standard
    render in v2 (now that items live there); this endpoint just
    bundles all children of the umbrella in one round trip.

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
        return jsonify({
            "umbrella_id": umbrella_id,
            "groups": groups_out,
        })
    except Exception as exc:
        logger.exception(
            "v5 groups failed for %s: %s", umbrella_id, exc,
        )
        return jsonify({"groups": [], "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Conversation API (renamed from Thread chat in v5 Stage 1)
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
    """Get a conversation with all messages in chronological order."""
    try:
        from work_buddy.conversations.store import get_conversation_with_messages
        result = get_conversation_with_messages(conversation_id)
        if result is None:
            return jsonify({"error": "Conversation not found"}), 404
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
        ok = close_conversation(conversation_id)
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
    """Spawn a persistent interactive agent session to investigate an issue."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    event = data.get("event", {})
    if not event:
        return jsonify({"success": False, "error": "No event provided."}), 400

    # Resolve the actual sidecar log path
    from work_buddy.paths import data_dir
    agents_dir = data_dir("agents")
    log_path = None
    if agents_dir.exists():
        for d in sorted(agents_dir.iterdir(), reverse=True):
            if "sidecar" in d.name:
                candidate = d / "logs" / "work_buddy.log"
                if candidate.exists():
                    log_path = str(candidate)
                    break

    from datetime import datetime, timezone
    ts = event.get("ts", 0)
    time_str = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%H:%M:%S") if ts else "unknown"

    lines = [
        "Please investigate this issue from the sidecar event log:",
        "",
        f"  Time:    {time_str}",
        f"  Event:   {event.get('kind', '?')}",
        f"  Source:  {event.get('source', '?')}",
        f"  Level:   {event.get('level', '?')}",
        f"  Summary: {event.get('summary', '?')}",
        "",
    ]
    if log_path:
        lines.append(f"The sidecar log file is at: {log_path}")
        lines.append("Search for the source name and timestamp to find the full context.")
    else:
        lines.append("No sidecar log file was found. Check the sidecar console output instead.")
    lines += ["", "Diagnose the root cause and fix the issue if possible."]
    prompt = "\n".join(lines)

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

    # v5 Stage 4: bootstrap the v5 Thread system in the dashboard's
    # process. Each subprocess has its own module-level state, so
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

    # Start the cross-process bridge: pulls ``bus.event`` messages
    # from the messaging service (port 5123) and republishes them on
    # the in-process bus. Sidecar publishers (cron jobs, IR rebuilds,
    # service-health monitor) reach the dashboard via this bridge.
    from work_buddy.dashboard.messaging_bridge import start_messaging_bridge
    start_messaging_bridge()

    logger.info("Dashboard starting on http://%s:%d%s", host, port, " (dev mode)" if dev else "")
    app.run(host=host, port=port, debug=dev)


if __name__ == "__main__":
    main()
