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
# Thread chat API
# ---------------------------------------------------------------------------


@app.get("/api/threads")
def api_threads_list():
    """List threads, optionally filtered by status."""
    status = request.args.get("status")
    try:
        from work_buddy.threads.store import list_threads
        threads = list_threads(status=status)
        return jsonify({"threads": threads})
    except Exception as exc:
        logger.error("Thread list failed: %s", exc)
        return jsonify({"threads": [], "error": str(exc)})


@app.get("/api/threads/<thread_id>")
def api_thread_get(thread_id: str):
    """Get a thread with all messages in chronological order."""
    try:
        from work_buddy.threads.store import get_thread_with_messages
        result = get_thread_with_messages(thread_id)
        if result is None:
            return jsonify({"error": "Thread not found"}), 404
        return jsonify(result)
    except Exception as exc:
        logger.error("Thread get failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/respond")
def api_thread_respond(thread_id: str):
    """User sends a message or responds to a pending question.

    Expects: {"value": "user's text"}

    If there's a pending question, answers it. Otherwise adds a general
    user message to the thread.
    """
    blocked = _reject_read_only()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    value = data.get("value", "")
    if not value and value is not False:
        return jsonify({"error": "Missing 'value' in request body"}), 400

    try:
        from work_buddy.threads.store import respond_to_thread, add_message
        # Try to answer a pending question first
        msg = respond_to_thread(thread_id, str(value))
        if msg is not None:
            return jsonify({"responded": True, "message_id": msg.message_id})
        # No pending question — add as a general user message
        msg = add_message(thread_id, "user", str(value))
        if msg is None:
            return jsonify({"error": "Thread not found or closed"}), 404
        return jsonify({"sent": True, "message_id": msg.message_id})
    except Exception as exc:
        logger.error("Thread respond failed for %s: %s", thread_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/api/threads/<thread_id>/close")
def api_thread_close(thread_id: str):
    """Close a thread."""
    blocked = _reject_read_only()
    if blocked:
        return blocked
    try:
        from work_buddy.threads.store import close_thread
        ok = close_thread(thread_id)
        if not ok:
            return jsonify({"error": "Thread not found"}), 404
        return jsonify({"closed": True})
    except Exception as exc:
        logger.error("Thread close failed for %s: %s", thread_id, exc)
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
        from work_buddy.remote_session import begin_session

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
        from work_buddy.remote_session import begin_session

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

    logger.info("Dashboard starting on http://%s:%d%s", host, port, " (dev mode)" if dev else "")
    app.run(host=host, port=port, debug=dev)


if __name__ == "__main__":
    main()
