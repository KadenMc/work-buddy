"""``journal_triage_scan`` capability — background-triage producer entrypoint.

Thin adapter around :class:`BackgroundTriageProducer`. Cadence is a
sidecar-job concern (see ``sidecar_jobs/journal-triage-scan.md``),
not a property of the capability itself.

Flow:
- :func:`work_buddy.triage.adapters.journal.collect_same_day_candidates`
  segments today's Running Notes into thread candidates.
- Each candidate is IR-enriched and sent to Sonnet via
  :class:`LLMRunner` with a constrained ``output_schema``
  (:data:`work_buddy.triage.verdict_schema.VERDICT_SCHEMA`). The parsed
  verdict is written directly to the Review pool via
  :func:`triage_submit`.
- Escalation: TIMEOUT / CONTEXT_EXCEEDED / EMPTY_CONTENT / RATE_LIMITED
  → FRONTIER_BEST (Opus).

Registered as a capability-type sidecar cron job. Safe to call
manually via ``wb_run`` for smoke tests or ad-hoc runs.
"""

from __future__ import annotations

from typing import Any

from work_buddy.llm import ErrorKind, LLMRunner, ModelTier
from work_buddy.logging_config import get_logger
from work_buddy.triage.background import BackgroundTriageProducer
from work_buddy.triage.items import TriageItem
from work_buddy.triage.verdict_schema import VERDICT_SCHEMA, verdict_to_submit_kwargs

logger = get_logger(__name__)


_AGENT_SYSTEM_PROMPT = """\
You are triaging one thread from a daily running-notes journal. Given
the thread, the user's current-context block, and IR hits for related
prior content, decide the single best next action and fill in the
verdict schema.

## Action selection

  - create_task       — new actionable work. Include ``suggested_task_text``.
                        DEFAULT to this for any actionable thread unless
                        one of the Active Tasks is UNAMBIGUOUSLY about
                        the same work.
  - record_into_task  — add detail to an existing task. Include
                        ``target_task_id`` (from the Active Tasks list —
                        never invent an ID; copy it verbatim). Use only
                        when the thread is about the same system, same
                        subject, same intent. Loose keyword overlap is
                        NOT enough. Quote the matching task title phrase
                        in the rationale.
  - leave             — keep in the note as-is. Observations, questions,
                        or thoughts that don't map to a clear action.
  - close             — safe to drop / already handled.
  - group             — belongs with sibling items already in the pool.
                        Include ``related_item_ids``.

If you are uncertain, pick ``leave``.

## Context

The user message includes a ``## User's Current Context`` block with
active tasks, contracts, projects, and recent commits. READ IT BEFORE
DECIDING. If the thread references an Active Contract or Project,
say so in the rationale. IR hits below the thread are semantic
neighbours — use them as supporting evidence.

## group_intent (required)

A short noun-phrase (3–8 words) naming the UNDERLYING INTENT behind
the thread — NOT the action name, NOT a restatement of the thread's
opening line. Shown as the card title in the review UI, so it should
help the user recognize which of their own thoughts this is about at
a glance.

Good:
  thread: "Background — weekly check of ETFs/stocks — prices and news?"
    → group_intent: "ETF/stock weekly tracking habit"
  thread: "In our search tool, do we have an optional operation param..."
    → group_intent: "search-tool filter API design"

Bad:
  - "Create task"             (that's the action, not the intent)
  - "Thread asks about ETFs"  (that's the rationale)
  - the full first line of the thread verbatim
  - leaving the field empty

## Rationale

One to three sentences. Cite specific thread content so the reviewer
can verify your reasoning.
"""


