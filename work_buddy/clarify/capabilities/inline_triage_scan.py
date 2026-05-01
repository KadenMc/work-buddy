"""``inline_triage_scan`` capability — user-initiated single-selection Clarify.

Right-click in Obsidian → :mod:`work_buddy.inline.handlers.send_to_agent`
kicks off this capability in a background thread. We build one
TriageItem from the selection + optional hint, collect the user-context
packet (active tasks, contracts, projects, recent commits), and run
the Slice 3 Clarify pipeline:

1. **Deadline pre-pass** (Haiku, cheap). Extracts has_deadline /
   deadline_date / has_dependency / dependency_hint from the
   selection. Failures degrade to "no hints found" silently.
2. **Main Clarify pass** (Sonnet, escalates to Opus on backend or
   validation failure). Produces the multi-record verdict using
   :data:`MULTI_RECORD_VERDICT_SCHEMA`. The deadline hints are
   merged into resulting task_proposals post-call.

The parsed verdict is submitted directly to the Resolution Surface
pool via :func:`triage_submit`.

Escalation:
  - Backend errors (TIMEOUT / CONTEXT_EXCEEDED / EMPTY_CONTENT /
    RATE_LIMITED) → FRONTIER_BEST via LLMRunner's built-in escalation.
  - Verdict parses but missing rationale / group_intent → one
    validation retry at FRONTIER_BEST, then
    :data:`ErrorKind.VALIDATION_FAILED`.
"""

from __future__ import annotations

from datetime import date as _date_cls
from typing import Any

from work_buddy.llm import ErrorKind, LLMRunner, ModelTier
from work_buddy.logging_config import get_logger
from work_buddy.clarify.background import BackgroundTriageProducer
from work_buddy.clarify.items import TriageItem
from work_buddy.clarify.verdict_schema import (
    MULTI_RECORD_VERDICT_SCHEMA,
    verdict_to_submit_kwargs,
)

logger = get_logger(__name__)


