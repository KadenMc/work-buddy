"""Slice 5b: ``/wb-task-me`` orchestration helpers.

Two pure-ish callables that the ``tasks/task-me`` workflow uses for
its auto_run code steps:

* :func:`load_context_for_task_me` — composes ``task_briefing``,
  ``context_calendar`` (best-effort), and ``contract_constraints``
  into a single dict.  Mirrors the morning routine's per-domain
  collectors but consolidates them so the engage step has one
  payload to reason against.

* :func:`build_now_plan` — calls
  ``work_buddy.obsidian.day_planner.planner.generate_plan`` with
  ``clamp_to_now=True`` so nothing lands in the past.  Returns the
  proposed timeline (NOT written back; write-back is a consent-gated
  reasoning step in the workflow).

Both callables degrade gracefully — a missing capability returns the
partial state with a status field so the engage step can render
"calendar unavailable, working with task-only context."  Slice 11's
reactive contexts work consumes these health flags in turn.

The workflow JSON definition lives in
``knowledge/store/workflows.json`` under ``tasks/task-me``; the
slash-command launcher in ``.claude/commands/wb-task-me.md``; and
the user-facing recipe in
``knowledge/store/tasks.json`` under ``tasks/task-me-directions``.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _coerce_paths(obj: Any) -> Any:
    """Recursively convert pathlib.Path values to strings.

    Used by ``load_context_for_task_me`` to defang Path objects from
    the contracts module before the result hits Flask's JSON encoder.
    """
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _coerce_paths(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce_paths(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Step 1: load-context
# ---------------------------------------------------------------------------


def load_context_for_task_me(
    *,
    user_current_contexts: list[str] | None = None,
) -> dict[str, Any]:
    """Combine task / calendar / contract state for the engage step.

    Args:
        user_current_contexts: Optional list of context tokens the
            user is currently in (e.g. ``["@filesystem", "@vault",
            "@user_workstation"]``).  Forwarded into the engage payload
            so the recommender can filter by who-can-act-now.  When
            None, the engage view treats every user-required context
            as unmet.

    Returns:
        Dict with the following keys (each may be missing if the
        underlying capability raised — the engage step inspects the
        ``status`` flag):

        - ``status``: ``"ok"`` | ``"degraded"`` (one or more sub-calls failed)
        - ``task_briefing``: focused / mit / overdue / inbox / stale buckets
        - ``calendar``: today's events when the calendar tool is available
        - ``contract_constraints``: WIP-limit + active-contract data
        - ``engage``: the live Slice-5a engage view (tier × who_can_act
          × user_now per task), pre-filtered by ``user_current_contexts``
        - ``current_contexts``: what the user declared this session
        - ``now_iso``: timestamp the bundle was assembled
    """
    out: dict[str, Any] = {
        "status": "ok",
        "current_contexts": list(user_current_contexts or []),
        "now_iso": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "errors": [],
    }

    # --- Task briefing ----------------------------------------------
    try:
        from work_buddy.obsidian.tasks.manager import daily_briefing
        out["task_briefing"] = daily_briefing()
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("task_me: task_briefing failed: %s", exc)
        out["status"] = "degraded"
        out["errors"].append({"step": "task_briefing", "error": str(exc)})

    # --- Calendar (best-effort; many users don't have it wired) -----
    try:
        from work_buddy.context.sources import calendar as cal_src
        # The collector module exposes ``today()`` or similar. If not
        # available, we degrade silently — the engage step can still
        # recommend without a calendar.
        events_fn = (
            getattr(cal_src, "today_events", None)
            or getattr(cal_src, "today", None)
        )
        if events_fn is not None:
            out["calendar"] = events_fn()
        else:
            out["calendar"] = []
    except Exception as exc:  # pragma: no cover — best-effort
        logger.debug("task_me: calendar fetch unavailable: %s", exc)
        out["status"] = "degraded"
        out["errors"].append({"step": "calendar", "error": str(exc)})
        out["calendar"] = []

    # --- Contract constraints ---------------------------------------
    # Contracts can carry pathlib.Path objects in their dict (the
    # source file_path).  Coerce to strings so the dashboard's JSON
    # encoder doesn't choke.  Cheap shallow walk; contracts aren't
    # nested deeper than 2 levels.
    try:
        from work_buddy import contracts as contracts_mod
        out["contract_constraints"] = _coerce_paths({
            "active": contracts_mod.active_contracts(),
            "constraints": contracts_mod.get_constraints(),
            "wip_limit": contracts_mod.check_wip_limit(),
        })
    except Exception as exc:  # pragma: no cover — best-effort
        logger.debug("task_me: contracts fetch unavailable: %s", exc)
        out["contract_constraints"] = {}

    # --- Engage view (Slice 5a) -------------------------------------
    try:
        from work_buddy.dashboard.service import _build_engage_view_payload
        out["engage"] = _build_engage_view_payload(
            current_contexts=user_current_contexts,
        )
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("task_me: engage view build failed: %s", exc)
        out["status"] = "degraded"
        out["errors"].append({"step": "engage", "error": str(exc)})
        out["engage"] = {"status": "error", "items": []}

    return out


# ---------------------------------------------------------------------------
# Step 2: build-now-plan
# ---------------------------------------------------------------------------


def build_now_plan(
    *,
    context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a clamp-to-now time-blocked plan from the engage state.

    Args:
        context: The output of :func:`load_context_for_task_me`.
            Falls back to a fresh load if None.
        config: Day-planner config overrides (work_hours, etc.).
            Defaults are read from ``config.local.yaml`` /
            ``config.yaml`` under ``morning.day_planner`` like the
            morning routine does.

    Returns:
        Dict with ``status``, ``plan`` (list of plan entries from
        ``generate_plan``), ``focused_count`` (how many tasks went
        in), ``calendar_event_count``, and ``clamped`` (True — we
        always clamp; the field is documented for the engage prompt).
    """
    if context is None:
        context = load_context_for_task_me()

    # Pull focused tasks from the engage view (so we benefit from the
    # Slice-5a context filter); fall back to task_briefing if engage
    # isn't available.
    focused_tasks: list[dict[str, Any]] = []
    engage = context.get("engage") or {}
    items = engage.get("items") or []
    if items:
        for it in items:
            if it.get("state") != "focused":
                continue
            who = it.get("who_can_act") or {}
            user_now = it.get("user_now") or {}
            # Slice-5a integration: skip items the agent can't satisfy
            # AND the user can't satisfy in their current context.
            if (not who.get("agent")) and (not user_now.get("satisfied")):
                continue
            focused_tasks.append({
                "description": it.get("text") or it.get("task_id"),
                "task_id": it.get("task_id"),
            })
    elif context.get("task_briefing"):
        focused_tasks = [
            {"description": t.get("description") or t.get("text", ""),
             "task_id": t.get("task_id") or t.get("id")}
            for t in (context["task_briefing"].get("focused") or [])
        ]

    calendar_events = context.get("calendar") or []

    # Day-planner config: same source as morning routine.
    if config is None:
        try:
            from work_buddy.config import load_config
            config = (load_config() or {}).get("morning", {}).get(
                "day_planner", {},
            )
        except Exception:  # pragma: no cover
            config = {}
    cfg = dict(config or {})
    cfg.setdefault("clamp_to_now", True)

    try:
        from work_buddy.obsidian.day_planner.planner import generate_plan
        plan = generate_plan(
            calendar_events=calendar_events,
            focused_tasks=focused_tasks,
            cfg=cfg,
        )
    except Exception as exc:
        logger.warning("task_me: generate_plan failed: %s", exc)
        return {
            "status": "error",
            "error": str(exc),
            "plan": [],
            "focused_count": len(focused_tasks),
            "calendar_event_count": len(calendar_events),
            "clamped": True,
        }

    return {
        "status": "ok",
        "plan": plan,
        "focused_count": len(focused_tasks),
        "calendar_event_count": len(calendar_events),
        "clamped": True,
        "now_iso": context.get("now_iso"),
    }


