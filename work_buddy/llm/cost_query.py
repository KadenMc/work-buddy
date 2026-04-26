"""Unified cost-query interface — single read API for both data sources.

Backs the ``llm_costs_query`` MCP capability and the dashboard's
``/api/costs`` route. The two underlying sources stay independent —
``work_buddy.dashboard.costs`` for the per-call internal log, and
``work_buddy.llm.claude_code_usage.aggregator`` for the transcript
cache — but this module gives callers one shape with smart parameters
to slice across them.

The two sources are **complementary, not overlapping**:

* The internal log captures every LLM call work-buddy itself makes
  through its runner (cloud + local).
* The Claude-Code-usage source captures every Claude Code session on
  the machine (any project, any launcher), all cloud.

So when ``source="all"``, summing the two is honest — no de-dup is
required.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------

# Map window labels to a (start_offset_days, end_offset_days) pair. ``None``
# in the start position means "no lower bound" (used by ``"all"``).
_NAMED_WINDOWS: dict[str, tuple[int | None, int]] = {
    "today":          (0, 0),
    "yesterday":      (1, 1),
    "7d":             (6, 0),
    "30d":            (29, 0),
    "90d":            (89, 0),
    "month_to_date":  (None, 0),  # special-cased below
    "all":            (None, 0),
}


def _resolve_window(window: str) -> dict[str, Any]:
    """Resolve ``window`` to absolute ISO bounds + a label + days span.

    Accepts:
      * named: ``today | yesterday | 7d | 30d | 90d | month_to_date | all``
      * single-day: ``YYYY-MM-DD`` (start of that day → end of that day)
      * range:      ``YYYY-MM-DD..YYYY-MM-DD`` (inclusive on both ends)
    """
    now = datetime.now(timezone.utc)

    # Range form
    range_match = re.match(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$", window)
    if range_match:
        s, e = range_match.groups()
        return _ymd_window(s, e, label=window)

    # Single-day form
    if re.match(r"^\d{4}-\d{2}-\d{2}$", window):
        return _ymd_window(window, window, label=window)

    if window not in _NAMED_WINDOWS:
        raise ValueError(
            f"Unknown window {window!r}. Expected named "
            f"({', '.join(sorted(_NAMED_WINDOWS))}), 'YYYY-MM-DD' "
            f"single-day, or 'YYYY-MM-DD..YYYY-MM-DD' range.",
        )

    if window == "all":
        return {
            "start": "",
            "end": now.strftime("%Y-%m-%d"),
            "label": "all",
            "days": None,
            "_dt_start": None,
            "_dt_end": now,
        }

    if window == "month_to_date":
        first_of_month = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        return {
            "start": first_of_month.strftime("%Y-%m-%d"),
            "end": now.strftime("%Y-%m-%d"),
            "label": "month_to_date",
            "days": (now - first_of_month).days + 1,
            "_dt_start": first_of_month,
            "_dt_end": now,
        }

    start_offset, end_offset = _NAMED_WINDOWS[window]
    end_dt = now - timedelta(days=end_offset)
    start_dt = now - timedelta(days=start_offset or 0)
    return {
        "start": start_dt.strftime("%Y-%m-%d"),
        "end": end_dt.strftime("%Y-%m-%d"),
        "label": window,
        "days": (start_offset or 0) - end_offset + 1,
        "_dt_start": start_dt.replace(hour=0, minute=0, second=0, microsecond=0),
        "_dt_end": end_dt.replace(hour=23, minute=59, second=59, microsecond=999999),
    }


def _ymd_window(start_ymd: str, end_ymd: str, *, label: str) -> dict[str, Any]:
    s_dt = datetime.strptime(start_ymd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    e_dt = datetime.strptime(end_ymd, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc,
    )
    return {
        "start": start_ymd,
        "end": end_ymd,
        "label": label,
        "days": (e_dt.date() - s_dt.date()).days + 1,
        "_dt_start": s_dt,
        "_dt_end": e_dt,
    }


def _previous_window(window_info: dict[str, Any]) -> dict[str, Any] | None:
    """Compute the immediately-preceding equally-sized window for comparison.

    Returns ``None`` when the input has no lower bound (``all``).
    """
    s_dt = window_info.get("_dt_start")
    e_dt = window_info.get("_dt_end")
    if s_dt is None or e_dt is None:
        return None
    span = e_dt - s_dt
    new_end = s_dt - timedelta(microseconds=1)
    new_start = new_end - span
    return {
        "start": new_start.strftime("%Y-%m-%d"),
        "end": new_end.strftime("%Y-%m-%d"),
        "label": "previous",
        "days": (new_end.date() - new_start.date()).days + 1,
        "_dt_start": new_start,
        "_dt_end": new_end,
    }


# ---------------------------------------------------------------------------
# Source readers — map raw read models into a uniform per-row stream
# ---------------------------------------------------------------------------


def _empty_totals() -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.0,
        "calls_by_source": {
            "claude_code_transcripts": 0,
            "work_buddy_internal_cloud": 0,
            "work_buddy_internal_local": 0,
        },
    }


def _add_row(
    bucket: dict[str, Any],
    *,
    source_bucket: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: float = 0.0,
    calls_or_turns: int = 1,
) -> None:
    bucket["calls"] += calls_or_turns
    bucket["input_tokens"] += int(input_tokens)
    bucket["output_tokens"] += int(output_tokens)
    bucket["cache_read_tokens"] += int(cache_read_tokens)
    bucket["cache_creation_tokens"] += int(cache_creation_tokens)
    bucket["cost_usd"] += float(cost_usd)
    bucket["calls_by_source"][source_bucket] = (
        bucket["calls_by_source"].get(source_bucket, 0) + calls_or_turns
    )


def _round_cost(d: dict[str, Any]) -> dict[str, Any]:
    if "cost_usd" in d:
        d["cost_usd"] = round(d["cost_usd"], 6)
    return d


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------


def _within_window(ts: str, window_info: dict[str, Any]) -> bool:
    s = window_info.get("_dt_start")
    e = window_info.get("_dt_end")
    if not ts:
        return s is None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return s is None
    if s is not None and dt < s:
        return False
    if e is not None and dt > e:
        return False
    return True


# ---------------------------------------------------------------------------
# Main query entry
# ---------------------------------------------------------------------------


def llm_costs_query(
    *,
    window: str = "30d",
    group_by: str | None = None,
    source: str = "all",
    min_cost: float = 0.0,
    project: str | None = None,
    model: str | None = None,
    top_n: int = 10,
    include_local: bool = True,
    compare_to_previous: bool = True,
) -> dict[str, Any]:
    """Aggregate LLM cost / usage across one or both data sources.

    See module docstring for source semantics. Returned shape::

        {
          "window":   {"start": ISO-date, "end": ISO-date, "label": str, "days": int|None},
          "source":   "all" | "internal" | "claude_code",
          "totals":   {... uniform totals (calls, tokens, cost, calls_by_source) ...},
          "groups":   [...]                   # populated only when group_by != None
          "comparison": {                     # populated only when compare_to_previous
              "previous_window": {...},
              "previous_totals": {...},
              "delta_pct_cost":   float,
              "delta_pct_calls":  float,
              "is_higher":        bool,
          },
          "warnings": [str, ...],
          "filters_applied": {... echo of input args ...},
        }
    """
    # Backwards-compat — old "transcripts" name still routes to claude_code.
    if source == "transcripts":
        source = "claude_code"
    if source not in {"all", "internal", "claude_code"}:
        raise ValueError(f"Unknown source {source!r}.")

    if group_by not in {None, "project", "model", "session", "day", "tool"}:
        raise ValueError(f"Unknown group_by {group_by!r}.")

    window_info = _resolve_window(window)
    warnings: list[str] = []

    totals = _empty_totals()
    groups: dict[str, dict[str, Any]] = {}

    # Pull from the chosen source(s) and feed a uniform per-row stream.
    if source in {"all", "internal"}:
        _accumulate_internal(
            totals=totals, groups=groups, group_by=group_by,
            window_info=window_info, project=project, model=model,
            min_cost=min_cost, include_local=include_local,
        )

    if source in {"all", "claude_code"}:
        _accumulate_claude_code(
            totals=totals, groups=groups, group_by=group_by,
            window_info=window_info, project=project, model=model,
            min_cost=min_cost, warnings=warnings,
        )

    # Rank and trim grouped output.
    group_list: list[dict[str, Any]] = []
    if group_by is not None:
        sortable = [_round_cost(dict(v)) for v in groups.values()]
        # Sessions sort by recency; everything else by cost desc.
        if group_by == "session":
            sortable.sort(key=lambda r: r.get("last") or "", reverse=True)
        else:
            sortable.sort(key=lambda r: r["cost_usd"], reverse=True)
        if top_n and top_n > 0:
            group_list = sortable[:top_n]
        else:
            group_list = sortable

    out: dict[str, Any] = {
        "window": {k: v for k, v in window_info.items() if not k.startswith("_")},
        "source": source,
        "totals": _round_cost(totals),
        "groups": group_list,
        "warnings": warnings,
        "filters_applied": {
            "window": window, "group_by": group_by, "source": source,
            "min_cost": min_cost, "project": project, "model": model,
            "top_n": top_n, "include_local": include_local,
            "compare_to_previous": compare_to_previous,
        },
    }

    if compare_to_previous:
        prev = _previous_window(window_info)
        if prev is None:
            out["comparison"] = None
        else:
            prev_totals = _empty_totals()
            prev_groups: dict[str, dict[str, Any]] = {}
            if source in {"all", "internal"}:
                _accumulate_internal(
                    totals=prev_totals, groups=prev_groups, group_by=None,
                    window_info=prev, project=project, model=model,
                    min_cost=min_cost, include_local=include_local,
                )
            if source in {"all", "claude_code"}:
                _accumulate_claude_code(
                    totals=prev_totals, groups=prev_groups, group_by=None,
                    window_info=prev, project=project, model=model,
                    min_cost=min_cost, warnings=warnings,
                )
            out["comparison"] = {
                "previous_window": {
                    k: v for k, v in prev.items() if not k.startswith("_")
                },
                "previous_totals": _round_cost(prev_totals),
                "delta_pct_cost": _pct_delta(
                    totals["cost_usd"], prev_totals["cost_usd"],
                ),
                "delta_pct_calls": _pct_delta(
                    totals["calls"], prev_totals["calls"],
                ),
                "is_higher": totals["cost_usd"] > prev_totals["cost_usd"],
            }

    return out


def _pct_delta(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round(((current - previous) / previous) * 100.0, 2)


# ---------------------------------------------------------------------------
# Source-specific accumulators
# ---------------------------------------------------------------------------


def _accumulate_internal(
    *,
    totals: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    group_by: str | None,
    window_info: dict[str, Any],
    project: str | None,
    model: str | None,
    min_cost: float,
    include_local: bool,
) -> None:
    """Walk the per-call internal log and feed buckets."""
    from work_buddy.dashboard.costs import _iter_cost_entries, _iter_session_dirs
    from work_buddy.dashboard.costs import _read_session_manifest

    for session_dir in _iter_session_dirs():
        manifest = _read_session_manifest(session_dir)
        sess_project = (manifest.get("project") or "").replace("\\", "/").rstrip("/")
        sess_short = sess_project.split("/")[-1] if sess_project else ""
        if project and project not in sess_project and project not in sess_short:
            continue

        sess_id = manifest.get("session_id") or session_dir.name
        sess_short_id = manifest.get("short_id") or sess_id[:8]

        for entry in _iter_cost_entries(session_dir):
            if not _within_window(entry.get("timestamp", ""), window_info):
                continue
            entry_model = entry.get("model", "") or ""
            if model and entry_model != model:
                continue
            mode = entry.get("execution_mode") or "cloud"
            if mode == "local" and not include_local:
                continue
            cost = float(entry.get("estimated_cost_usd") or 0.0)
            if cost < min_cost:
                continue

            source_bucket = (
                "work_buddy_internal_local" if mode == "local"
                else "work_buddy_internal_cloud"
            )
            row_kwargs = dict(
                source_bucket=source_bucket,
                input_tokens=entry.get("input_tokens", 0),
                output_tokens=entry.get("output_tokens", 0),
                cache_read_tokens=entry.get("cache_read_tokens") or 0,
                cache_creation_tokens=entry.get("cache_creation_tokens") or 0,
                cost_usd=cost,
                calls_or_turns=1,
            )
            _add_row(totals, **row_kwargs)

            if group_by is None:
                continue

            key = _group_key_internal(
                group_by, entry, session_dir.name, sess_short, sess_id,
                sess_short_id, mode,
            )
            bucket = groups.setdefault(key, _group_empty(group_by))
            bucket["key"] = key
            _add_row(bucket, **row_kwargs)
            if group_by == "session":
                # Update last-seen timestamp for session ranking by recency.
                ts = entry.get("timestamp", "") or ""
                if ts > (bucket.get("last") or ""):
                    bucket["last"] = ts


def _accumulate_claude_code(
    *,
    totals: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    group_by: str | None,
    window_info: dict[str, Any],
    project: str | None,
    model: str | None,
    min_cost: float,
    warnings: list[str],
) -> None:
    """Walk the Claude-Code-usage SQLite cache and feed buckets."""
    import sqlite3
    from work_buddy.llm.claude_code_usage import scanner as _scanner
    from work_buddy.llm.claude_code_usage.pricing import calc_cost

    db_path = _scanner.get_db_path()
    if not db_path.exists():
        warnings.append(
            "claude_code source requested but the cache is empty. "
            "Trigger claude_code_usage_scan to populate.",
        )
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute("""
            SELECT timestamp, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens, tool_name, cwd,
                   session_id
            FROM turns
        """)
        for row in cursor:
            ts = row["timestamp"] or ""
            if not _within_window(ts, window_info):
                continue
            row_model = row["model"] or ""
            if model and row_model != model:
                continue

            cwd = (row["cwd"] or "").replace("\\", "/").rstrip("/")
            row_project = cwd.split("/")[-1] if cwd else "unknown"
            if project and project not in cwd and project not in row_project:
                continue

            cost = calc_cost(
                row_model,
                int(row["input_tokens"] or 0),
                int(row["output_tokens"] or 0),
                int(row["cache_read_tokens"] or 0),
                int(row["cache_creation_tokens"] or 0),
            )
            if cost < min_cost:
                continue

            row_kwargs = dict(
                source_bucket="claude_code_transcripts",
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
                cache_read_tokens=row["cache_read_tokens"] or 0,
                cache_creation_tokens=row["cache_creation_tokens"] or 0,
                cost_usd=cost,
                calls_or_turns=1,
            )
            _add_row(totals, **row_kwargs)

            if group_by is None:
                continue

            key = _group_key_claude_code(group_by, row, row_project)
            bucket = groups.setdefault(key, _group_empty(group_by))
            bucket["key"] = key
            _add_row(bucket, **row_kwargs)
            if group_by == "session":
                if ts > (bucket.get("last") or ""):
                    bucket["last"] = ts
    finally:
        conn.close()


def _group_empty(group_by: str) -> dict[str, Any]:
    base = _empty_totals()
    base["key"] = ""
    if group_by == "session":
        base["last"] = ""
    return base


def _group_key_internal(
    group_by: str,
    entry: dict[str, Any],
    session_dir_name: str,
    sess_short: str,
    sess_id: str,
    sess_short_id: str,
    mode: str,
) -> str:
    if group_by == "model":
        return entry.get("model", "") or "unknown"
    if group_by == "session":
        return sess_short_id
    if group_by == "project":
        return sess_short or "unknown"
    if group_by == "day":
        return (entry.get("timestamp", "") or "")[:10] or "unknown"
    if group_by == "tool":
        # Internal log doesn't track tool_name; use task_id prefix as proxy.
        task = entry.get("task_id", "") or ""
        return task.split(":", 1)[0] or "unknown"
    return "unknown"


def _group_key_claude_code(
    group_by: str,
    row: Any,
    row_project: str,
) -> str:
    if group_by == "model":
        return row["model"] or "unknown"
    if group_by == "session":
        return (row["session_id"] or "")[:8] or "unknown"
    if group_by == "project":
        return row_project
    if group_by == "day":
        return (row["timestamp"] or "")[:10] or "unknown"
    if group_by == "tool":
        return row["tool_name"] or "(no tool)"
    return "unknown"
