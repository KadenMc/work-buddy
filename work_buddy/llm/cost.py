"""Append-only LLM cost log with caller tracing and cost breakdown.

Writes to ``agents/<session>/llm_costs.jsonl`` — one JSON object per line.
Each entry records: model, tokens, cost, task_id, caller chain, trace_id,
and cache status. Provides session-level totals and per-task breakdowns.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Approximate cost per 1M tokens (as of 2026-04)
_COST_PER_M_TOKENS: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
}


def _cost_log_path() -> Path:
    from work_buddy.agent_session import get_session_dir

    session_dir = get_session_dir()
    return session_dir / "llm_costs.jsonl"


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD."""
    rates = _COST_PER_M_TOKENS.get(model, {"input": 1.0, "output": 5.0})
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


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
) -> None:
    """Append a cost entry to the session log.

    Args:
        model: Model name used.
        input_tokens: Input token count.
        output_tokens: Output token count.
        task_id: Identifier for the task (e.g., "chrome_infer:batch").
        trace_id: Optional UUID linking related calls in a single invocation.
        cached: If True, this was a cache hit (no API call, zero cost).
    """
    entry: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "task_id": task_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": 0.0 if cached else round(
            _estimate_cost(model, input_tokens, output_tokens), 6,
        ),
        "cached": cached,
        "caller": _get_caller_chain(),
    }
    if trace_id:
        entry["trace_id"] = trace_id

    try:
        path = _cost_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logger.warning("Failed to write LLM cost log: %s", e)


def session_total() -> dict:
    """Summarize costs for the current session."""
    entries = _read_log()
    if not entries:
        return {
            "total_calls": 0, "api_calls": 0, "cache_hits": 0,
            "total_input_tokens": 0, "total_output_tokens": 0,
            "estimated_cost_usd": 0,
        }

    total_calls = len(entries)
    cache_hits = sum(1 for e in entries if e.get("cached"))
    api_calls = total_calls - cache_hits
    total_input = sum(e.get("input_tokens", 0) for e in entries)
    total_output = sum(e.get("output_tokens", 0) for e in entries)
    total_cost = sum(e.get("estimated_cost_usd", 0) for e in entries)

    return {
        "total_calls": total_calls,
        "api_calls": api_calls,
        "cache_hits": cache_hits,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
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
