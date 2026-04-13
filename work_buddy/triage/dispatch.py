"""Auto_run dispatch steps for triage workflows.

Programmatic wrappers that create dashboard modals (clarify / review),
deliver them via the notification system, and poll for user responses.
These run as auto_run code steps — no agent reasoning needed.

Uses the pure Python notification API directly (no MCP server imports).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.notifications.dispatcher import SurfaceDispatcher
from work_buddy.notifications.models import Notification
from work_buddy.notifications.store import (
    create_notification,
    get_notification,
    mark_delivered,
)

logger = get_logger(__name__)

# Default timeouts (seconds) — must match YAML timeout values
_CLARIFY_TIMEOUT = 300  # 5 min
_REVIEW_TIMEOUT = 600  # 10 min
_POLL_INTERVAL = 3  # seconds between polls


def _has_clarifying_questions(presentation: dict[str, Any]) -> bool:
    """Check if any group in the presentation has clarifying questions."""
    for groups in presentation.get("groups_by_action", {}).values():
        for group in groups:
            if group.get("clarifying_questions"):
                return True
    return False


def _dispatch_and_poll(
    title: str,
    template_type: str,
    presentation: dict[str, Any],
    timeout_seconds: int,
    surfaces: list[str] | None = None,
) -> dict[str, Any]:
    """Create a notification, deliver to surfaces, poll for response.

    Returns:
        On response: {"responded": True, "value": <response>, ...}
        On timeout:  {"timeout": True, "request_id": "...", ...}
        On error:    {"error": "...", ...}
    """
    session_id = os.environ.get("WORK_BUDDY_SESSION_ID", "")

    notif = Notification(
        title=title,
        body="",
        priority="normal",
        source="workflow",
        source_type="agent",
        response_type="custom",
        custom_template={"type": template_type, "presentation": presentation},
        callback_session_id=session_id,
        surfaces=surfaces or ["dashboard"],
    )

    created = create_notification(notif)
    nid = created.notification_id

    # Deliver
    dispatcher = SurfaceDispatcher.from_config()
    results = dispatcher.deliver(created, mark_delivered_fn=mark_delivered)
    any_ok = any(results.values())

    if not any_ok:
        failed = [k for k, v in results.items() if not v]
        error_msg = f"No surfaces delivered: {', '.join(failed)}" if failed else "No eligible surfaces"
        logger.error("dispatch[%s]: %s", template_type, error_msg)
        return {"error": error_msg, "presentation": presentation}

    logger.info(
        "dispatch[%s]: delivered %s via %s, polling (timeout=%ds)",
        template_type, nid,
        [k for k, v in results.items() if v],
        timeout_seconds,
    )

    # Re-read from store to get updated delivered_surfaces
    fresh = get_notification(nid) or created

    # Poll for response
    response = dispatcher.poll_response(
        fresh,
        timeout_seconds=timeout_seconds,
        interval_seconds=_POLL_INTERVAL,
    )

    if response is None:
        logger.warning(
            "dispatch[%s]: timeout after %ds for %s",
            template_type, timeout_seconds, nid,
        )
        return {
            "timeout": True,
            "request_id": nid,
            "presentation": presentation,
        }

    # First-response-wins: dismiss on other surfaces
    fresh_again = get_notification(nid)
    if fresh_again and fresh_again.delivered_surfaces:
        try:
            dispatcher.dismiss_others(
                nid,
                responding_surface=response.surface,
                delivered_surfaces=fresh_again.delivered_surfaces,
            )
        except Exception:
            pass  # best-effort

    logger.info(
        "dispatch[%s]: got response via %s for %s",
        template_type, response.surface, nid,
    )

    return {
        "responded": True,
        "value": response.value,
        "surface": response.surface,
        "request_id": nid,
        "presentation": presentation,
    }


def _validate_presentation(presentation: Any, caller: str) -> dict[str, Any] | None:
    """Validate that a presentation dict has the expected structure.

    Returns an error dict if invalid, ``None`` if valid.
    """
    if not isinstance(presentation, dict):
        logger.error(
            "%s: expected dict, got %s", caller, type(presentation).__name__,
        )
        return {
            "error": (
                f"{caller}: expected presentation dict, "
                f"got {type(presentation).__name__}"
            ),
        }

    if "groups_by_action" not in presentation:
        logger.error(
            "%s: missing 'groups_by_action'. Keys: %s",
            caller, list(presentation.keys()),
        )
        return {
            "error": (
                f"{caller}: invalid presentation — missing 'groups_by_action'. "
                f"Received keys: {list(presentation.keys())}"
            ),
            "received_keys": list(presentation.keys()),
        }

    return None


# ── Auto_run entry points ─────────────────────────────────────


def dispatch_clarify(presentation: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the triage_clarify modal if clarifying questions exist.

    Auto_run entry point for the dispatch-clarify step.

    Args:
        presentation: Updated presentation dict from the resolve-and-clarify
            reasoning step (groups may have ``clarifying_questions`` arrays).

    Returns:
        If questions exist and user responds:
            {"responded": True, "answers": {...}, "presentation": {...}}
        If no questions:
            {"skipped": True, "presentation": {...}}
        If timeout:
            {"timeout": True, "request_id": "...", "presentation": {...}}
    """
    # Unwrap if the reasoning step wrapped it
    if "presentation" in presentation and isinstance(presentation.get("presentation"), dict):
        presentation = presentation["presentation"]

    # Validate input — catch malformed data from reasoning step
    validation_err = _validate_presentation(presentation, "dispatch_clarify")
    if validation_err:
        return validation_err

    # Save presentation before dispatching (timeout recovery)
    try:
        from work_buddy.triage.presentation import save_presentation
        save_presentation(presentation)
        logger.info("dispatch_clarify: saved presentation to disk")
    except Exception as e:
        logger.warning("dispatch_clarify: failed to save presentation: %s", e)

    if not _has_clarifying_questions(presentation):
        logger.info("dispatch_clarify: no clarifying questions, skipping")
        return {"skipped": True, "presentation": presentation}

    result = _dispatch_and_poll(
        title="Triage — Clarifying Questions",
        template_type="triage_clarify",
        presentation=presentation,
        timeout_seconds=_CLARIFY_TIMEOUT,
    )

    # Normalize: extract answers from the response value
    if result.get("responded"):
        value = result.get("value", {})
        result["answers"] = value.get("answers", value) if isinstance(value, dict) else value

    return result