# ---------------------------------------------------------------------------
# Recommendation helper used by the dashboard's Today tab
# ---------------------------------------------------------------------------


def top_recommendations(
    engage: dict[str, Any] | None,
    *,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Pick the top-N actionable engage items for the Today tab.

    Heuristic (no LLM required for the tab — the slash command does
    the LLM-driven engage step):

    1. Filter to tasks the agent OR user can act on right now.
    2. Sort: state=focused first, then mit, then inbox.
    3. Within state, urgency: high → medium → low.
    4. Within urgency, contract-attached first.

    Returns the top ``limit`` items (default 2 — V1a attention
    scarcity; "what should I do right now?" expects 1-2 cards, not 8).
    """
    if not engage or not engage.get("items"):
        return []

    state_rank = {"focused": 0, "mit": 1, "inbox": 2, "snoozed": 3, "done": 4}
    urgency_rank = {"high": 0, "medium": 1, "low": 2}

    def actionable(it: dict) -> bool:
        who = it.get("who_can_act") or {}
        user_now = it.get("user_now") or {}
        # If neither agent nor user can act, it's blocked-blocked.
        if (not who.get("agent")) and (not user_now.get("satisfied")):
            return False
        # If state is done/snoozed, not actionable now.
        if it.get("state") in {"done", "archived"}:
            return False
        return True

    def sort_key(it: dict) -> tuple:
        return (
            state_rank.get(it.get("state"), 9),
            urgency_rank.get(it.get("urgency"), 9),
            0 if it.get("contract") else 1,
        )

    candidates = [it for it in engage["items"] if actionable(it)]
    candidates.sort(key=sort_key)
    return candidates[:limit]
