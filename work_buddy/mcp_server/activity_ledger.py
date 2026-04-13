"""Session activity ledger — structured record of all MCP gateway dispatch.

Records every wb_run, wb_advance, and wb_search call as append-only JSONL
in the *calling agent's* session directory. The gateway resolves the caller
via ``wb_init`` session registration (MCP session → agent session mapping).

Queryable via ``session_activity`` and ``session_summary`` MCP capabilities.

Inspired by OpenHarness's tool carryover pattern (query.py:_record_tool_carryover)
but adapted for work-buddy's MCP-native architecture where Claude Code owns
the runtime and work-buddy exposes capabilities through the gateway.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Ledger path resolution
# ---------------------------------------------------------------------------

def _get_ledger_path(agent_session_id: str | None = None) -> Path | None:
    """Return the activity ledger path for the given agent session.

    If *agent_session_id* is provided, resolves that agent's session
    directory.  Otherwise falls back to the MCP server's own session
    (from ``WORK_BUDDY_SESSION_ID`` env var).
    """
    try:
        from work_buddy.agent_session import get_session_dir
        session_dir = get_session_dir(agent_session_id)
        return session_dir / "activity_ledger.jsonl"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core append — fire-and-forget, never raises
# ---------------------------------------------------------------------------

def _append_event(event: dict[str, Any], agent_session_id: str | None = None) -> None:
    """Append one JSON line to the agent's activity ledger.

    Protected by threading.Lock. Exceptions are logged to stderr,
    never propagated to the gateway.
    """
    try:
        path = _get_ledger_path(agent_session_id)
        if path is None:
            return
        line = json.dumps(event, default=_json_default, separators=(",", ":"))
        with _write_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # Fire-and-forget: log but never crash the gateway
        print(f"[activity_ledger] append failed: {sys.exc_info()[1]}", file=sys.stderr)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return obj.as_posix()
    return str(obj)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _session_id() -> str:
    return os.environ.get("WORK_BUDDY_SESSION_ID", "unknown")


def _duration_ms(start_monotonic: float) -> int:
    return int((time.monotonic() - start_monotonic) * 1000)


# ---------------------------------------------------------------------------
# Result summarization — extract salient fields per category
# ---------------------------------------------------------------------------

def _pick(result: dict, keys: list[str], truncate: dict[str, int] | None = None) -> dict:
    """Extract named keys from result, optionally truncating string values."""
    out: dict[str, Any] = {}
    for k in keys:
        v = result.get(k)
        if v is not None:
            if truncate and k in truncate and isinstance(v, str):
                v = v[:truncate[k]]
            out[k] = v
    return out


def summarize_result(capability_name: str, category: str, result: Any) -> dict:
    """Extract salient fields from a capability result by category."""
    if not isinstance(result, dict):
        return _fallback_summary(result)

    if category == "tasks":
        return _pick(result, ["task_id", "state", "task_text", "count", "suggestions"],
                      truncate={"task_text": 80})
    elif category == "journal":
        return _pick(result, ["entry_count", "target_date", "entries_written",
                               "section", "note"])
    elif category == "contracts":
        return _pick(result, ["contract", "name", "status", "overdue", "count",
                               "active", "stale"])
    elif category == "messaging":
        return _pick(result, ["message_id", "recipient", "subject", "thread_id",
                               "status", "count"])
    elif category == "context":
        return _pick(result, ["collectors", "bundle_path", "collector_count",
                               "query", "results_count"])
    elif category == "memory":
        return _pick(result, ["memory_id", "bank", "query", "count", "memories"])
    elif category == "notifications":
        return _pick(result, ["request_id", "response_type", "status", "mode",
                               "operation", "granted"])
    elif category == "projects":
        return _pick(result, ["slug", "name", "status", "observation_count",
                               "candidates"])
    else:
        return _fallback_summary(result)


def _fallback_summary(result: Any) -> dict:
    if isinstance(result, dict):
        keys = sorted(result.keys())[:8]
        return {"keys": keys}
    return {"type": type(result).__name__, "len": len(str(result))}


# ---------------------------------------------------------------------------
# Event recording functions — called by gateway.py
# ---------------------------------------------------------------------------

def record_init(agent_session_id: str) -> None:
    """Record a session initialization event."""
    _append_event({
        "ts": _now_iso(),
        "session_id": agent_session_id,
        "type": "session_initialized",
    }, agent_session_id=agent_session_id)


def record_capability(
    capability: str,
    category: str,
    operation_id: str,
    params: dict[str, Any],
    mutates_state: bool,
    start_time: float,
    result: Any,
    error: str | None,
    consent_required: bool,
    consent_operation: str | None = None,
    *,
    agent_session_id: str | None = None,
) -> None:
    """Record a capability invocation."""
    if error:
        status = "consent_required" if consent_required else "error"
    else:
        status = "ok"

    sid = agent_session_id or _session_id()
    _append_event({
        "ts": _now_iso(),
        "session_id": sid,
        "type": "capability_invoked",
        "capability": capability,
        "category": category,
        "operation_id": operation_id,
        "params_keys": sorted(params.keys()) if params else [],
        "mutates_state": mutates_state,
        "duration_ms": _duration_ms(start_time),
        "status": status,
        "error_summary": str(error)[:200] if error else None,
        "result_summary": summarize_result(capability, category, result) if result else None,
        "consent_required": consent_required,
        "consent_operation": consent_operation,
    }, agent_session_id=agent_session_id)


def record_workflow_started(
    workflow_name: str,
    workflow_run_id: str | None,
    operation_id: str,
    step_count: int,
    first_step_id: str | None,
    *,
    agent_session_id: str | None = None,
) -> None:
    """Record a workflow start."""
    sid = agent_session_id or _session_id()
    _append_event({
        "ts": _now_iso(),
        "session_id": sid,
        "type": "workflow_started",
        "workflow_name": workflow_name,
        "workflow_run_id": workflow_run_id,
        "operation_id": operation_id,
        "step_count": step_count,
        "first_step_id": first_step_id,
    }, agent_session_id=agent_session_id)


def record_workflow_step(
    workflow_run_id: str,
    result: Any,
    start_time: float,
    *,
    agent_session_id: str | None = None,
) -> None:
    """Record a workflow step completion from wb_advance."""
    step_id = None
    step_name = None
    next_step_id = None
    status = "ok"

    if isinstance(result, dict):
        step_id = result.get("completed_step") or result.get("step_id")
        step_name = result.get("step_name")
        next_step = result.get("current_step") or result.get("next_step")
        if isinstance(next_step, dict):
            next_step_id = next_step.get("id")
        elif isinstance(next_step, str):
            next_step_id = next_step
        if result.get("status") == "completed":
            next_step_id = None  # workflow finished
        if result.get("error"):
            status = "error"

    sid = agent_session_id or _session_id()
    _append_event({
        "ts": _now_iso(),
        "session_id": sid,
        "type": "workflow_step_completed",
        "workflow_run_id": workflow_run_id,
        "step_id": step_id,
        "step_name": step_name,
        "duration_ms": _duration_ms(start_time),
        "status": status,
        "next_step_id": next_step_id,
    }, agent_session_id=agent_session_id)


def record_search(
    query: str,
    category_filter: str | None,
    result_count: int,
    agent_session_id: str | None = None,
) -> None:
    """Record a wb_search call."""
    sid = agent_session_id or _session_id()
    _append_event({
        "ts": _now_iso(),
        "session_id": sid,
        "type": "search_performed",
        "query": query,
        "category_filter": category_filter,
        "result_count": result_count,
    }, agent_session_id=agent_session_id)


# ---------------------------------------------------------------------------
# Query functions — registered as MCP capabilities
# ---------------------------------------------------------------------------

def _read_ledger(agent_session_id: str | None = None) -> list[dict[str, Any]]:
    """Read all events from an agent session's ledger."""
    path = _get_ledger_path(agent_session_id)
    if path is None or not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def query_activity(
    event_type: str | None = None,
    capability_name: str | None = None,
    category: str | None = None,
    status: str | None = None,
    last_n: int = 20,
    include_searches: bool = False,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Query the session activity ledger with optional filters.

    Returns the last N matching entries (newest first) plus counts.
    """
    all_events = _read_ledger(agent_session_id)
    total = len(all_events)

    filtered = []
    for ev in all_events:
        if not include_searches and ev.get("type") == "search_performed":
            continue
        if event_type and ev.get("type") != event_type:
            continue
        if capability_name and ev.get("capability") != capability_name:
            continue
        if category and ev.get("category") != category:
            continue
        if status and ev.get("status") != status:
            continue
        filtered.append(ev)

    # Return last_n newest
    result_events = filtered[-last_n:] if last_n else filtered
    result_events.reverse()  # newest first

    return {
        "events": result_events,
        "total_count": total,
        "filtered_count": len(filtered),
        "returned_count": len(result_events),
    }


def query_session_summary(agent_session_id: str | None = None) -> dict[str, Any]:
    """Compact summary of what this agent session has done through work-buddy."""
    sid = agent_session_id or _session_id()
    all_events = _read_ledger(agent_session_id)
    if not all_events:
        return {"session_id": sid, "total_events": 0, "message": "No activity recorded yet."}

    by_category: dict[str, int] = {}
    by_capability: dict[str, int] = {}
    errors = 0
    consent_requests = 0
    mutations = 0
    workflows_started = 0
    workflows_completed = 0
    searches = 0
    key_artifacts: list[str] = []

    for ev in all_events:
        ev_type = ev.get("type")

        if ev_type == "capability_invoked":
            cat = ev.get("category", "unknown")
            cap = ev.get("capability", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1
            by_capability[cap] = by_capability.get(cap, 0) + 1

            if ev.get("status") == "error":
                errors += 1
            if ev.get("consent_required"):
                consent_requests += 1
            if ev.get("mutates_state"):
                mutations += 1

            # Extract key artifacts from result_summary
            rs = ev.get("result_summary") or {}
            for artifact_key in ("task_id", "message_id", "request_id",
                                 "memory_id", "bundle_path", "slug"):
                val = rs.get(artifact_key)
                if val and str(val) not in key_artifacts:
                    key_artifacts.append(f"{artifact_key}={val}")

        elif ev_type == "workflow_started":
            workflows_started += 1
        elif ev_type == "workflow_step_completed":
            if ev.get("next_step_id") is None:
                workflows_completed += 1
        elif ev_type == "search_performed":
            searches += 1

    # Duration from first to last event
    first_ts = all_events[0].get("ts", "")
    last_ts = all_events[-1].get("ts", "")
    duration_minutes = None
    try:
        t0 = datetime.fromisoformat(first_ts)
        t1 = datetime.fromisoformat(last_ts)
        duration_minutes = int((t1 - t0).total_seconds() / 60)
    except (ValueError, TypeError):
        pass

    # Top capabilities by frequency
    top_capabilities = dict(
        sorted(by_capability.items(), key=lambda x: x[1], reverse=True)[:10]
    )

    return {
        "session_id": sid,
        "total_events": len(all_events),
        "duration_minutes": duration_minutes,
        "capabilities_invoked": sum(by_category.values()),
        "by_category": by_category,
        "by_capability": top_capabilities,
        "workflows_started": workflows_started,
        "workflows_completed": workflows_completed,
        "searches": searches,
        "errors": errors,
        "consent_requests": consent_requests,
        "mutations": mutations,
        "key_artifacts": key_artifacts[:20],
    }
