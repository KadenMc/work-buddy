"""``inline_triage_scan`` capability — user-initiated single-selection triage.

Right-click in Obsidian → :mod:`work_buddy.inline.handlers.send_to_agent`
kicks off this capability in a background thread. We build one
TriageItem from the selection + optional hint, collect the user-context
packet (active tasks, contracts, projects, recent commits), and hand
the whole thing to Sonnet via :class:`LLMRunner` with a constrained
``output_schema`` for the verdict. The parsed verdict is submitted
directly to the Review pool via :func:`triage_submit`.

No tool-call dance, no local-LLM timeouts: the migration from
``llm_with_tools`` on a local model (qwen2.5-coder-14b, 4+ min TTFT,
drops verdict fields) to ``LLMRunner.call(tier=FRONTIER_BALANCED,
output_schema=_VERDICT_SCHEMA)`` is what this file ships as phase 2 of
the LLM + Context refactor. Escalation falls through to
``FRONTIER_BEST`` on timeout / context-exceeded / empty-content.
"""

from __future__ import annotations

from typing import Any

from work_buddy.llm import ErrorKind, LLMRunner, ModelTier
from work_buddy.logging_config import get_logger
from work_buddy.triage.background import BackgroundTriageProducer
from work_buddy.triage.items import TRIAGE_ACTIONS, TriageItem

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompts + schema
# ---------------------------------------------------------------------------


_AGENT_SYSTEM_PROMPT = """\
You are triaging one selection a user sent from Obsidian. Given the
selection, an optional user hint, and the user's current work context
(active tasks, contracts, projects, recent commits), decide the single
best next action and fill in the JSON verdict schema.

Action guide:
  - create_task       — new actionable work. Include ``suggested_task_text``.
  - record_into_task  — add detail to an existing task. Include ``target_task_id``
                        (from the active-tasks list).
  - leave             — keep in the note as-is. Choose this if the selection
                        is an observation, a question, or a thought that doesn't
                        map to a clear action.
  - close             — safe to drop. Rare.
  - group             — belongs with sibling items in the pool. Inline sends
                        are single-item, so prefer ``leave`` over ``group``
                        unless you're certain there's an active cluster.

Weight the user's hint heavily — it's direct intent. If uncertain after
that, pick ``leave``. Rationale: one to three sentences. ``group_intent``:
a short noun phrase (≤8 words) naming what this selection is *about*, not
what to do with it — used as the card title in the Review view.
"""


