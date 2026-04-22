"""Dispatcher for requirement fixes — backend half of the Fix system.

The dashboard's ``POST /api/control/fix/<req_id>`` endpoint calls
:func:`run_fix` after the read-only guard. This module:

  * Looks up the requirement and validates it has a fix.
  * Imports and calls the fix function (programmatic / input_required).
  * For agent_handoff, builds a brief and spawns a Claude Code session.
  * Re-runs the requirement's check so the caller can show the new
    state immediately without waiting for a graph-cache refresh.
  * Invalidates the control graph so subsequent reads see fresh data.

Errors return a structured ``{ok: False, detail, error}`` dict — never
raise — so the endpoint can return them as 200s with ok=False rather
than a 500. The frontend distinguishes "fix didn't apply" from "the
backend exploded" cleanly.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

log = logging.getLogger(__name__)


def run_fix(req_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply the fix for a requirement and return a structured result.

    Parameters
    ----------
    req_id:
        Requirement ID (e.g. ``core/data/writable``). Must exist in
        ``REQUIREMENT_REGISTRY``.
    params:
        For ``input_required`` fixes, the user-supplied form values.
        Ignored for ``programmatic`` and ``agent_handoff`` kinds.

    Returns
    -------
    dict with keys:
        ``ok``: bool — did the fix apply successfully?
        ``detail``: str — human-readable result
        ``side_effects``: list[str] — what changed (optional, fixer-supplied)
        ``recheck``: dict — fresh result from the requirement's check_fn
                    after the fix ran (so the caller can show updated state)
        ``spawned``: dict | None — for agent_handoff, info about the
                                    launched Claude Code session
    """
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY, RequirementChecker
    from work_buddy.control.graph import invalidate_graph

    req = REQUIREMENT_REGISTRY.get(req_id)
    if req is None:
        return {
            "ok": False,
            "detail": f"Unknown requirement: {req_id}",
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }

    if req.fix_kind == "none":
        return {
            "ok": False,
            "detail": (
                f"Requirement {req_id} has no automated fix. Follow the "
                f"fix_hint manually: {req.fix_hint}"
            ),
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }

    if req.fix_kind == "agent_handoff":
        return _spawn_fix_agent(req)

    # programmatic | input_required
    if not req.fix_fn:
        return {
            "ok": False,
            "detail": (
                f"Requirement {req_id} declares fix_kind={req.fix_kind!r} "
                "but has no fix_fn — registry data error."
            ),
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }

    user_params = params or {}
    if req.fix_kind == "input_required":
        # Validate required fields are present (presence only — fixer
        # validates value shape and returns ok=False on bad input).
        missing = [
            name for name, spec in req.fix_params.items()
            if spec.get("required") and name not in user_params
        ]
        if missing:
            return {
                "ok": False,
                "detail": f"Missing required input fields: {', '.join(missing)}",
                "side_effects": [],
                "recheck": None,
                "spawned": None,
            }

    # Import + call the fix function
    try:
        mod_path, fn_name = req.fix_fn.rsplit(".", 1)
        mod = importlib.import_module(mod_path)
        fn = getattr(mod, fn_name)
    except (ImportError, AttributeError, ValueError) as exc:
        return {
            "ok": False,
            "detail": f"Could not import fix function {req.fix_fn}: {exc}",
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }

    try:
        if req.fix_kind == "input_required":
            result = fn(**user_params)
        else:
            result = fn()
    except Exception as exc:  # broad: fixers should never raise, but if they do
        log.exception("Fix function %s raised", req.fix_fn)
        return {
            "ok": False,
            "detail": f"Fix function raised: {exc}",
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }

    # Normalize result shape
    if not isinstance(result, dict):
        return {
            "ok": False,
            "detail": f"Fix function returned non-dict ({type(result).__name__})",
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }

    fix_ok = bool(result.get("ok"))
    fix_detail = str(result.get("detail", ""))
    side_effects = list(result.get("side_effects") or [])

    # Re-run the check so the caller can show the new state immediately.
    # If the fix said ok=True but the check still fails, surface that —
    # it means the fix didn't actually fix what we claimed.
    checker = RequirementChecker()
    recheck = checker._run_check(req).to_dict()

    # Bust the graph cache so the next /api/control/graph read includes
    # the fresh state for this requirement.
    invalidate_graph()

    return {
        "ok": fix_ok and recheck.get("ok", False),
        "detail": fix_detail,
        "side_effects": side_effects,
        "recheck": recheck,
        "spawned": None,
    }


def _spawn_fix_agent(req) -> dict[str, Any]:
    """Spawn a Claude Code session to walk the user through a complex fix.

    Used for ``fix_kind == "agent_handoff"`` requirements where a one-
    click programmatic fix isn't appropriate (multi-step, requires
    judgement, touches systems beyond work-buddy's reach).
    """
    from work_buddy.consent import grant_consent
    from work_buddy.session_launcher import begin_session

    brief = req.fix_agent_brief or _generic_fix_brief(req)
    grant_consent("sidecar:remote_session_launch", mode="once")

    try:
        # remote_control=False — these sessions are an interactive
        # desktop terminal the user drives locally. Remote-control mode
        # is for Telegram/phone bridging; we want the normal case here.
        result = begin_session(prompt=brief, remote_control=False)
    except Exception as exc:
        log.exception("Failed to spawn fix agent for %s", req.id)
        return {
            "ok": False,
            "detail": f"Could not launch agent session: {exc}",
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }

    if result.get("status") != "ok":
        return {
            "ok": False,
            "detail": result.get("error", "Agent launch failed."),
            "side_effects": [],
            "recheck": None,
            "spawned": None,
        }

    return {
        "ok": True,
        "detail": (
            "Agent session launched — follow the prompts in the new "
            "terminal to complete the fix."
        ),
        "side_effects": [],
        "recheck": None,
        "spawned": {
            "session_id": result.get("session_id", ""),
            "pid": result.get("pid"),
            "message": result.get("message", "Session started."),
        },
    }


def _generic_fix_brief(req) -> str:
    """Build a fallback brief when a fixer didn't supply a custom one."""
    return (
        f"You are helping the user fix a work-buddy requirement that's "
        f"currently failing.\n\n"
        f"Requirement: {req.id}\n"
        f"Description: {req.description}\n"
        f"Severity: {req.severity}\n\n"
        f"Fix hint:\n{req.fix_hint}\n\n"
        f"Walk the user through resolving this. When done, ask them to "
        f"open the work-buddy dashboard Settings tab and confirm the "
        f"requirement now shows green."
    )
