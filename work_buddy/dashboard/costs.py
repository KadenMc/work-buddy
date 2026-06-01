"""Cost aggregation for the dashboard Costs tab.

Reads first-party LLM cost log files at ``<data_root>/agents/<session>/llm_costs.jsonl``
(written by :mod:`work_buddy.llm.cost`) and produces summary read models for
the Costs UI: totals, daily series, per-model, per-backend, per-task, and
per-session breakdowns.

This is the **Phase 1** data source — it captures every LLM call routed
through ``work_buddy.llm.runner`` (cloud + local profile). Phase 2 adds
a second source for Claude Code transcript-derived usage via the
vendored ``claude_usage_scanner`` module; both feed into the same read
model surfaced at ``GET /api/costs``.

Cache hits (``cached: true``) and local executions (``execution_mode: "local"``)
are logged with ``estimated_cost_usd: 0.0`` upstream — we preserve that
choice and surface them in their own counters so the UI can show "real
spend" separately from "model usage."
"""

from __future__ import annotations

import functools
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from work_buddy.paths import data_dir

logger = logging.getLogger(__name__)


_AGENTS_DIR = data_dir("agents")


# Pricing comes from the canonical table at
# ``work_buddy.llm.claude_code_usage.pricing`` — both the per-call writer
# (``work_buddy.llm.cost``) and this dashboard aggregator now share one
# rate source. Re-estimation here only happens when a row is missing
# ``estimated_cost_usd`` (rare; written for every modern row).


# ---------------------------------------------------------------------------
# JSONL ingestion
# ---------------------------------------------------------------------------


def _iter_session_dirs(agents_dir: Path | None = None) -> Iterable[Path]:
    """Yield each session directory under ``<data_root>/agents/``."""
    root = agents_dir or _AGENTS_DIR
    if not root.exists():
        return
    for entry in root.iterdir():
        if entry.is_dir():
            yield entry


def _read_session_manifest(session_dir: Path) -> dict[str, Any]:
    """Best-effort read of ``manifest.json``. Empty dict on failure."""
    path = session_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("manifest.json unreadable at %s: %s", path, exc)
        return {}


def _iter_cost_entries(session_dir: Path) -> Iterable[dict[str, Any]]:
    """Yield every parseable JSON record from one session's cost log.

    Tolerates partial-line corruption: bad lines are skipped silently
    rather than aborting the whole file.
    """
    path = session_dir / "llm_costs.jsonl"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        logger.debug("llm_costs.jsonl unreadable at %s: %s", path, exc)


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse a timestamp string. Tolerates ISO 8601 with or without timezone."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _entry_day(entry: dict[str, Any]) -> str:
    """Extract the ``YYYY-MM-DD`` day for an entry. Empty string on failure."""
    dt = _parse_ts(entry.get("timestamp"))
    return dt.strftime("%Y-%m-%d") if dt else ""


