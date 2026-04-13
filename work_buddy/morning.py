"""Morning routine orchestration for work-buddy.

Phase gating (config-driven enable/disable of morning DAG steps) and
briefing content formatting. Journal I/O (sign-in, wellness, briefing
persistence) lives in :mod:`work_buddy.journal`.
"""

import re
from typing import Any

from omegaconf import OmegaConf

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Phase mapping: config snake_case  <->  DAG kebab-case step IDs
# ---------------------------------------------------------------------------

_PHASE_MAP: dict[str, str] = {
    "yesterday_close": "yesterday-close",
    "context_snapshot": "context-snapshot",
    "calendar": "calendar-today",
    "task_briefing": "task-briefing",
    "contract_check": "contract-check",
    "blindspot_scan": "blindspot-scan",
}

# Reverse: step-id -> config key
_STEP_TO_CONFIG: dict[str, str] = {v: k for k, v in _PHASE_MAP.items()}

# These phases cannot be disabled via config
_ALWAYS_ON: set[str] = {"synthesize", "plan-today", "sign-in"}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_morning_config(overrides: list[str] | None = None) -> dict[str, Any]:
    """Load the full config with optional OmegaConf dotlist overrides.

    Same merge strategy as ``collect.py``: base config from YAML/defaults,
    then CLI-style dotlist overrides applied on top.

    Returns:
        The complete config dict (not just the ``morning`` section).
    """
    cfg = load_config()
    if overrides:
        base = OmegaConf.create(cfg)
        cli = OmegaConf.from_dotlist(overrides)
        cfg = OmegaConf.to_container(OmegaConf.merge(base, cli), resolve=True)
    return cfg


def is_phase_enabled(step_id: str, cfg: dict[str, Any]) -> bool:
    """Check whether a morning phase is enabled in the current config.

    ``synthesize`` and ``plan-today`` always return True.

    Accepts both DAG step IDs (kebab-case, e.g. ``"yesterday-close"``) and
    config keys (snake_case, e.g. ``"yesterday_close"``).

    Args:
        step_id: DAG step ID or config key for the phase.
        cfg: Full config dict (as returned by :func:`get_morning_config`).
    """
    if step_id in _ALWAYS_ON:
        return True

    # Try kebab-case step ID first
    config_key = _STEP_TO_CONFIG.get(step_id)
    if config_key is None:
        # Try as a config key directly (snake_case)
        if step_id in _PHASE_MAP:
            config_key = step_id
        else:
            logger.warning("Unknown morning phase: %s — treating as enabled", step_id)
            return True

    phases = cfg.get("morning", {}).get("phases", {})
    return bool(phases.get(config_key, True))


def resolve_phases(cfg: dict[str, Any]) -> dict[str, bool]:
    """Return ``{step_id: enabled}`` for all 9 morning phases.

    Useful for logging/displaying the phase plan before the DAG starts.
    """
    result: dict[str, bool] = {}
    for config_key, step_id in _PHASE_MAP.items():
        result[step_id] = is_phase_enabled(step_id, cfg)
    for step_id in _ALWAYS_ON:
        result[step_id] = True
    return result


# ---------------------------------------------------------------------------
# Briefing content formatting
# ---------------------------------------------------------------------------

def format_briefing(results: dict[str, Any]) -> str:
    """Format collected step results into a markdown briefing.

    Omits sections for keys whose value is ``None`` (phase was skipped/failed).

    Args:
        results: Dict with optional keys matching step IDs. Expected shape::

            {
                "yesterday": str | None,      # 1-2 sentence summary
                "calendar": dict | None,       # get_today_schedule() result
                "tasks": dict | None,          # task_briefing result
                "contracts": dict | None,      # contract_constraints + health
                "blindspots": str | None,      # pattern summary
            }

    Returns:
        Markdown string for the briefing (no heading — caller adds context).
    """
    lines: list[str] = []

    yesterday = results.get("yesterday")
    if yesterday:
        lines.append(f"**Yesterday:** {yesterday}")

    cal = results.get("calendar")
    if cal is not None:
        if cal.get("available") is False:
            lines.append("**Schedule:** Calendar unavailable")
        else:
            events = cal.get("events", [])
            upcoming = [e for e in events if e.get("timeStatus") in ("upcoming", "current")]
            if upcoming:
                summaries = [
                    f"{e.get('start', {}).get('dateTime', '?')[:5]} {e.get('summary', '?')}"
                    for e in upcoming[:5]
                ]
                lines.append(f"**Schedule:** {', '.join(summaries)}")
            elif events:
                lines.append(f"**Schedule:** {len(events)} event(s), all past or all-day")
            else:
                lines.append("**Schedule:** No events today")

    tasks = results.get("tasks")
    if tasks is not None:
        focused = tasks.get("focused_count", 0)
        overdue = tasks.get("overdue_count", 0)
        inbox = tasks.get("inbox_count", 0)
        lines.append(f"**Tasks:** {focused} focused, {overdue} overdue, {inbox} inbox")

    contracts = results.get("contracts")
    if contracts is not None:
        active = contracts.get("active_count", 0)
        constraint = contracts.get("top_constraint", "none")
        lines.append(f"**Contracts:** {active} active, constraint: \"{constraint}\"")

    blindspots = results.get("blindspots")
    if blindspots:
        lines.append(f"**Patterns:** {blindspots}")
    elif blindspots is not None:
        lines.append("**Patterns:** None detected")

    return "\n".join(lines)


# ── Day Planner helpers ────────────────────────────────────────


def format_plan_summary(entries: list[dict]) -> str:
    """Format generated plan entries into a brief summary for the briefing.

    Returns a compact text showing the day's time-blocked schedule.
    """
    if not entries:
        return "No plan entries generated."

    lines = []
    for e in entries:
        start = e.get("time_start")
        end = e.get("time_end")
        text = e.get("text", "")
        if start and end:
            lines.append(f"  {start}–{end}  {text}")
        else:
            lines.append(f"  (unscheduled)  {text}")

    return "\n".join(lines)
