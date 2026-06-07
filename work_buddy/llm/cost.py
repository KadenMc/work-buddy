"""Append-only LLM cost log with caller tracing and cost breakdown.

Writes to ``agents/<session>/llm_costs.jsonl`` — one JSON object per line.
Each entry records: model, tokens, cost, task_id, caller chain, trace_id,
and cache status. Provides session-level totals and per-task breakdowns.

Cost computation lives in :mod:`work_buddy.llm.claude_code_usage.pricing`
(:func:`calc_cost`) — one canonical pricing table for the whole repo.
Rows produced before the consolidation (pre-2026-04-25) are stamped with
``priced_with: "v1"`` by the migration; rows produced after carry
``priced_with: "v2"``. The stamp is bookkeeping for future migrations;
costs themselves did not change because legacy rows lack the cache
token data needed to apply cache-rate adjustments retroactively.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.llm.claude_code_usage.pricing import calc_cost

logger = logging.getLogger(__name__)


# Bump this when the cost-computation contract changes in a way that
# rows can't be re-derived from. The migration writes this value into
# every existing row so future migrations can detect old shapes.
_PRICING_VERSION = "v2"


def _cost_log_path() -> Path:
    """Resolve the cost log path, routing to the originating session if set.

    When the sidecar's retry sweep replays a queued llm_submit op, it sets
    an originating-session context var so this log entry lands in the
    agent's directory rather than the sidecar's. Falls back to the normal
    session when no override is active.
    """
    from work_buddy.agent_session import (
        get_session_dir,
        get_originating_session,
    )

    override = get_originating_session()
    session_dir = get_session_dir(override) if override else get_session_dir()
    return session_dir / "llm_costs.jsonl"


def _get_caller_chain(skip: int = 2) -> list[str]:
    """Walk the call stack and return a compact caller chain.

    Skips internal frames (this module + runner.py) and returns the
    external callers that triggered the LLM call, bottom-up.

    Example: ["chrome_infer:infer_browsing_activity", "context_wrappers:chrome_infer"]
    """
    chain: list[str] = []
    internal = {"cost.py", "runner.py"}
    for frame_info in inspect.stack()[skip:]:
        filename = Path(frame_info.filename).name
        if filename in internal:
            continue
        if filename.startswith("<") or "site-packages" in frame_info.filename:
            break
        func = frame_info.function
        module = filename.replace(".py", "")
        chain.append(f"{module}:{func}")
        if len(chain) >= 4:
            break
    return chain


def _read_log() -> list[dict]:
    """Read all log entries."""
    path = _cost_log_path()
    if not path.exists():
        return []
    entries = []
    try:
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if line:
                entries.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        pass
    return entries


def log_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    task_id: str,
    *,
    trace_id: str | None = None,
    cached: bool = False,
    execution_mode: str = "cloud",
    backend: str | None = None,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> None:
    """Append a cost entry to the session log.

    Args:
        model: Model name used.
        input_tokens: Fresh-input token count (excludes cache reads/writes).
        output_tokens: Output token count.
        task_id: Identifier for the task (e.g., "chrome_infer:batch").
        trace_id: Optional UUID linking related calls in a single invocation.
        cached: If True, this was a work-buddy-side cache hit (no API call,
            zero cost). Distinct from ``cache_read_tokens`` which is
            Anthropic's server-side prompt cache.
        execution_mode: ``"cloud"`` (default) or ``"local"``. Local calls
            log ``estimated_cost_usd: 0.0`` rather than falling back to
            the unknown-model price heuristic.
        backend: Optional backend id (e.g., ``"anthropic_default"``,
            ``"lmstudio_local"``) for per-backend cost breakdowns.
        cache_read_tokens: Input tokens served from Anthropic's server-side
            prompt cache (90% off the input rate). Available on every
            Anthropic response when prompt caching is enabled. Local
            backends don't have this concept; default 0.
        cache_creation_tokens: Input tokens being written to the cache
            (25% premium on the input rate). Same notes as cache_read.
    """
    if cached or execution_mode == "local":
        est_cost = 0.0
    else:
        est_cost = round(
            calc_cost(
                model, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
            ),
            6,
        )

    entry: dict[str, Any] = {
        # UTC with explicit tz offset so the frontend's ``new Date(...)``
        # (which interprets TZ-less ISO as local time) doesn't compare
        # against the wrong epoch. Also makes cross-machine log files
        # safely portable. Pre-2026-04-26 rows lacked the offset; the
        # frontend defensively appends "Z" to TZ-less strings before
        # parsing, so legacy rows degrade gracefully.
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "task_id": task_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "estimated_cost_usd": est_cost,
        "cached": cached,
        "execution_mode": execution_mode,
        "priced_with": _PRICING_VERSION,
        "caller": _get_caller_chain(),
    }
    if backend:
        entry["backend"] = backend
    if trace_id:
        entry["trace_id"] = trace_id

    try:
        path = _cost_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logger.warning("Failed to write LLM cost log: %s", e)
        return  # don't publish if we couldn't persist

    # Best-effort publish to the dashboard event bus. The Costs panel
    # picks this up and refreshes (subject to the smart-refresh policy).
    try:
        from work_buddy.dashboard.events import publish_auto
        publish_auto("llm.call_logged", {
            "model": entry["model"],
            "task_id": entry["task_id"],
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            "estimated_cost_usd": entry["estimated_cost_usd"],
            "execution_mode": entry["execution_mode"],
            "cached": entry["cached"],
        })
    except Exception:
        pass  # never let event publish hurt the call-logging path

    # First-class inference provenance — a SEPARATE record beside this cost
    # ledger entry (cost stays authoritative for $; provenance answers "what
    # for"). Every completion path funnels through log_call, so emitting here
    # gives universal coverage without threading through each backend. The
    # call_id + detail are read from the ambient call context (bound by
    # run_task / with_tools); call_site is derived from the caller chain.
    try:
        from work_buddy.llm.provenance import record_inference_call
        record_inference_call(
            kind="completion",
            model=model,
            provider=backend or ("cloud" if execution_mode == "cloud" else "local"),
            execution_mode=execution_mode,
            status="cached" if cached else "ok",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            task_id=task_id,
            trace_id=trace_id,
        )
    except Exception:
        pass  # provenance is best-effort; never affects the cost path


def session_total() -> dict:
    """Summarize costs for the current session."""
    entries = _read_log()
    if not entries:
        return {
            "total_calls": 0, "api_calls": 0, "cache_hits": 0,
            "total_input_tokens": 0, "total_output_tokens": 0,
            "total_cache_read_tokens": 0, "total_cache_creation_tokens": 0,
            "estimated_cost_usd": 0,
        }

    total_calls = len(entries)
    cache_hits = sum(1 for e in entries if e.get("cached"))
    api_calls = total_calls - cache_hits
    total_input = sum(e.get("input_tokens", 0) for e in entries)
    total_output = sum(e.get("output_tokens", 0) for e in entries)
    total_cache_read = sum(e.get("cache_read_tokens", 0) for e in entries)
    total_cache_create = sum(e.get("cache_creation_tokens", 0) for e in entries)
    total_cost = sum(e.get("estimated_cost_usd", 0) for e in entries)

    return {
        "total_calls": total_calls,
        "api_calls": api_calls,
        "cache_hits": cache_hits,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_creation_tokens": total_cache_create,
        "estimated_cost_usd": round(total_cost, 6),
    }


def session_breakdown() -> dict[str, Any]:
    """Break down costs by task prefix and model.

    Returns a structured summary showing where costs are coming from:
    per-task totals, per-model totals, and the most expensive callers.
    """
    entries = _read_log()
    if not entries:
        return {"by_task": {}, "by_model": {}, "top_callers": [], "total": session_total()}

    # Aggregate by task_id prefix (e.g., "chrome_infer:batch" → "chrome_infer")
    by_task: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "api_calls": 0, "cache_hits": 0,
        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
    })
    for e in entries:
        task_prefix = e.get("task_id", "unknown").split(":")[0]
        t = by_task[task_prefix]
        t["calls"] += 1
        if e.get("cached"):
            t["cache_hits"] += 1
        else:
            t["api_calls"] += 1
        t["input_tokens"] += e.get("input_tokens", 0)
        t["output_tokens"] += e.get("output_tokens", 0)
        t["cost_usd"] += e.get("estimated_cost_usd", 0)

    # Round costs
    for t in by_task.values():
        t["cost_usd"] = round(t["cost_usd"], 6)

    # Aggregate by model
    by_model: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
    })
    for e in entries:
        model = e.get("model", "unknown")
        m = by_model[model]
        m["calls"] += 1
        m["input_tokens"] += e.get("input_tokens", 0)
        m["output_tokens"] += e.get("output_tokens", 0)
        m["cost_usd"] += e.get("estimated_cost_usd", 0)

    for m in by_model.values():
        m["cost_usd"] = round(m["cost_usd"], 6)

    # Top callers by cost
    caller_costs: dict[str, float] = defaultdict(float)
    for e in entries:
        caller = e.get("caller", [])
        if caller:
            caller_costs[caller[0]] += e.get("estimated_cost_usd", 0)

    top_callers = sorted(
        [{"caller": k, "cost_usd": round(v, 6)} for k, v in caller_costs.items()],
        key=lambda x: x["cost_usd"],
        reverse=True,
    )[:10]

    return {
        "by_task": dict(by_task),
        "by_model": dict(by_model),
        "top_callers": top_callers,
        "total": session_total(),
    }