def _entry_cost(entry: dict[str, Any]) -> float:
    """Cost for a log entry. Re-estimates via the canonical table if missing.

    Used as a fallback only — rows written by current ``log_call`` always
    carry ``estimated_cost_usd``. The re-estimation path matters for any
    hand-edited / migrated rows that lose the field.
    """
    cost = entry.get("estimated_cost_usd")
    if cost is not None:
        return float(cost)
    if entry.get("cached") or entry.get("execution_mode") == "local":
        return 0.0
    from work_buddy.llm.claude_code_usage.pricing import calc_cost
    return calc_cost(
        entry.get("model", ""),
        int(entry.get("input_tokens", 0)),
        int(entry.get("output_tokens", 0)),
        int(entry.get("cache_read_tokens") or 0),
        int(entry.get("cache_creation_tokens") or 0),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _empty_totals() -> dict[str, Any]:
    return {
        "calls": 0,
        "api_calls": 0,
        "cache_hits": 0,
        "cloud_calls": 0,
        "local_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.0,
    }


def _accumulate(bucket: dict[str, Any], entry: dict[str, Any]) -> None:
    """Mutate ``bucket`` (an _empty_totals dict) with ``entry``'s contribution."""
    bucket["calls"] += 1
    # Cloud/local is the canonical execution-mode taxonomy. Anything missing
    # the field defaults to cloud (see the comment in get_costs_summary).
    mode = entry.get("execution_mode") or "cloud"
    if mode == "local":
        bucket["local_calls"] += 1
    else:
        bucket["cloud_calls"] += 1
    if entry.get("cached"):
        bucket["cache_hits"] += 1
    else:
        bucket["api_calls"] += 1
    bucket["input_tokens"] += int(entry.get("input_tokens", 0))
    bucket["output_tokens"] += int(entry.get("output_tokens", 0))
    # cache_read_tokens / cache_creation_tokens were added 2026-04-25.
    # Older rows lack the fields → ``int(...or 0)`` covers both missing
    # and explicit-None cases.
    bucket["cache_read_tokens"] += int(entry.get("cache_read_tokens") or 0)
    bucket["cache_creation_tokens"] += int(entry.get("cache_creation_tokens") or 0)
    bucket["cost_usd"] += _entry_cost(entry)


def _round_cost(bucket: dict[str, Any]) -> dict[str, Any]:
    bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    return bucket


@functools.lru_cache(maxsize=8192)
def _resolve_project_name(path_or_slug: str) -> str:
    """Canonical project name for a cwd / project path.

    Memoized: pure mapping from a cwd/slug to a canonical name, called
    once per usage turn during cost aggregation (the same handful of
    project paths recur across hundreds of thousands of rows).

    Reuses :func:`work_buddy.collectors.chat_collector.project_name_from_slug`
    so the Costs tab matches the Chats tab — worktrees and nested
    feature directories collapse to their parent project (e.g.
    ``electricrag-fg-clep`` → ``electricrag``).

    The chat collector's function expects a Claude-Code-style slug
    (``C--repo-foo``); here we accept either a slug or a full path and
    convert the path form to slug form first.
    """
    if not path_or_slug:
        return ""
    try:
        from work_buddy.collectors.chat_collector import project_name_from_slug
    except Exception:
        # Fallback: just return the last path component.
        return (path_or_slug.replace("\\", "/").rstrip("/").split("/")[-1]
                or path_or_slug)
    s = path_or_slug
    # Path → slug: replace separators and the drive-letter colon with '-'
    if "\\" in s or "/" in s or ":" in s:
        s = s.replace("\\", "-").replace("/", "-").replace(":", "-")
    return project_name_from_slug(s)


def _project_matches(session_project: str, project_filter: str | None) -> bool:
    """Match the user's project filter against a session's project path.

    Filter values come from the dropdown, which is populated with
    canonical project names (resolved via :func:`_resolve_project_name`).
    To make filtering work on freshly-scanned data without requiring a
    full rescan of historical records, we resolve the session's project
    on-the-fly and compare the canonical name. Falls back to substring
    match against the raw path if resolution fails.
    """
    if not project_filter:
        return True
    if not session_project:
        return False
    pf = project_filter.lower()
    canonical = _resolve_project_name(session_project).lower()
    if pf == canonical:
        return True
    sp = session_project.lower().replace("\\", "/")
    last = sp.rstrip("/").split("/")[-1] if sp else ""
    return pf in canonical or pf in sp or pf in last


def get_costs_summary(
    *,
    agents_dir: Path | None = None,
    project: str | None = None,
    execution_mode: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    models: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Return the full cost summary read model.

    Top-level shape::

        {
          "generated_at": "2026-04-25T03:14:00",
          "totals":      {... _empty_totals shape ...},
          "by_day":      [{"day": "YYYY-MM-DD", ... _empty_totals ...}, ...],
          "by_model":    [{"model": "...",     ... _empty_totals ...}, ...],
          "by_backend":  [{"backend": "...",   ... _empty_totals ...}, ...],
          "by_task":     [{"task": "...",      ... _empty_totals ...}, ...],
          "by_execution_mode": [{"mode": "cloud"|"local", ... _empty_totals ...}, ...],
          "sessions":    [{"session_id": "...", "short_id": "...",
                           "project": "...", "first": "...", "last": "...",
                           "models": [...], ... _empty_totals ...}, ...],
          "all_models":  ["model-name", ...],          # sorted by token volume desc
          "source":      "work_buddy_internal",        # ``claude_code`` is the parallel source
          "session_count": int,
          "log_files_seen": int,
          "log_files_parsed": int,
        }

    Args:
        agents_dir: Override the default ``<data_root>/agents/`` location.
        project: Optional substring filter. Matched against each session's
            canonical project name (resolved via the same logic the Chats
            tab uses) and falls back to substring against the raw path.
        start_date: Optional ``"YYYY-MM-DD"`` lower bound (inclusive).
            Rows whose ``timestamp[:10]`` is before this are excluded
            from every aggregate. ``None`` = no lower bound.
        end_date: Optional ``"YYYY-MM-DD"`` upper bound (inclusive).
            Same shape as ``start_date``. ``None`` = no upper bound.
        models: Optional iterable of model names. When non-empty, only
            rows whose ``model`` is in the set are included in every
            aggregate. ``None`` or empty = no filter. Same use case as
            ``start_date``/``end_date``: keeps cards/charts/tables in
            sync when the user narrows via the chip filter.
        execution_mode: Optional row-level filter. ``"cloud"`` = only
            ``execution_mode == "cloud"`` rows; ``"local"`` = only local;
            ``None`` / ``"all"`` = no filter. Filters apply at row
            granularity, so by_model / by_day / sessions / etc. all
            reflect the filtered slice.

    Costs are pre-summed per bucket; the frontend can derive percentages
    or filtered slices client-side without a second round trip.
    """
    mode_filter = (execution_mode or "").lower()
    if mode_filter not in ("", "all", "cloud", "local"):
        mode_filter = ""
    # ``None`` = no filter; empty set = filter to nothing (every row drops).
    # The empty case matters: the chip rail lets the user de-select every
    # model, and we want cards / charts / sessions to reflect that
    # explicitly rather than silently fall back to all-time.
    model_filter = set(models) if models is not None else None
    totals = _empty_totals()
    by_day: dict[str, dict[str, Any]] = defaultdict(_empty_totals)
    by_model: dict[str, dict[str, Any]] = defaultdict(_empty_totals)
    by_backend: dict[str, dict[str, Any]] = defaultdict(_empty_totals)
    by_task: dict[str, dict[str, Any]] = defaultdict(_empty_totals)
    by_mode: dict[str, dict[str, Any]] = defaultdict(_empty_totals)
    sessions: list[dict[str, Any]] = []

    log_files_seen = 0
    log_files_parsed = 0

    for session_dir in _iter_session_dirs(agents_dir):
        log_path = session_dir / "llm_costs.jsonl"
        if not log_path.exists():
            continue
        log_files_seen += 1

        manifest = _read_session_manifest(session_dir)
        sess_id = manifest.get("session_id") or session_dir.name
        short_id = manifest.get("short_id") or sess_id[:8]
        sess_project = manifest.get("project") or ""

        # Drop sessions outside the project filter before parsing the log.
        if not _project_matches(sess_project, project):
            continue

        sess_bucket = _empty_totals()
        sess_models: set[str] = set()
        sess_first_ts: str = ""
        sess_last_ts: str = ""

        had_any_entry = False
        for entry in _iter_cost_entries(session_dir):
            # Defaults match the aggregator: missing → cloud.
            entry_mode = entry.get("execution_mode") or "cloud"
            if mode_filter in ("cloud", "local") and entry_mode != mode_filter:
                continue
            # Date-range filter at row level so every aggregate
            # (totals, by_model, by_task, by_backend, sessions, ...)
            # respects the same window. Without this, the cards (which
            # used to slice ``by_day`` client-side) disagreed with
            # ``by_task`` and ``by_model`` (still all-time).
            if start_date or end_date:
                day_str = _entry_day(entry)
                if day_str:
                    if start_date and day_str < start_date:
                        continue
                    if end_date and day_str > end_date:
                        continue
            # Model filter at row level — same rationale as the
            # date-range filter: keeps cards/charts/tables in sync
            # when the user narrows via the chip filter.
            if model_filter is not None:
                if (entry.get("model") or "") not in model_filter:
                    continue
            had_any_entry = True
            ts = entry.get("timestamp", "") or ""
            if not sess_first_ts or ts < sess_first_ts:
                sess_first_ts = ts
            if not sess_last_ts or ts > sess_last_ts:
                sess_last_ts = ts

            day = _entry_day(entry)
            model = entry.get("model", "unknown") or "unknown"
            backend = entry.get("backend") or "unknown"
            task_full = entry.get("task_id", "unknown") or "unknown"
            task_prefix = task_full.split(":", 1)[0]
            # Default missing execution_mode to "cloud" — verified safe by
            # the 2026-04-25 audit + scripts/backfill_execution_mode.py.
            # The legacy gap (~2026-04-06 to 2026-04-12) only covered
            # Anthropic API rows; ``log_call`` has defaulted ``cloud``
            # since the field landed, so new rows always carry it.
            mode = entry.get("execution_mode") or "cloud"

            sess_models.add(model)
            _accumulate(totals, entry)
            _accumulate(sess_bucket, entry)
            if day:
                _accumulate(by_day[day], entry)
            _accumulate(by_model[model], entry)
            _accumulate(by_backend[backend], entry)
            _accumulate(by_task[task_prefix], entry)
            _accumulate(by_mode[mode], entry)

        if had_any_entry:
            log_files_parsed += 1
            sessions.append({
                "session_id": sess_id,
                "short_id": short_id,
                "project": sess_project,
                "directory": session_dir.name,
                "first": sess_first_ts,
                "last": sess_last_ts,
                "models": sorted(sess_models),
                **_round_cost(dict(sess_bucket)),
            })

    sessions.sort(key=lambda s: s.get("last") or "", reverse=True)

    by_day_list = sorted(
        ({"day": k, **_round_cost(dict(v))} for k, v in by_day.items()),
        key=lambda r: r["day"],
    )
    by_model_list = sorted(
        ({"model": k, **_round_cost(dict(v))} for k, v in by_model.items()),
        key=lambda r: (r["input_tokens"] + r["output_tokens"]),
        reverse=True,
    )
    by_backend_list = sorted(
        ({"backend": k, **_round_cost(dict(v))} for k, v in by_backend.items()),
        key=lambda r: r["calls"],
        reverse=True,
    )
    by_task_list = sorted(
        ({"task": k, **_round_cost(dict(v))} for k, v in by_task.items()),
        key=lambda r: r["cost_usd"],
        reverse=True,
    )
    by_mode_list = sorted(
        ({"mode": k, **_round_cost(dict(v))} for k, v in by_mode.items()),
        key=lambda r: r["calls"],
        reverse=True,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": _round_cost(totals),
        "by_day": by_day_list,
        "by_model": by_model_list,
        "by_backend": by_backend_list,
        "by_task": by_task_list,
        "by_execution_mode": by_mode_list,
        "sessions": sessions,
        "all_models": [r["model"] for r in by_model_list],
        "source": "work_buddy_internal",
        "session_count": len(sessions),
        "log_files_seen": log_files_seen,
        "log_files_parsed": log_files_parsed,
    }