def journal_triage_scan(
    *,
    journal_date: str | None = None,
    force: bool = False,
    profile: str | None = None,
    tier: ModelTier | str = ModelTier.FRONTIER_BALANCED,
    enrich: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a single background-triage pass over the journal's same-day notes.

    Args:
        journal_date: ``YYYY-MM-DD`` or ``None`` for today.
        force: Ignore the unchanged-content idempotence gate.
        profile: Override the configured ``triage.segment_profile``
            (the segmentation call — agent LLM is now tier-driven).
        tier: Starting LLM tier for the agent. Defaults to
            FRONTIER_BALANCED (Sonnet); escalates to FRONTIER_BEST
            (Opus) on TIMEOUT / CONTEXT_EXCEEDED / EMPTY_CONTENT /
            RATE_LIMITED.
        enrich: Pre-fetch hybrid-IR context for each candidate.
            Default True.
        dry_run: Collect candidates and enrich, but skip the agent
            loop. Returns what would have been sent.

    Returns:
        Status dict (see :class:`ProducerResult.to_dict`).
    """
    from work_buddy.triage.config import load_triage_config, resolve_profile

    # Accept raw-string tier from MCP callers; enum callers pass through.
    if isinstance(tier, str) and not isinstance(tier, ModelTier):
        try:
            tier = ModelTier(tier)
        except ValueError as exc:
            raise ValueError(
                f"Unknown tier {tier!r}. Valid: {[t.value for t in ModelTier]}"
            ) from exc

    cfg = load_triage_config()
    # Segmentation still runs on the local profile — it's a
    # deterministic classification task the local model handles well.
    seg_profile = resolve_profile(cfg, "segment", override=profile)
    enrich_cfg = cfg.get("enrich", {}) or {}

    # Build the "what the user is actively working on" registry once
    # per run. Injected into each per-item agent prompt so the agent
    # can pick a real existing task_id for ``record_into_task`` and
    # reason about which contracts/projects a thread relates to —
    # the same block Chrome triage's Sonnet cluster-level call sees.
    #
    # Scope is narrower than Chrome's call on purpose: per-item
    # prompts go to a smaller local model (Qwen 14B) that
    # degenerates on long contexts. We ship only MIT + focused
    # tasks (active work the user is currently doing), cap at 12,
    # and drop recent_commits entirely — commits are noise for
    # journal thread matching. Inbox tasks are omitted because
    # they're unprocessed backlog; the typical journal thread is
    # about current-focus work or genuinely new material. Cap /
    # state filters live in feature config so they can be tuned
    # without code changes.
    ctx_cfg = cfg.get("triage_context", {}) or {}
    try:
        from work_buddy.triage.recommend import (
            build_triage_context, render_triage_context_block,
        )
        triage_context = build_triage_context(
            task_states=ctx_cfg.get(
                "task_states", ["focused", "mit", "inbox"],
            ),
            max_tasks=ctx_cfg.get("max_tasks", 12),
        )
        # Drop recent_commits for the per-item prompt — they rarely
        # help classify a journal thread and eat tokens.
        if not ctx_cfg.get("include_recent_commits", False):
            triage_context.pop("recent_commits", None)
        triage_context_block = render_triage_context_block(triage_context)
    except Exception as exc:
        logger.warning("journal_triage_scan: build_triage_context failed: %s", exc)
        triage_context_block = ""

    def _collect() -> tuple[list[TriageItem], str | None]:
        from work_buddy.triage.adapters.journal import (
            collect_same_day_candidates,
        )
        return collect_same_day_candidates(
            journal_date=journal_date, profile=seg_profile,
        )

    if dry_run:
        items, ch = _collect()
        if enrich and items:
            from work_buddy.triage.enrich import enrich_with_ir_context
            enrich_with_ir_context(
                items,
                top_k=enrich_cfg.get("top_k", 5),
                source=enrich_cfg.get("source"),
                max_text_chars=enrich_cfg.get("max_text_chars", 600),
            )
        return {
            "status": "dry_run",
            "item_count": len(items),
            "content_hash": ch,
            "items": [it.to_dict() for it in items],
        }

    runner = LLMRunner()

    def _agent(item: TriageItem, run_id: str) -> dict[str, Any]:
        return _invoke_agent(
            runner=runner,
            item=item,
            run_id=run_id,
            tier=tier,
            triage_context_block=triage_context_block,
        )

    producer = BackgroundTriageProducer(
        adapter_name="journal_triage",
        source="journal_thread",
        collect=_collect,
        agent=_agent,
        enrich=enrich and enrich_cfg.get("enabled", True),
        ir_top_k=enrich_cfg.get("top_k", 5),
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
    tier: ModelTier,
    triage_context_block: str = "",
) -> dict[str, Any]:
    """Call the unified runner with a constrained verdict schema.

    The verdict comes back as structured JSON (enforced on Anthropic's
    side). We parse it, call :func:`triage_submit` directly to write
    the pool entry, and return a result dict shaped the way
    :class:`BackgroundTriageProducer` expects (``content`` / ``error``
    / ``error_kind``) so its submission-check path works unchanged.

    ``triage_context_block`` is the rendered "User's Current Context"
    block (active tasks / contracts / projects) from
    :func:`recommend.build_triage_context`. Prepended to the user
    prompt so the agent can pick real existing task IDs for
    ``record_into_task``.
    """
    from work_buddy.triage.capabilities.triage_submit import triage_submit

    user_prompt = _render_item_prompt(
        item=item, run_id=run_id, triage_context_block=triage_context_block,
    )

    resp = runner.call(
        tier=tier,
        system=_AGENT_SYSTEM_PROMPT,
        user=user_prompt,
        output_schema=VERDICT_SCHEMA,
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
            "journal_triage: LLM failed for item %s on tier %s (%s): %s",
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
            "journal_triage: LLM returned no recommended_action for item %s "
            "(tier=%s, content_len=%d)",
            item.id, resp.tier_used, len(resp.content),
        )
        return {
            "content": resp.content,
            "error": "LLM returned no recommended_action",
            "error_kind": ErrorKind.SCHEMA_VIOLATION.value,
        }

    submit_kwargs = verdict_to_submit_kwargs(verdict)
    submit_result = triage_submit(
        run_id=run_id,
        item_id=item.id,
        **submit_kwargs,
    )

    if submit_result.get("status") != "ok":
        logger.warning(
            "journal_triage: triage_submit rejected verdict for item %s: %s",
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


def _render_item_prompt(
    *,
    item: TriageItem,
    run_id: str,
    triage_context_block: str = "",
) -> str:
    """Compose the per-item user prompt with context + IR inlined.

    Prompt layout (top-to-bottom):
      1. Triage run id + item id (for the agent to copy into submit)
      2. Thread source dates (if any)
      3. Global "User's Current Context" block (active tasks,
         contracts, projects, recent commits) — enables the agent
         to pick a real ``target_task_id`` for ``record_into_task``.
      4. Thread content itself
      5. Per-item IR hits (semantic neighbours of this thread)
      6. Closing instruction

    Global context comes BEFORE the thread so the agent reads it
    first and has the task registry in mind while interpreting the
    thread. IR hits come AFTER the thread because they're the
    neighbours OF the thread — order matches reading flow.
    """
    from work_buddy.triage.enrich import render_ir_context

    meta = item.metadata or {}
    ir_block = render_ir_context(meta.get("ir_context", []) or [])
    ir_block = f"\n\nSupporting context (pre-fetched):\n{ir_block}\n" if ir_block else ""

    dates = ", ".join(meta.get("source_dates", []) or [])
    date_line = f"Thread source date(s): {dates}\n" if dates else ""

    ctx_block = f"\n{triage_context_block}\n" if triage_context_block else ""

    return (
        f"Item id: {item.id}\n"
        f"{date_line}"
        f"{ctx_block}"
        f"\n--- Thread content ---\n"
        f"{item.text.strip()}\n"
        f"--- End thread ---"
        f"{ir_block}"
    )