_AGENT_SYSTEM_PROMPT = """\
You are running the Clarify step on one selection a user sent from
Obsidian (right-click "Send to agent" or capture-tag). Given the
selection, an optional user hint, the user's current work context
(active tasks, contracts, projects, recent commits), and pre-extracted
deadline / dependency hints, produce a verdict in the multi-record
schema.

A captured selection produces ZERO OR MORE records. Each record has a
``destination`` (one of: ``task``, ``reference``, ``calendar_only``,
``delete``) and a destination-specific proposal.

## Destination guide

  - ``task``           — actionable work. Populate ``task_proposal``
                         with required ``suggested_task_text`` plus
                         optional Slice 2 metadata (kind,
                         outcome_text, next_action_text,
                         definition_of_done, creation_effort,
                         user_involvement). Copy any deadline /
                         dependency hints from the user message into
                         the proposal when present.

                         To UPDATE an existing task, set
                         ``task_proposal.target_task_id`` to a
                         task_id from the Active Tasks list (NEVER
                         invent ids).

  - ``reference``      — non-actionable knowledge to file. Populate
                         ``reference_proposal.summary``. Slice 6 wires
                         actual filing.

  - ``calendar_only``  — temporal-marker events. Populate
                         ``calendar_proposal.title`` and (when known)
                         ``datetime``.

  - ``delete``         — safe to drop. Populate ``delete_reason``.

Inline sends are usually single-shot — one selection, one record.
Multi-record output is rarer here than for journal threads but valid
(e.g., a selection mentioning both a task and a calendar event).
Empty ``records: []`` means "no record produced" (ambient — the user
clicked send-to-agent on something not actionable).

## Refusal

When you can't commit a verdict (project ambiguity, can't tell if
actionable), set ``refusal`` instead of producing records:

  refusal: {"question": "Which project does this belong to?",
            "missing_context": ["project"]}

The Resolution Surface renders this as a clarification card; the
user's answer re-queues the Clarify pass. ``refusal`` and ``records``
are mutually exclusive.

## Heeding the user's hint

The user CAN provide a hint when sending the selection. Weight it
heavily — it's direct intent. If the hint says "this is a reference
about ECG papers," route to ``reference`` even if the surface text
looks like an actionable thought.

## Required fields

  - ``rationale`` (required): one to three sentences.
  - ``group_intent`` (required): short noun phrase (≤8 words) naming
                                 what the selection is *about*, not
                                 what to do with it.
  - ``confidence`` (optional): 0.0–1.0.

If genuinely uncertain after weighting the hint, prefer ``refusal``
over a low-confidence verdict. The downstream resurfacing system
relies on verdicts being trustworthy.

## Risk profile (Slice 4)

For every ``task`` record, populate ``task_proposal.risk_profile``
with the four-dimension + three-amplifier assessment. The downstream
resolver uses this against the user's tolerance to decide how far the
agent may take this autonomously. Inline-sent items are USER-INITIATED
— the user just clicked send-to-agent — which is a strong signal that
``inference_uncertainty`` is at most ``medium`` (the user picked this
text on purpose). But errors are still possible (wrong project guess,
ambiguous intent), so set ``high`` when applicable.

  - ``financial_cents``: 0 unless the task involves spending.
  - ``privacy``: ``none`` (local) | ``internal`` | ``public``.
  - ``accuracy``: ``low_stakes`` | ``consequential`` | ``critical``.
  - ``compute``: ``instant`` | ``background`` | ``expensive``.
  - ``reversibility``: ``trivial`` | ``moderate`` | ``irreversible``.
  - ``regret_potential``: ``low`` | ``medium`` | ``high``.
  - ``inference_uncertainty``: default ``medium`` for inline sends;
    ``low`` only when the hint or selection unambiguously names the
    action; ``high`` when project/tone/intent are guessed.

Be honest. A user-initiated capture with high regret potential still
gates at the regret amplifier — V2b (honest signaling) trumps the
"the user clicked send so it's fine" instinct. If you can't classify
confidently, leave ``risk_profile`` unset (safe-profile fallback);
NEVER fabricate a permissive profile.
"""


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
    """Run a Clarify pass over one user-sent selection.

    Args:
        file_path: Vault-relative source path (for provenance).
        selection: The user's literal selection; falls back to paragraph.
        paragraph: Surrounding paragraph (used when selection is empty).
        cursor_line: 0-indexed cursor line in the source file.
        hint: Optional user-typed intent hint from the modal.
        force: Default ``True`` — user-initiated clicks should re-run.
        tier: Starting LLM tier. Defaults to FRONTIER_BALANCED (Sonnet).
        enrich: Include the user-context packet
            (tasks / contracts / projects / commits). Default True.
        dry_run: Collect the item, skip the LLM calls.

    Returns:
        Status dict (see :class:`ProducerResult.to_dict`).
    """
    if isinstance(tier, str) and not isinstance(tier, ModelTier):
        try:
            tier = ModelTier(tier)
        except ValueError as exc:
            raise ValueError(
                f"Unknown tier {tier!r}. Valid: {[t.value for t in ModelTier]}"
            ) from exc

    def _collect() -> tuple[list[TriageItem], str | None]:
        from work_buddy.clarify.adapters.inline import collect_inline_selection
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

    from work_buddy.clarify.config import is_verdict_pass_enabled_for, load_triage_config
    cfg = load_triage_config()
    verdict_pass_enabled = is_verdict_pass_enabled_for(cfg, "inline")

    if verdict_pass_enabled:
        from work_buddy.clarify.recommend import build_triage_context
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
    else:
        def _agent(item: TriageItem, run_id: str) -> dict[str, Any]:
            return {
                "content": "",
                "error": (
                    "verdict_pass disabled but agent invoked — this is a bug"
                ),
                "error_kind": "verdict_pass_disabled",
            }

    producer = BackgroundTriageProducer(
        adapter_name="inline_triage",
        source="inline",
        collect=_collect,
        agent=_agent,
        enrich=False,
        verdict_pass_enabled=verdict_pass_enabled,
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
    """Run the Slice 3 Clarify pipeline for one inline selection."""
    from work_buddy.clarify.capabilities.triage_submit import triage_submit
    from work_buddy.clarify.deadline_extract import (
        extract_deadline_hints,
        merge_hints_into_records,
    )
    from work_buddy.clarify.verdict_call import call_for_verdict

    # Deadline pre-pass.
    hints = extract_deadline_hints(
        item.text or "",
        message_date=_date_cls.today(),
        item_id=item.id,
    )

    user_prompt = _render_item_prompt(
        item=item, run_id=run_id, context=context, deadline_hints=hints,
    )

    resp = call_for_verdict(
        runner=runner,
        tier=tier,
        system=_AGENT_SYSTEM_PROMPT,
        user=user_prompt,
        output_schema=MULTI_RECORD_VERDICT_SCHEMA,
        required_fields=("rationale", "group_intent"),
        caller="inline_clarify",
        item_id=item.id,
    )

    if resp.is_error():
        logger.warning(
            "inline_clarify: LLM failed for item %s on tier %s (%s): %s",
            item.id, resp.tier_used, resp.error_kind, resp.error,
        )
        return {
            "content": resp.content,
            "error": resp.error,
            "error_kind": resp.error_kind.value if resp.error_kind else None,
        }

    verdict = resp.structured_output or {}
    if "records" in verdict:
        verdict["records"] = merge_hints_into_records(
            verdict.get("records"), hints,
        )

    submit_kwargs = verdict_to_submit_kwargs(verdict)
    submit_result = triage_submit(
        run_id=run_id,
        item_id=item.id,
        **submit_kwargs,
    )

    if submit_result.get("status") != "ok":
        logger.warning(
            "inline_clarify: triage_submit rejected verdict for item %s: %s",
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
        "deadline_hints": hints,
    }


def _render_item_prompt(
    *,
    item: TriageItem,
    run_id: str,
    context: dict[str, Any],
    deadline_hints: dict[str, Any] | None = None,
) -> str:
    """Compose the per-item user prompt with file + hint + user context + hints."""
    from work_buddy.clarify.recommend import render_triage_context_block

    meta = item.metadata or {}
    file_path = meta.get("file_path", "") or "(unknown)"
    cursor_line = meta.get("cursor_line", 0)
    hint = meta.get("hint", "") or "(none)"

    context_block = render_triage_context_block(context) if context else ""
    context_block = f"\n\n{context_block}\n" if context_block else ""

    hints_block = ""
    if deadline_hints:
        if deadline_hints.get("hint_extraction_failed"):
            hints_block = (
                "\nDeadline hints: extraction failed; rely on the selection "
                "text itself.\n"
            )
        elif (
            deadline_hints.get("has_deadline")
            or deadline_hints.get("has_dependency")
        ):
            parts = []
            if deadline_hints.get("has_deadline"):
                d = deadline_hints.get("deadline_date") or "(date unspecified)"
                parts.append(f"deadline: {d}")
            if deadline_hints.get("has_dependency"):
                dep = deadline_hints.get("dependency_hint") or "(dependency unspecified)"
                parts.append(f"dependency: {dep}")
            hints_block = f"\nDeadline hints (pre-extracted): {'; '.join(parts)}\n"
        else:
            hints_block = "\nDeadline hints: none detected.\n"

    return (
        f"Item id: {item.id}\n"
        f"File: {file_path}:{cursor_line}\n"
        f"Hint: {hint}\n"
        f"{hints_block}"
        f"\n--- Selection ---\n"
        f"{item.text.strip()}\n"
        f"--- End ---"
        f"{context_block}"
    )
