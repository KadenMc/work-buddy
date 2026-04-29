"""``triage_review_pool`` capability — compose and dispatch a review modal.

Reads unreviewed pool entries, builds a modal-ready presentation
using the existing ``triage.presentation`` / ``triage.dispatch``
stack, fires the dashboard modal, applies the user's decisions via
``triage.execute``, and marks the pool entries reviewed.

This is the "on demand" review entry point that complements the
producer. The user triggers it when they want to clear the pool —
there is no automatic modal firing.
"""

from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.triage.background import PoolEntry, get_pool

logger = get_logger(__name__)


def triage_review_pool(
    *,
    source: str | None = None,
    adapter: str | None = None,
    since: str | None = None,
    max_items: int = 100,
    dispatch: bool = True,
) -> dict[str, Any]:
    """Review pending triage proposals and (optionally) open the modal.

    Args:
        source: Optional source filter (``"journal_thread"``,
            ``"chrome_tab"``, …).
        adapter: Optional adapter-name filter (``"journal_triage"``).
        since: ISO timestamp; only entries created at-or-after this
            moment are considered.
        max_items: Safety cap on how many pending entries are loaded.
        dispatch: When True (default), opens the dashboard review
            modal and blocks until the user responds or the modal
            times out. When False, returns the composed
            presentation without dispatching — useful for CLI
            inspection and tests.

    Returns:
        ``{"status": "empty"}`` when nothing is pending.
        ``{"status": "composed", "presentation": {...}, "pending": N}``
            when ``dispatch=False``.
        ``{"status": "reviewed", "presentation": {...}, "executed": {...},
           "pool_updates": N}`` when the user responded.
        ``{"status": "timeout"}`` when the modal timed out with no
            response.
    """
    pool = get_pool()
    pending = pool.pending(
        source=source,
        adapter=adapter,
        since=since,
        max_items=max_items,
    )

    if not pending:
        return {"status": "empty", "pending": 0}

    presentation = _build_presentation_from_pool(pending)

    if not dispatch:
        return {
            "status": "composed",
            "presentation": presentation,
            "pending": len(pending),
        }

    from work_buddy.triage.dispatch import dispatch_review
    result = dispatch_review(presentation)

    if result.get("timeout"):
        return {
            "status": "timeout",
            "request_id": result.get("request_id"),
            "pending": len(pending),
        }
    if result.get("error"):
        return {
            "status": "error",
            "error": result["error"],
            "pending": len(pending),
        }
    if not result.get("responded"):
        return {
            "status": "error",
            "error": "dispatch_review returned no response",
            "pending": len(pending),
        }

    decisions = result.get("decisions", {})

    # Hand off to the existing triage executor — it already knows
    # how to handle source-agnostic actions (create_task,
    # record_into_task, leave). Source-specific actions (close,
    # group) only execute meaningfully for Chrome today.
    executed: dict[str, Any] = {"skipped": True}
    try:
        from work_buddy.triage.execute import execute_triage_decisions
        executed = execute_triage_decisions(decisions, presentation)
    except Exception as exc:
        logger.warning("execute_triage_decisions failed: %s", exc)
        executed = {"error": f"{type(exc).__name__}: {exc}"}

    # Stamp entries reviewed. Use a coarse outcome for v1; a future
    # refactor can reflect per-action outcomes from `executed`.
    keys = [(pe.run_id, pe.item_id) for pe in pending]
    pool_updates = pool.mark_reviewed(keys, outcome="reviewed")

    return {
        "status": "reviewed",
        "presentation": presentation,
        "decisions": decisions,
        "executed": executed,
        "pool_updates": pool_updates,
    }


