"""``journal_triage_scan`` capability — background-triage producer entrypoint.

Thin adapter around :class:`BackgroundTriageProducer`. Cadence is a
sidecar-job concern (see ``sidecar_jobs/journal-triage-scan.md``),
not a property of the capability itself.

- Uses :func:`work_buddy.triage.adapters.journal.collect_same_day_candidates`
  to get today's thread candidates.
- Runs each candidate through a local-LLM agent loop using the
  ``triage_agent`` tool preset and the configured
  ``triage.agent_profile``.
- The agent's only submission path is the ``triage_submit`` tool.
  No tool call → the run is recorded as ``unsubmitted`` and the
  item is not added to the pool.

Registered as a capability-type sidecar cron job. Safe to call
manually via ``wb_run`` for smoke tests or ad-hoc runs.
"""

from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.triage.background import BackgroundTriageProducer
from work_buddy.triage.items import TriageItem

logger = get_logger(__name__)


_AGENT_SYSTEM_PROMPT = """\
You are triaging one thread from a daily running-notes journal.

Your job: decide the single best next action and record it by
calling the work-buddy gateway.

## How to call work-buddy tools

work-buddy exposes two top-level tools in this session: `wb_run` and
`wb_search`. Every domain capability (triage_submit, task_briefing,
context_search, …) is dispatched through `wb_run` — NOT as its own
top-level tool.

To submit your verdict, call `wb_run` with these exact params:

- `capability`: the string `"triage_submit"`
- `params`: an object containing
  - `run_id` — copy it verbatim from the "Triage run id:" line in
    the user message below. Do NOT invent a value.
  - `item_id` — copy it verbatim from the "Item id:" line. Do NOT
    invent a value.
  - `recommended_action` — one of: `close`, `group`, `create_task`,
    `record_into_task`, `leave`
  - `rationale` — one to three sentences in your own words
  - `group_intent` — optional but strongly preferred; a short (3-8
    word) noun-phrase naming the underlying intent of the thread.
    Used as the card title in the review UI. See the
    `group_intent` section below for examples. Do NOT call `triage_submit`
as a top-level tool (it doesn't exist at that layer). Similarly,
use `wb_search(query="…")` to discover other capabilities if you
want to gather context before deciding.

## Valid recommended_action values

  - "create_task"        (new actionable work — include `suggested_task_text` in params)
  - "record_into_task"   (add detail to an existing task — include `target_task_id` in params)
  - "leave"              (keep in notes as-is; not actionable or already captured)
  - "close"              (safe to drop / already handled)
  - "group"              (belongs with sibling items — include `related_item_ids` in params)

## Using the "User's Current Context" block

The user message includes a `## User's Current Context` block listing
the user's currently-active tasks (with IDs like `t-abc123`),
contracts, projects, and recent commits. Read it BEFORE deciding.

- DEFAULT TO `create_task` for any new actionable thread. Only
  pick `record_into_task` when the thread is UNAMBIGUOUSLY about
  the same specific work as one of the Active Tasks — same system,
  same subject, same intent. A loose keyword overlap is NOT enough.
  A thread about "ETFs" does NOT belong in a task about
  "dashboard columns" just because both exist in the list.
- If you choose `record_into_task`, quote the precise phrase from
  the task title that matches the thread in your rationale. If you
  can't, the match is too weak — switch to `create_task`.
- Never invent task IDs. Copy the exact ID from the block.
- If the thread references an Active Contract or Project, say so in
  the rationale so the reviewer can see the linkage.

## About `group_intent` (required!)

`group_intent` is a short noun-phrase (3-8 words) naming the
**underlying intent** behind the thread — NOT the action name and
NOT a restatement of the thread's opening line. It's what shows up
as the card title in the review UI, so it should help the human
recognize which of their own thoughts this is about at a glance.

Good examples:
  thread: "- Background - weekly check of ETFs/stocks - prices and news?"
    → group_intent: "ETF/stock weekly tracking habit"
  thread: "- In our search tool, do we have some kind of optional `operation` param..."
    → group_intent: "search-tool filter API design"
  thread: "- Need to migrate embedding service off CPU"
    → group_intent: "migrate embedding service to GPU"

Bad examples (do NOT do these):
  - "Create task" ........ (that's the action, not the intent)
  - "The thread asks about ETF prices" ........ (that's the rationale)
  - the full first line of the thread verbatim
  - leaving the field empty

Always include `group_intent`. No exceptions.

## Rules

1. You MUST call `wb_run` with `capability="triage_submit"` exactly
   once before finishing.
2. Read-only lookups via `wb_run` are allowed first (e.g.
   `wb_run(capability="context_search", params={"query": "..."})`)
   but keep it to one or two lookups max.
3. If you are uncertain, pick `"leave"` — do not skip submission.
4. Rationale must be one to three sentences.
5. `group_intent` should be a short noun-phrase distinct from the
   rationale (see "About group_intent" above).
6. Use the pre-fetched IR context inlined below before paying for
   more lookups.
"""