def dispatch_review(presentation: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the triage_review modal and collect user decisions.

    Auto_run entry point for the dispatch-review step.

    Saves the presentation to disk before dispatching, and saves
    the user's decisions alongside it after response.

    Args:
        presentation: Final presentation dict from the build-recommendations
            reasoning step.

    Returns:
        If user responds:
            {"responded": True, "decisions": {...}, "presentation": {...}}
        If timeout:
            {"timeout": True, "request_id": "...", "presentation": {...}}
    """
    # Unwrap if the reasoning step wrapped it
    if "presentation" in presentation and isinstance(presentation.get("presentation"), dict):
        presentation = presentation["presentation"]

    # Validate input — catch malformed data from reasoning step
    validation_err = _validate_presentation(presentation, "dispatch_review")
    if validation_err:
        return validation_err

    # Save presentation to disk before dispatching
    try:
        from work_buddy.triage.presentation import save_presentation
        save_presentation(presentation)
        logger.info("dispatch_review: saved presentation to disk")
    except Exception as e:
        logger.warning("dispatch_review: failed to save presentation: %s", e)

    result = _dispatch_and_poll(
        title="Triage — Review Actions",
        template_type="triage_review",
        presentation=presentation,
        timeout_seconds=_REVIEW_TIMEOUT,
    )

    # Normalize: extract decisions from the response value
    if result.get("responded"):
        value = result.get("value", {})
        result["decisions"] = value if isinstance(value, dict) else {"raw": value}

        # Save decisions to disk
        try:
            from work_buddy.triage.presentation import save_decisions
            save_decisions(result["decisions"])
            logger.info("dispatch_review: saved decisions to disk")
        except Exception as e:
            logger.warning("dispatch_review: failed to save decisions: %s", e)

    return result