def _build_presentation_from_pool(
    entries: list[PoolEntry],
) -> dict[str, Any]:
    """Shape a list of pool entries into the modal's presentation dict.

    Mirrors the output of ``build_presentation`` closely enough that
    the existing frontend renderer needs no changes. Grouping is done
    by ``verdict.recommended_action``; items within a group are
    further sub-grouped by ``related_item_ids`` linkage when present
    (v1 keeps this simple — one entry per group).
    """
    from work_buddy.triage.items import TRIAGE_ACTIONS

    # Infer source for the modal. If mixed, report "unknown" so the
    # modal uses its generic icon/label.
    # Normalize per-item source name (e.g. ``chrome_tab``, ``journal_thread``)
    # to the presentation-level short form the modal + executor expect
    # (``chrome``, ``journal``). This matches the convention set by
    # ``work_buddy.triage.presentation._detect_source`` which derives from
    # item id prefixes; we keep it in sync here so both code paths feed
    # the same downstream key.
    _SOURCE_NORMALIZE = {
        "chrome_tab": "chrome",
        "journal_thread": "journal",
        "conversation": "conversation",
        "inline": "inline",
    }
    sources = {_SOURCE_NORMALIZE.get(e.source, e.source) for e in entries}
    source = next(iter(sources)) if len(sources) == 1 else "unknown"

    groups_by_action: dict[str, list[dict[str, Any]]] = {
        action: [] for action in TRIAGE_ACTIONS
    }

    all_item_ids: list[str] = []
    for i, pe in enumerate(entries):
        # Slice 1: raw entries (verdict_pass gated off) carry no
        # ``recommended_action``, no rationale, no group_intent. The
        # default verdicted-card layout puts IR-context plumbing in the
        # spotlight and buries the user's captured text. Render them
        # differently: lead with the captured text, drop the IR
        # context, mark them clearly as needing triage. Slice 3 brings
        # GTD-shaped verdicts back; until then this is the right shape.
        is_raw = bool(pe.verdict.get("raw"))

        action = pe.verdict.get("recommended_action", "leave")
        if action not in groups_by_action:
            action = "leave"

        item_obj = pe.item or {}
        label = item_obj.get("label") or pe.item_id
        url = item_obj.get("url", "") or ""
        text = item_obj.get("text", "") or ""
        meta = item_obj.get("metadata", {}) or {}
        summary = _short(text, 240)
        ir_ctx = meta.get("ir_context", []) or []

        modal_item = {
            "id": pe.item_id,
            "label": label,
            "summary": summary,
        }
        if url:
            modal_item["url"] = url

        # Per-source "open in app" actions, declared in
        # SourceDescriptor.config.open_action and resolved against the
        # item's metadata. Frontend renders each entry as a button next
        # to the label and POSTs the click to /api/palette/execute. See
        # work_buddy/triage/card_actions.py for the contract.
        from work_buddy.triage.card_actions import build_card_actions
        actions = build_card_actions(pe.source, item_obj)
        if actions:
            modal_item["actions"] = actions

        if is_raw:
            # Card title = first ~80 chars of the captured text, with
            # any leading capture-marker prose stripped so the user's
            # actual thought is what shows up.
            intent = _raw_intent_from_text(text)
            rationale = (
                "Raw capture — verdict pending. "
                "Slice 3 will revisit with the new GTD-shaped schema; "
                "for now, pick an action manually."
            )
            # Drop IR-context display: it's pre-LLM enrichment, not
            # content. Showing it dominates the visual without value.
            context_block = ""
        else:
            # Verdicted entry — original presentation. Prefer the
            # agent-supplied group_intent (short, noun-phrase naming
            # the *intent*). Fall back ladder:
            #   1. explicit group_intent field (best)
            #   2. suggested_task_text (agents emit this reliably for
            #      create_task; it's usually a clean noun-phrase and
            #      beats a rationale excerpt for a card title)
            #   3. rationale first 120 chars (last resort; keeps
            #      backwards compat with pre-group_intent pool entries)
            intent = (
                pe.verdict.get("group_intent")
                or pe.verdict.get("suggested_task_text")
                or _short(pe.verdict.get("rationale", ""), 120)
            )
            rationale = pe.verdict.get("rationale", "")
            context_block = _render_context_block(ir_ctx)

        presentation_group: dict[str, Any] = {
            "index": i,
            "intent": intent,
            "confidence": _confidence_label(pe.verdict.get("confidence")),
            "items": [modal_item],
            "rationale": rationale,
            "context": context_block,
            "ambiguities": [],
            "likely_task_id": pe.verdict.get("target_task_id", "") or "",
            "suggested_action": action,
            "pool_run_id": pe.run_id,  # non-UI; used for mark_reviewed
            "is_raw": is_raw,          # non-UI; lets renderers tag the card
        }
        if action == "create_task":
            presentation_group["suggested_task_text"] = (
                pe.verdict.get("suggested_task_text")
                or pe.verdict.get("rationale", "")
            )

        groups_by_action[action].append(presentation_group)
        all_item_ids.append(pe.item_id)

    return {
        "source": source,
        "narrative": (
            f"{len(entries)} pending triage proposals from "
            f"{len(sources)} source(s)."
        ),
        "total_groups": len(entries),
        "total_items": len(all_item_ids),
        "groups_by_action": groups_by_action,
        "display_order": list(range(len(entries))),
        "uncategorized": [],
        "available_detail_ids": all_item_ids,
        "has_clarifying_questions": False,
        "revisions": 0,
    }


def _raw_intent_from_text(text: str, max_chars: int = 80) -> str:
    """Build a card-title intent string for a raw entry.

    The captured text often leads with a Telegram-capture marker like::

        > #wb/capture/mobile from Kaden McKeen (@kadenmckeen) at 2026-04-27 10:17
        GTD: he has a nice sort of is this thing 2 minutes? ...

    Stripping the marker exposes the user's actual thought, which is
    what makes a useful card title. The strip is conservative — if no
    marker pattern is matched, return the leading ``max_chars`` of the
    raw text as-is.
    """
    if not text:
        return "(empty capture — needs triage)"
    cleaned = text.strip()
    # Drop a leading blockquote line (the ``> #wb/capture/...`` marker).
    # If the next line starts with a "topic prefix" like ``GTD:`` or
    # ``Idea:``, keep the topic prefix — it's content the user wrote.
    if cleaned.startswith(">"):
        nl = cleaned.find("\n")
        if nl >= 0:
            cleaned = cleaned[nl + 1:].lstrip()
    cleaned = cleaned.replace("\n", " ").strip()
    if not cleaned:
        return "(empty capture — needs triage)"
    return _short(cleaned, max_chars)


def _short(text: str, n: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _confidence_label(c: Any) -> str:
    try:
        v = float(c)
    except (TypeError, ValueError):
        return "low"
    if v >= 0.8:
        return "high"
    if v >= 0.5:
        return "medium"
    return "low"


def _render_context_block(hits: list[dict[str, Any]]) -> str:
    """Render IR context as a compact block for the modal's context field."""
    if not hits:
        return ""
    from work_buddy.triage.enrich import render_ir_context
    return render_ir_context(hits)
