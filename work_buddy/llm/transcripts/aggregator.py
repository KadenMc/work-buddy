"""Read the transcript SQLite cache into the shape the Costs tab expects.

The shape mirrors :func:`work_buddy.dashboard.costs.get_costs_summary`'s
top-level structure so the frontend can render either source through
the same renderer with only a per-row remap. The vocabulary differs in
two places that the consumer has to handle deliberately:

* The transcript view records **cache_read_tokens** and
  **cache_creation_tokens** as first-class fields. The internal log
  collapses both into a single ``cache_hits`` boolean. The transcript
  read model exposes them separately so the UI can show the cache-read
  discount at work.
* The transcript view computes cost from the richer pricing table at
  :mod:`work_buddy.llm.transcripts.pricing` (cache rates included),
  not the truncated table in :mod:`work_buddy.llm.cost`.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.llm.transcripts import scanner as _scanner
from work_buddy.llm.transcripts.pricing import calc_cost

logger = logging.getLogger(__name__)


def _empty_totals() -> dict[str, Any]:
    return {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.0,
    }


def _add_turn(bucket: dict[str, Any], row: sqlite3.Row | dict) -> None:
    bucket["turns"] += 1
    bucket["input_tokens"] += int(row["input_tokens"] or 0)
    bucket["output_tokens"] += int(row["output_tokens"] or 0)
    bucket["cache_read_tokens"] += int(row["cache_read_tokens"] or 0)
    bucket["cache_creation_tokens"] += int(row["cache_creation_tokens"] or 0)
    bucket["cost_usd"] += calc_cost(
        row["model"], int(row["input_tokens"] or 0),
        int(row["output_tokens"] or 0),
        int(row["cache_read_tokens"] or 0),
        int(row["cache_creation_tokens"] or 0),
    )


def _round(bucket: dict[str, Any]) -> dict[str, Any]:
    bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    return bucket


def get_transcripts_summary(*, db_path: Path | None = None) -> dict[str, Any]:
    """Return the transcript-derived cost / usage read model.

    When the cache DB has not been populated yet, returns
    ``{"available": False, "source": "claude_transcripts", ...}`` so the
    UI can render an explicit "scan to populate" CTA.
    """
    p = db_path or _scanner.get_db_path()
    if not p.exists():
        return {
            "available": False,
            "source": "claude_transcripts",
            "message": ("No transcripts cache yet. Trigger a scan via "
                        "POST /api/costs/rescan or "
                        "wb_run('claude_transcripts_scan')."),
            "db_path": str(p),
        }

    try:
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.warning("transcripts: cannot open cache db at %s: %s", p, exc)
        return {"available": False, "source": "claude_transcripts",
                "error": str(exc), "db_path": str(p)}

    try:
        totals = _empty_totals()
        by_day: dict[str, dict[str, Any]] = defaultdict(_empty_totals)
        by_model: dict[str, dict[str, Any]] = defaultdict(_empty_totals)
        by_tool: dict[str, dict[str, Any]] = defaultdict(_empty_totals)
        by_project: dict[str, dict[str, Any]] = defaultdict(_empty_totals)

        for row in conn.execute("""
            SELECT timestamp, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens, tool_name, cwd,
                   session_id
            FROM turns
        """):
            _add_turn(totals, row)
            day = (row["timestamp"] or "")[:10]
            if day:
                _add_turn(by_day[day], row)
            _add_turn(by_model[row["model"] or "unknown"], row)
            _add_turn(by_tool[row["tool_name"] or "(no tool)"], row)
            project = ((row["cwd"] or "")
                       .replace("\\", "/").rstrip("/").split("/")[-1]
                       or "unknown")
            _add_turn(by_project[project], row)

        sessions = []
        for s in conn.execute("""
            SELECT session_id, project_name, first_timestamp, last_timestamp,
                   git_branch, total_input_tokens, total_output_tokens,
                   total_cache_read, total_cache_creation, model, turn_count
            FROM sessions
            ORDER BY last_timestamp DESC
        """):
            cost = calc_cost(
                s["model"], int(s["total_input_tokens"] or 0),
                int(s["total_output_tokens"] or 0),
                int(s["total_cache_read"] or 0),
                int(s["total_cache_creation"] or 0),
            )
            sessions.append({
                "session_id": s["session_id"],
                "short_id": (s["session_id"] or "")[:8],
                "project": s["project_name"] or "",
                "branch": s["git_branch"] or "",
                "first": s["first_timestamp"] or "",
                "last": s["last_timestamp"] or "",
                "model": s["model"] or "",
                "turns": int(s["turn_count"] or 0),
                "input_tokens": int(s["total_input_tokens"] or 0),
                "output_tokens": int(s["total_output_tokens"] or 0),
                "cache_read_tokens": int(s["total_cache_read"] or 0),
                "cache_creation_tokens": int(s["total_cache_creation"] or 0),
                "cost_usd": round(cost, 6),
            })
    finally:
        conn.close()

    by_day_list = sorted(
        ({"day": k, **_round(dict(v))} for k, v in by_day.items()),
        key=lambda r: r["day"],
    )
    by_model_list = sorted(
        ({"model": k, **_round(dict(v))} for k, v in by_model.items()),
        key=lambda r: r["cost_usd"], reverse=True,
    )
    by_tool_list = sorted(
        ({"tool": k, **_round(dict(v))} for k, v in by_tool.items()),
        key=lambda r: r["turns"], reverse=True,
    )
    by_project_list = sorted(
        ({"project": k, **_round(dict(v))} for k, v in by_project.items()),
        key=lambda r: r["cost_usd"], reverse=True,
    )

    return {
        "available": True,
        "source": "claude_transcripts",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": _round(totals),
        "by_day": by_day_list,
        "by_model": by_model_list,
        "by_tool": by_tool_list,
        "by_project": by_project_list,
        "sessions": sessions,
        "session_count": len(sessions),
        "all_models": [r["model"] for r in by_model_list],
        "db_path": str(p),
    }