_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommended_action": {
            "type": "string",
            "enum": list(TRIAGE_ACTIONS),
            "description": "One of: create_task, record_into_task, leave, close, group.",
        },
        "rationale": {
            "type": "string",
            "description": "One to three sentences explaining the decision.",
        },
        "group_intent": {
            "type": "string",
            "description": (
                "Short noun phrase (≤8 words) naming the underlying intent. "
                "Shown as the card title in the Review view."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0.0–1.0 self-assessed confidence.",
        },
        "suggested_task_text": {
            "type": "string",
            "description": "Required when recommended_action == 'create_task'.",
        },
        "target_task_id": {
            "type": "string",
            "description": "Required when recommended_action == 'record_into_task'.",
        },
        "related_item_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Required when recommended_action == 'group'.",
        },
    },
    "required": ["recommended_action", "rationale", "group_intent"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Capability entry point
# ---------------------------------------------------------------------------


def inline_triage_scan(
    *,
    file_path: str,
    selection: str = "",
    paragraph: str = "",
    cursor_line: int = 0,
    hint: str = "",
    force: bool = True,
    tier: ModelTier | str = ModelTier.FRONTIER_BALANCED,
    enrich: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a single triage pass over one user-sent selection.

    Args:
        file_path: Vault-relative source path (for provenance).
        selection: The user's literal selection; falls back to paragraph.
        paragraph: Surrounding paragraph (used when selection is empty).
        cursor_line: 0-indexed cursor line in the source file.
        hint: Optional user-typed intent hint from the modal.
        force: Default ``True`` — user-initiated clicks should re-run
            even when the same selection was sent before.
        tier: Starting LLM tier. Defaults to FRONTIER_BALANCED (Sonnet);
            escalates to FRONTIER_BEST (Opus) on
            TIMEOUT / CONTEXT_EXCEEDED / EMPTY_CONTENT / RATE_LIMITED.
        enrich: Include the user-context packet
            (tasks / contracts / projects / commits). Default True.
        dry_run: Collect the item, skip the LLM call.

    Returns:
        Status dict (see :class:`ProducerResult.to_dict`).
    """
    # Accept raw-string tier from MCP callers; enum callers pass through.
    if isinstance(tier, str) and not isinstance(tier, ModelTier):
        try:
            tier = ModelTier(tier)
        except ValueError as exc:
            raise ValueError(
                f"Unknown tier {tier!r}. Valid: {[t.value for t in ModelTier]}"
            ) from exc

    def _collect() -> tuple[list[TriageItem], str | None]:
        from work_buddy.triage.adapters.inline import collect_inline_selection
        return collect_inline_selection(
            file_path=file_path,
            selection=selection,
            paragraph=paragraph,
            cursor_line=cursor_line,
            hint=hint,
        )

    if dry_run:
        items, ch = _collect()
        return {
            "status": "dry_run",
            "item_count": len(items),
            "content_hash": ch,
            "items": [it.to_dict() for it in items],
        }

    # Build the user-context packet once per pass. Sonnet's context
    # window comfortably fits the full packet so we don't truncate —
    # Sonnet can weigh all tasks/projects/commits itself rather than
    # relying on a state-priority prefilter like the local path did.
    from work_buddy.triage.recommend import build_triage_context
    triage_context = build_triage_context() if enrich else {}

    runner = LLMRunner()

    def _agent(item: TriageItem, run_id: str) -> dict[str, Any]:
        return _invoke_agent(
            runner=runner,
            item=item,
            run_id=run_id,
            context=triage_context,
            tier=tier,
        )

    # ``enrich=False`` on the producer disables IR enrichment — inline
    # uses the ``build_triage_context`` packet above instead, so per-item
    # IR hits would just add noise.
    producer = BackgroundTriageProducer(
        adapter_name="inline_triage",
        source="inline",
        collect=_collect,
        agent=_agent,
        enrich=False,
    )
    return producer.run(force=force).to_dict()


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------


def _invoke_agent(
    *,
    runner: LLMRunner,
    item: TriageItem,
    run_id: str,
    context: dict[str, Any],
    tier: ModelTier,
) -> dict[str, Any]:
    """Call the unified runner with constrained structured output, then submit.

    The verdict comes back as JSON (enforced by ``_VERDICT_SCHEMA`` on
    Anthropic's side). We parse it, call :func:`triage_submit` directly
    to write the pool entry, and return a result dict shaped the way
    :class:`BackgroundTriageProducer` expects (``content`` / ``error`` /
    ``error_kind``) so its submission-check-and-log path works unchanged.
    """
    from work_buddy.triage.capabilities.triage_submit import triage_submit

    user_prompt = _render_item_prompt(item=item, run_id=run_id, context=context)

    resp = runner.call(
        tier=tier,
        system=_AGENT_SYSTEM_PROMPT,
        user=user_prompt,
        output_schema=_VERDICT_SCHEMA,
        escalate_on=[
            ErrorKind.TIMEOUT,
            ErrorKind.CONTEXT_EXCEEDED,
            ErrorKind.EMPTY_CONTENT,
            ErrorKind.RATE_LIMITED,
        ],
        escalate_to=[ModelTier.FRONTIER_BEST],
    )

    if resp.is_error():
        logger.warning(
            "inline_triage: LLM failed for item %s on tier %s (%s): %s",
            item.id, resp.tier_used, resp.error_kind, resp.error,
        )
        return {
            "content": resp.content,
            "error": resp.error,
            "error_kind": resp.error_kind.value if resp.error_kind else None,
        }

    verdict = resp.structured_output or {}
    if not verdict.get("recommended_action"):
        logger.warning(
            "inline_triage: LLM returned no recommended_action for item %s "
            "(tier=%s, content_len=%d)",
            item.id, resp.tier_used, len(resp.content),
        )
        return {
            "content": resp.content,
            "error": "LLM returned no recommended_action",
            "error_kind": ErrorKind.SCHEMA_VIOLATION.value,
        }

    # Submit directly — no tool-call dance. triage_submit whitelists
    # fields and validates the run_id.
    submit_kwargs = _verdict_to_submit_kwargs(verdict)
    submit_result = triage_submit(
        run_id=run_id,
        item_id=item.id,
        **submit_kwargs,
    )

    if submit_result.get("status") != "ok":
        logger.warning(
            "inline_triage: triage_submit rejected verdict for item %s: %s",
            item.id, submit_result,
        )
        return {
            "content": resp.content,
            "error": f"triage_submit rejected: {submit_result.get('error', 'unknown')}",
            "error_kind": ErrorKind.BAD_REQUEST.value,
        }

    return {
        "content": resp.content or "",
        "verdict": verdict,
        "tier_used": resp.tier_used,
    }


def _verdict_to_submit_kwargs(verdict: dict[str, Any]) -> dict[str, Any]:
    """Filter the parsed verdict down to ``triage_submit``'s named kwargs.

    The schema allows a few optional fields that only make sense for
    certain actions (``suggested_task_text`` for create_task,
    ``target_task_id`` for record_into_task, ``related_item_ids`` for
    group). ``triage_submit`` silently ignores unrecognized fields, but
    filtering here keeps the pool entry tidy.
    """
    allowed = {
        "recommended_action",
        "rationale",
        "group_intent",
        "confidence",
        "suggested_task_text",
        "target_task_id",
        "related_item_ids",
    }
    return {k: v for k, v in verdict.items() if k in allowed and v is not None}


def _render_item_prompt(
    *,
    item: TriageItem,
    run_id: str,
    context: dict[str, Any],
) -> str:
    """Compose the per-item user prompt with file + hint + user context."""
    from work_buddy.triage.recommend import render_triage_context_block

    meta = item.metadata or {}
    file_path = meta.get("file_path", "") or "(unknown)"
    cursor_line = meta.get("cursor_line", 0)
    hint = meta.get("hint", "") or "(none)"

    context_block = render_triage_context_block(context) if context else ""
    context_block = f"\n\n{context_block}\n" if context_block else ""

    return (
        f"Item id: {item.id}\n"
        f"File: {file_path}:{cursor_line}\n"
        f"Hint: {hint}\n"
        f"\n--- Selection ---\n"
        f"{item.text.strip()}\n"
        f"--- End ---"
        f"{context_block}"
    )