def journal_triage_scan(
    *,
    journal_date: str | None = None,
    force: bool = False,
    profile: str | None = None,
    enrich: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a single background-triage pass over the journal's same-day notes.

    Args:
        journal_date: ``YYYY-MM-DD`` or ``None`` for today.
        force: Ignore the unchanged-content idempotence gate.
        profile: Override the configured ``triage.agent_profile``.
            Primarily useful for tests.
        enrich: Pre-fetch hybrid-IR context for each candidate.
            Default True.
        dry_run: Collect candidates and enrich, but skip the agent
            loop. Returns what would have been sent.

    Returns:
        Status dict (see :class:`ProducerResult.to_dict`).
    """
    from work_buddy.triage.config import load_triage_config, resolve_profile

    cfg = load_triage_config()
    agent_profile = resolve_profile(cfg, "agent", override=profile)
    seg_profile = resolve_profile(cfg, "segment", override=profile)
    enrich_cfg = cfg.get("enrich", {}) or {}
    agent_cfg = cfg.get("agent", {}) or {}

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

    def _agent(item: TriageItem, run_id: str) -> dict[str, Any]:
        return _invoke_agent(
            item=item,
            run_id=run_id,
            profile=agent_profile,
            max_tokens=agent_cfg.get("max_tokens", 1024),
            temperature=agent_cfg.get("temperature", 0.0),
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
    item: TriageItem,
    run_id: str,
    profile: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    triage_context_block: str = "",
) -> dict[str, Any]:
    """Call ``llm_with_tools`` with the triage_agent preset.

    The agent is expected to call ``triage_submit`` exactly once.
    We do not inspect ``tool_calls`` to decide "submitted" —
    ``triage_submit`` writes to the pool, and
    :func:`BackgroundTriageProducer.run` checks the pool directly.

    ``triage_context_block`` is the rendered "User's Current
    Context" block (active tasks / contracts / projects / recent
    commits) from ``recommend.build_triage_context``. Prepended to
    the user prompt so the agent can pick real existing task IDs
    for ``record_into_task`` — same shape Chrome's Sonnet call sees.
    """
    from work_buddy.llm.with_tools import llm_with_tools

    user_prompt = _render_item_prompt(
        item=item, run_id=run_id, triage_context_block=triage_context_block,
    )
    return llm_with_tools(
        system=_AGENT_SYSTEM_PROMPT,
        user=user_prompt,
        profile=profile,
        tool_preset="triage_agent",
        required_capabilities=["triage_submit"],
        max_tokens=max_tokens,
        temperature=temperature,
    )


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
        f"Triage run id: {run_id}\n"
        f"Item id: {item.id}\n"
        f"{date_line}"
        f"{ctx_block}"
        f"\n--- Thread content ---\n"
        f"{item.text.strip()}\n"
        f"--- End thread ---"
        f"{ir_block}"
        f"\nDecide one action for this thread, then submit it by calling "
        f"wb_run with capability='triage_submit' and params including "
        f"run_id={run_id!r} and item_id={item.id!r}."
    )
