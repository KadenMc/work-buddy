"""Per-action context-availability status — Stage 4.11.

UX.md §12 spec: per-action card shows whether required contexts
are currently available. Replaces the rejected "global current
contexts banner" idea with an inline status line on each action.

The check is read-only and called per render. Backed by the
existing ``automation.contexts.CONTEXT_REGISTRY`` + ``tools.is_tool_available``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def context_status(token: str) -> dict[str, Any]:
    """Check whether a single context token is currently available.

    Returns:
        {
            "token": "@email_send",
            "available": True | False,
            "reason": str | None,           # short explanation if unavailable
            "kind": "user_only" | "always" | "probe_gated" | "unknown"
        }
    """
    try:
        from work_buddy.automation.contexts import CONTEXT_REGISTRY
    except Exception as e:
        logger.warning("contexts module import failed: %s", e)
        return {
            "token": token, "available": False,
            "reason": "context registry unavailable", "kind": "unknown",
        }

    if token not in CONTEXT_REGISTRY:
        return {
            "token": token, "available": False,
            "reason": f"unknown context token {token!r}",
            "kind": "unknown",
        }

    tool_ids = CONTEXT_REGISTRY[token]

    if tool_ids is None:
        # User-only context — agent never satisfies. Render as
        # "user-only" rather than "unavailable" because the action
        # would dispatch to the user, not the agent.
        return {
            "token": token, "available": False,
            "reason": "user-only — agent can't satisfy",
            "kind": "user_only",
        }
    if tool_ids == []:
        # Universally available
        return {
            "token": token, "available": True, "reason": None,
            "kind": "always",
        }

    # Probe-gated: check tool status
    try:
        from work_buddy.tools import is_tool_available
    except Exception as e:
        logger.warning("tools module import failed: %s", e)
        return {
            "token": token, "available": False,
            "reason": "tool status unavailable", "kind": "probe_gated",
        }

    missing = []
    for tid in tool_ids:
        try:
            ok = is_tool_available(tid)
        except Exception:
            ok = False
        if not ok:
            missing.append(tid)
    if missing:
        return {
            "token": token, "available": False,
            "reason": f"missing tool(s): {', '.join(missing)}",
            "kind": "probe_gated",
        }
    return {
        "token": token, "available": True, "reason": None,
        "kind": "probe_gated",
    }


def context_statuses(tokens: list[str]) -> list[dict[str, Any]]:
    """Bulk-check a list of context tokens. Returns a parallel list."""
    return [context_status(t) for t in (tokens or [])]
