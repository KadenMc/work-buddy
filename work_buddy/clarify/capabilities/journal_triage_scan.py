"""``journal_triage_scan`` capability — background-triage producer entrypoint.

Thin adapter around :class:`BackgroundTriageProducer`. Cadence is a
sidecar-job concern (see ``sidecar_jobs/journal-triage-scan.md``),
not a property of the capability itself.

Slice 3 flow:
- :func:`work_buddy.clarify.adapters.journal.collect_same_day_candidates`
  segments today's Running Notes into thread candidates.
- For each candidate, the cheap deadline-extraction Haiku pass
  (:mod:`work_buddy.clarify.deadline_extract`) detects deadline +
  dependency mentions. Hints get merged into the resulting
  records[].task_proposal so Slice 8's resurfacing has the data even
  for sparse captures.
- The main Clarify pass calls Sonnet via :class:`LLMRunner` with the
  new multi-record schema (:data:`MULTI_RECORD_VERDICT_SCHEMA`).
  The parsed verdict is written directly to the Review pool via
  :func:`triage_submit`.
- Backend-error escalation: TIMEOUT / CONTEXT_EXCEEDED / EMPTY_CONTENT /
  RATE_LIMITED → FRONTIER_BEST (Opus).
- Validation-failure escalation: verdict parsed but missing required
  fields at a tier below FRONTIER_BEST → one retry at FRONTIER_BEST
  before giving up with :data:`ErrorKind.VALIDATION_FAILED`.
  Handled by :func:`work_buddy.clarify.verdict_call.call_for_verdict`.

Registered as a capability-type sidecar cron job. Safe to call
manually via ``wb_run`` for smoke tests or ad-hoc runs.
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
You are running the Clarify step on one thread from a daily running-notes
journal. Given the thread, the user's current-context block, IR hits for
related prior content, and pre-extracted deadline / dependency hints,
produce a verdict in the multi-record schema.

A captured thread can produce ZERO OR MORE records. Each record has a
``destination`` (one of: ``task``, ``reference``, ``calendar_only``,
``delete``) and a destination-specific proposal.

## Destination guide

  - ``task``           — actionable work the user needs to track.
                         Populate ``task_proposal``: required
                         ``suggested_task_text``; optional Slice 2
                         metadata (``kind``, ``outcome_text``,
                         ``next_action_text``, ``definition_of_done``,
                         ``creation_effort``, ``user_involvement``);
                         deadline / dependency hints (the user-message
                         block carries pre-extracted hints — copy them
                         into the proposal when the underlying text
                         actually mentions them).

                         For tracking work that's an UPDATE to an
                         existing task in Active Tasks, set
                         ``task_proposal.target_task_id`` to that exact
                         task_id (NEVER invent ids; copy verbatim).
                         The system records the captured text into the
                         existing task's note rather than creating a
                         new one. Use only when the thread is about
                         the same system, same subject, same intent
                         — loose keyword overlap is NOT enough; quote
                         the matching task title phrase in the
                         rationale.

  - ``reference``      — non-actionable knowledge to file (a paper to
                         remember, a snippet, a quote). Populate
                         ``reference_proposal.summary``. Slice 6 wires
                         actual filing — for now we just persist the
                         summary.

  - ``calendar_only``  — pure temporal-marker events that don't deserve
                         task-class infrastructure (a friend's
                         birthday with no prep needed, a holiday, a
                         meeting reminder where the agent can't
                         actually take action). Populate
                         ``calendar_proposal.title`` and
                         ``calendar_proposal.datetime`` when known.

  - ``delete``         — safe to drop. Populate ``delete_reason``
                         briefly (one sentence). Use when the captured
                         text is already-handled, redundant with an
                         existing record, or genuinely throwaway.

## Multi-record output

A single thread can produce several records. Examples:

- "Sarah's 30th birthday party on May 12 — need to buy gift" produces
  TWO records: a ``calendar_only`` for the party AND a ``task`` for
  the gift. The records share context but route independently.

- "Random idea: should we tighten the threshold on Figure 3?
  See the Smith 2024 paper for prior art" might produce TWO records:
  a ``task`` to investigate the threshold AND a ``reference`` to
  remember the paper.

Most threads produce ONE record. Some produce ZERO (ambient observation
with no actionable component) — return ``records: []`` for those. The
system treats empty records the same as the old ``leave`` action.

## Refusal (instead of records)

When you don't have enough context to commit a verdict — most commonly
when the project assignment is ambiguous or you can't tell whether
something is actionable — set ``refusal`` instead of producing records:

  refusal: {
    "question": "Which project does this belong to: <best-guess A> or <B>?",
    "missing_context": ["project"]
  }

The Resolution Surface renders this as a clarification card; the
user's answer re-queues the Clarify pass with the answer as a forced
context. ``refusal`` and ``records`` are mutually exclusive — use one
or the other, never both.

USE REFUSAL HONESTLY. The downstream resurfacing system relies on
verdicts being trustworthy. A refusal is a small cost; a wrong verdict
is a much bigger one (the user may not catch it in review).

## Required fields

  - ``rationale`` (required): one to three sentences explaining the
                              verdict. Cite specific thread content
                              so the reviewer can verify your
                              reasoning.
  - ``group_intent`` (required): short noun phrase (≤8 words) naming
                                 the underlying intent (NOT the
                                 destination, NOT the action). Used as
                                 the card title in the Resolution
                                 Surface.
  - ``confidence`` (optional): 0.0–1.0 self-assessed.

## Context block

The user message includes a ``## User's Current Context`` block with
active tasks, contracts, projects. READ IT BEFORE DECIDING. If the
thread references an Active Contract or Project, say so in the
rationale. IR hits below the thread are semantic neighbours — use them
as supporting evidence, particularly for ``record_into_task``
matching.

## group_intent (required)

A short noun-phrase (3–8 words) naming the UNDERLYING INTENT behind
the thread — NOT a destination name, NOT a restatement of the thread's
opening line. Shown as the card title in the Resolution Surface, so
it should help the user recognize which of their own thoughts this is
about at a glance.

Good:
  thread: "Background — weekly check of ETFs/stocks — prices and news?"
    → group_intent: "ETF/stock weekly tracking habit"
  thread: "In our search tool, do we have an optional operation param..."
    → group_intent: "search-tool filter API design"

Bad:
  - "Create task"             (that's a destination, not the intent)
  - "Thread asks about ETFs"  (that's the rationale)
  - the full first line of the thread verbatim
  - leaving the field empty

## Risk profile (Slice 4)

For every ``task`` record, populate ``task_proposal.risk_profile`` with
a four-dimension + three-amplifier assessment of what would happen if
the agent took this task end-to-end. The downstream resolver
(``work_buddy.automation.risk``) reads this against the user's
configured tolerance to decide how far the agent may go autonomously.

  - ``financial_cents``: estimated max spend in cents. 0 for purely
    informational or in-vault work. Use cents (50 ≠ $50).
  - ``privacy``: ``none`` (local-only) | ``internal`` (trusted
    services like calendar, vault) | ``public`` (sent email, public
    commit). Most tasks are ``none`` or ``internal``.
  - ``accuracy``: ``low_stakes`` (tab close, draft) | ``consequential``
    (refactor, structural change) | ``critical`` (medical, legal,
    publication-bound claim).
  - ``compute``: ``instant`` | ``background`` (cron-class, <5min) |
    ``expensive`` (ML training, ≥$1 cost). Default ``background``
    when uncertain — most agent tasks fit there.
  - ``reversibility``: ``trivial`` | ``moderate`` (git-revert
    possible) | ``irreversible`` (sent email, deleted thing,
    committed transaction).
  - ``regret_potential``: ``low`` | ``medium`` | ``high``. High when
    the action would be embarrassing or relationship-damaging if
    wrong (email under user's name, public-facing post,
    decision-on-user's-behalf).
  - ``inference_uncertainty``: ``low`` | ``medium`` | ``high``.
    DEFAULT ``medium`` for any task you didn't see the user invoke
    in the same thread. Set ``high`` when you're guessing about
    project assignment, tone, or whether the user wants this done.
    Set ``low`` only when the user explicitly says "do X to Y."

Be honest. The user reads this to calibrate trust. Underestimating
high-regret or high-uncertainty work is a worse failure than being
slightly conservative — V2b (honest signaling) is the load-bearing
value here.

If you can't classify confidently, leave ``risk_profile`` unset; the
system falls back to a conservative safe-profile that caps autonomy
at the lowest dimension level. NEVER fabricate a permissive profile
to make the user's queue look smaller.

## Action contexts (Slice 5a)

For every ``task`` record, populate BOTH context lists. The resolver
uses them to answer "who can act now?" against the live tool-status
cache:

  - ``task_proposal.agent_required_contexts``: tokens describing what
    the AGENT needs to act on this autonomously. Examples:
    ``@filesystem`` (read/write project files), ``@vault`` (Obsidian
    bridge), ``@email_send`` (send under user's identity),
    ``@web_public`` (WebFetch / WebSearch), ``@github`` (gh CLI / web).
    Empty array = the user does this work; the agent contributes
    nothing.

  - ``task_proposal.user_required_contexts``: tokens describing the
    environment the USER needs to be in. Examples:
    ``@user_workstation`` (at their dev machine),
    ``@phone_voice`` (making a call), ``@user_creds`` (signed into a
    portal — banking, CRA, healthcare), ``@in_person`` (physically
    present), ``@physical`` (anything bodily). Empty = the agent
    handles this without the user.

  - ``task_proposal.required_contexts_source``: set to
    ``"agent_inferred"``. The dashboard flips this to
    ``"user_authored"`` if the user edits the lists.

Context registry (the resolver knows these tokens; you may invent
new ones for forward-compat — they'll resolve to user-only until the
registry catches up):

  user-only       : @physical, @in_person, @phone_voice,
                    @user_creds, @user_workstation, @cluster
  universal       : @filesystem, @web_public, @llm, @github
  probe-gated     : @vault → obsidian
                    @email_send → thunderbird
                    @email_read → thunderbird
                    @chrome_active → chrome_extension

Heuristics:

  - Code / file edits        → agent: [@filesystem],
                                user:  [@user_workstation]
  - Send email under user    → agent: [@email_send],
                                user:  [@email_send]
  - Read user's email        → user:  [@email_read]  (agent reads via
                                                       Thunderbird only
                                                       once user is at
                                                       desk)
  - Phone call               → agent: [],
                                user:  [@phone_voice]
  - Banking / portal task    → agent: [],
                                user:  [@user_creds, @user_workstation]
  - Vault edit               → agent: [@vault, @filesystem],
                                user:  []
  - Browser-mediated triage  → agent: [@chrome_active],
                                user:  [@user_workstation]
  - Pure-physical errand     → agent: [],
                                user:  [@physical]
  - Information lookup       → agent: [@web_public],
                                user:  []

When in doubt, prefer FEWER contexts (avoid over-restricting). Both
lists are joined by AND; either may be empty. The resolver caps
achievable tier at 1 ("suggest only") whenever the agent can't
satisfy its list — the surface then renders a handoff card if the
user's side is satisfied (per ROADMAP §3.2 "agent is blocked, not
user"). If you OMIT both lists, the resolver falls back to the
Slice-4 risk-only behavior (no context-cap applied).
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
    """Run a single Clarify pass over the journal's same-day notes.

    Args:
        journal_date: ``YYYY-MM-DD`` or ``None`` for today.
        force: Ignore the unchanged-content idempotence gate.
        profile: Override the configured ``triage.segment_profile``
            (the segmentation call — agent LLM is now tier-driven).
        tier: Starting LLM tier for the Clarify pass. Defaults to
            FRONTIER_BALANCED (Sonnet); escalates to FRONTIER_BEST
            (Opus) on TIMEOUT / CONTEXT_EXCEEDED / EMPTY_CONTENT /
            RATE_LIMITED.
        enrich: Pre-fetch hybrid-IR context for each candidate.
            Default True.
        dry_run: Collect candidates and enrich, but skip the LLM
            calls. Returns what would have been sent.

    Returns:
        Status dict (see :class:`ProducerResult.to_dict`).
    """
    from work_buddy.clarify.config import (
        is_verdict_pass_enabled_for, load_triage_config, resolve_profile,
    )

    # Accept raw-string tier from MCP callers; enum callers pass through.
    if isinstance(tier, str) and not isinstance(tier, ModelTier):
        try:
            tier = ModelTier(tier)
        except ValueError as exc:
            raise ValueError(
                f"Unknown tier {tier!r}. Valid: {[t.value for t in ModelTier]}"
            ) from exc

    cfg = load_triage_config()
    seg_profile = resolve_profile(cfg, "segment", override=profile)
    enrich_cfg = cfg.get("enrich", {}) or {}

    # Build the "what the user is actively working on" registry.
    ctx_cfg = cfg.get("triage_context", {}) or {}
    try:
        from work_buddy.clarify.recommend import (
            build_triage_context, render_triage_context_block,
        )
        triage_context = build_triage_context(
            task_states=ctx_cfg.get(
                "task_states", ["focused", "mit", "inbox"],
            ),
            max_tasks=ctx_cfg.get("max_tasks", 12),
        )
        if not ctx_cfg.get("include_recent_commits", False):
            triage_context.pop("recent_commits", None)
        triage_context_block = render_triage_context_block(triage_context)
    except Exception as exc:
        logger.warning("journal_triage_scan: build_triage_context failed: %s", exc)
        triage_context_block = ""

    def _collect() -> tuple[list[TriageItem], str | None]:
        from work_buddy.clarify.adapters.journal import (
            collect_same_day_candidates,
        )
        return collect_same_day_candidates(
            journal_date=journal_date, profile=seg_profile,
        )

    if dry_run:
        items, ch = _collect()
        if enrich and items:
            from work_buddy.clarify.enrich import enrich_with_ir_context
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

    verdict_pass_enabled = is_verdict_pass_enabled_for(cfg, "journal")

    if verdict_pass_enabled:
        runner = LLMRunner()

        def _agent(item: TriageItem, run_id: str) -> dict[str, Any]:
            return _invoke_agent(
                runner=runner,
                item=item,
                run_id=run_id,
                tier=tier,
                triage_context_block=triage_context_block,
                journal_date=journal_date,
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
        adapter_name="journal_triage",
        source="journal_thread",
        collect=_collect,
        agent=_agent,
        enrich=enrich and enrich_cfg.get("enabled", True),
        ir_top_k=enrich_cfg.get("top_k", 5),
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
    tier: ModelTier,
    triage_context_block: str = "",
    journal_date: str | None = None,
) -> dict[str, Any]:
    """Run the Slice 3 Clarify pipeline for one journal thread.

    Two passes in sequence:

    1. **Deadline pre-pass** (Haiku, cheap). Extracts has_deadline /
       deadline_date / has_dependency / dependency_hint from the
       thread text. Failures degrade gracefully (return all-false
       sentinel).
    2. **Main Clarify pass** (Sonnet, escalates to Opus on backend or
       validation failure). Produces the multi-record verdict. The
       deadline hints are merged into resulting task_proposals
       post-call so the Sonnet output never has to redo the
       extraction work.
    """
    from work_buddy.clarify.capabilities.triage_submit import triage_submit
    from work_buddy.clarify.deadline_extract import (
        extract_deadline_hints,
        merge_hints_into_records,
    )
    from work_buddy.clarify.verdict_call import call_for_verdict

    # Pass 1: deadline / dependency hints.
    msg_date: _date_cls | str | None = journal_date
    if not msg_date:
        # Use thread's source_dates if the adapter populated them,
        # else today.
        meta = item.metadata or {}
        dates = meta.get("source_dates") or []
        msg_date = dates[0] if dates else _date_cls.today()
    hints = extract_deadline_hints(
        item.text or "",
        message_date=msg_date,
        item_id=item.id,
    )

    # Pass 2: main Clarify verdict.
    user_prompt = _render_item_prompt(
        item=item, run_id=run_id,
        triage_context_block=triage_context_block,
        deadline_hints=hints,
    )

    resp = call_for_verdict(
        runner=runner,
        tier=tier,
        system=_AGENT_SYSTEM_PROMPT,
        user=user_prompt,
        output_schema=MULTI_RECORD_VERDICT_SCHEMA,
        # Slice 3: the multi-record schema requires rationale +
        # group_intent. records / refusal are checked at the submit
        # layer (at least one must be set).
        required_fields=("rationale", "group_intent"),
        caller="journal_clarify",
        item_id=item.id,
    )

    if resp.is_error():
        logger.warning(
            "journal_clarify: LLM failed for item %s on tier %s (%s): %s",
            item.id, resp.tier_used, resp.error_kind, resp.error,
        )
        return {
            "content": resp.content,
            "error": resp.error,
            "error_kind": resp.error_kind.value if resp.error_kind else None,
        }

    verdict = resp.structured_output or {}
    # Merge deadline hints into task records.
    if "records" in verdict:
        verdict["records"] = merge_hints_into_records(
            verdict.get("records"), hints,
        )

    # Submit. triage_submit accepts both shapes; multi-record fields
    # come through verdict_to_submit_kwargs.
    submit_kwargs = verdict_to_submit_kwargs(verdict)
    submit_result = triage_submit(
        run_id=run_id,
        item_id=item.id,
        **submit_kwargs,
    )

    if submit_result.get("status") != "ok":
        logger.warning(
            "journal_clarify: triage_submit rejected verdict for item %s: %s",
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
    triage_context_block: str = "",
    deadline_hints: dict[str, Any] | None = None,
) -> str:
    """Compose the per-item user prompt with context + IR + hints inlined.

    Prompt layout (top-to-bottom):
      1. Triage run id + item id (for the agent to copy into submit)
      2. Thread source dates (if any)
      3. Global "User's Current Context" block (active tasks,
         contracts, projects, recent commits) — enables the agent
         to pick a real ``target_task_id`` for record-into-task.
      4. Deadline / dependency hints (Slice 3 pre-pass output)
      5. Thread content itself
      6. Per-item IR hits (semantic neighbours of this thread)
    """
    from work_buddy.clarify.enrich import render_ir_context

    meta = item.metadata or {}
    ir_block = render_ir_context(meta.get("ir_context", []) or [])
    ir_block = f"\n\nSupporting context (pre-fetched):\n{ir_block}\n" if ir_block else ""

    dates = ", ".join(meta.get("source_dates", []) or [])
    date_line = f"Thread source date(s): {dates}\n" if dates else ""

    ctx_block = f"\n{triage_context_block}\n" if triage_context_block else ""

    hints_block = ""
    if deadline_hints:
        if deadline_hints.get("hint_extraction_failed"):
            hints_block = (
                "\nDeadline hints: extraction failed; rely on the thread "
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
                hint = deadline_hints.get("dependency_hint") or "(dependency unspecified)"
                parts.append(f"dependency: {hint}")
            hints_block = f"\nDeadline hints (pre-extracted): {'; '.join(parts)}\n"
        else:
            hints_block = "\nDeadline hints: none detected.\n"

    return (
        f"Item id: {item.id}\n"
        f"{date_line}"
        f"{ctx_block}"
        f"{hints_block}"
        f"\n--- Thread content ---\n"
        f"{item.text.strip()}\n"
        f"--- End thread ---"
        f"{ir_block}"
    )
