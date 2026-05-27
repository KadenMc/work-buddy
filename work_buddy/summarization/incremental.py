"""Incremental refresh — the v2 producer's core algorithm.

This module implements PRD §5.1: given a session that has been observed and
partially summarized, refresh only the trailing-topic-onwards region. Prior
finalized topics are immutable and provided as compressed context.

The flow:

1. Load existing topics from the store (if any).
2. Look up `total_turns` from the source.
3. Compute `finalized_count` — how many existing children are immutable
   (distance-based heuristic from the strategy).
4. Determine `fresh_from_turn` = `max(span_end of all prior topics) + 1`.
5. Render the fresh tail (`source.render_from(item_id, fresh_from_turn)`).
6. **Pathway selection** (P3): if predicted input tokens fit within
   `pathway_threshold_ratio × per_call_budget`, single-call. Otherwise
   chunked (segment fresh tail → per-chunk LLM call → topics accumulate
   into the next call's context).
7. Build the incremental user prompt for each call:
   - finalized topics as compressed context (titles + 1-line summaries + keywords)
   - trailing topic (if any) at higher detail
   - fresh raw turns (or this chunk's portion)
8. Call the LLM with the strategy's incremental schema. Model chain
   escalation is handled inside the LLMCaller; this module records which
   model actually produced the output via the `model` field on
   `LLMCallResult`.
9. Parse the response into a `SummaryNode` containing `trailing_and_new_topics`.
10. Merge via `store.apply_incremental(finalized_count=...)`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from work_buddy.summarization.protocol import (
    LLMCaller,
    Provenance,
    SummarizationError,
    SummaryCapability,
    SummaryNode,
)

logger = logging.getLogger(__name__)


# Default per-tier context budgets (PRD OQ9). Used by pathway selection
# and (in future) pre-flight gating. Single source of truth.
_TIER_BUDGETS_TOKENS: dict[str, int] = {
    # Local tier: conservative; most local models cap ~8-32k native.
    "local_general": 8_000,
    "local_fast": 8_000,
    "local_tool_calling": 8_000,
    # Haiku-class: 200k native, but we cap at 32k to keep incremental cheap.
    "frontier_fast": 32_000,
    # Sonnet/Opus retained for completeness; dropped per PRD OQ9 as defaults.
    "frontier_balanced": 64_000,
    "frontier_best": 64_000,
}
_DEFAULT_PER_CALL_BUDGET_TOKENS = 32_000  # tracks frontier_fast default
_PATHWAY_THRESHOLD_RATIO = 0.85  # PRD OQ17
_CHARS_PER_TOKEN = 4              # rough conversion for content estimation


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def refresh_one_incremental(
    summarizer,
    item_id: str,
    *,
    freshness_token: Any,
    llm_caller: LLMCaller,
    profile: str | None = None,
) -> SummaryNode | None:
    """Run one incremental refresh against the given item.

    Returns the merged `SummaryNode` tree after save, or `None` if there was
    nothing to do (empty session, no fresh turns) or the LLM/parse failed.

    The caller is expected to have already verified that the item is stale —
    this function does not consult `store.is_fresh`.
    """
    from work_buddy.summarization.orchestrator import (
        build_error_provenance,
        build_provenance,
    )

    strategy = summarizer.strategy
    source = summarizer.source
    store = summarizer.store

    if SummaryCapability.INCREMENTAL not in strategy.capabilities:
        raise SummarizationError(
            f"refresh_one_incremental requires an INCREMENTAL strategy; "
            f"got {strategy.name!r}"
        )

    # --- Load prior state -------------------------------------------------
    prior_root = store.load(item_id)
    prior_topics: list[SummaryNode] = (
        list(prior_root.children) if prior_root else []
    )

    # `total_turns` is a duck-typed method on the source (validated by
    # coherence check). For SessionSource it's the count from observed_sessions.
    total_turns = source.total_turns(item_id)
    if total_turns is None or total_turns == 0:
        return None  # nothing to summarize

    # --- Compute finalization boundary -----------------------------------
    finalized_count = _compute_finalized_count(
        prior_topics, total_turns, strategy,
    )
    last_finalized_end = -1
    if finalized_count > 0:
        last_finalized_end = max(
            (
                int(t.extra.get("span_end", -1))
                for t in prior_topics[:finalized_count]
                if isinstance(t.extra.get("span_end"), int)
            ),
            default=-1,
        )

    # The fresh tail starts AFTER the highest span_end seen so far across all
    # prior topics — including the trailing topic. The trailing topic is fed
    # to the LLM as mutable context; the LLM can extend it OR emit new
    # topics. Either way, the raw turns the LLM needs to see are the ones
    # that don't appear in ANY existing topic.
    highest_covered_end = max(
        (
            int(t.extra.get("span_end", -1))
            for t in prior_topics
            if isinstance(t.extra.get("span_end"), int)
        ),
        default=-1,
    )
    fresh_from_turn = highest_covered_end + 1

    if fresh_from_turn >= total_turns:
        # Every turn already lives in some topic — no fresh content. Don't
        # burn an LLM call.
        return prior_root

    # --- Pathway selection (P3) ------------------------------------------
    # We estimate the prompt size BEFORE rendering the full fresh tail —
    # the per-turn average from total_turns vs. the session's char span lets
    # us choose single-call vs. chunked without paying for a full render.
    finalized_topics = prior_topics[:finalized_count]
    trailing_topic = (
        prior_topics[finalized_count]
        if finalized_count < len(prior_topics)
        else None
    )
    context_token_estimate = _estimate_topic_context_tokens(
        finalized_topics, trailing_topic,
    )
    # Budget: read from config if available; fallback to frontier_fast default.
    budget_tokens = _resolve_per_call_budget()
    fresh_budget_tokens = (
        int(budget_tokens * _PATHWAY_THRESHOLD_RATIO) - context_token_estimate
    )
    if fresh_budget_tokens <= 0:
        # Context is already over budget — fail loudly rather than truncating
        # the prior-topic context silently.
        raise SummarizationError(
            f"Prior-topic context (~{context_token_estimate} tokens) exceeds "
            f"per-call budget (~{budget_tokens} × {_PATHWAY_THRESHOLD_RATIO}); "
            f"finalization heuristic may need re-tuning"
        )

    # Quick fresh-tail size probe via render_from (which respects the 40k
    # char cap; if a session is much larger we'll chunk regardless).
    fresh_text_probe = source.render_from(item_id, fresh_from_turn)
    if not fresh_text_probe:
        return prior_root
    fresh_text_tokens = _estimate_tokens(fresh_text_probe)

    if fresh_text_tokens <= fresh_budget_tokens:
        # SINGLE-CALL pathway: fresh tail fits in one shot.
        return _refresh_single_call(
            summarizer=summarizer,
            item_id=item_id,
            finalized_count=finalized_count,
            finalized_topics=finalized_topics,
            trailing_topic=trailing_topic,
            fresh_text=fresh_text_probe,
            fresh_from_turn=fresh_from_turn,
            last_finalized_end=last_finalized_end,
            total_turns=total_turns,
            llm_caller=llm_caller,
            profile=profile,
            freshness_token=freshness_token,
        )
    # CHUNKED pathway: fresh tail exceeds budget; split into chunks.
    return _refresh_chunked(
        summarizer=summarizer,
        item_id=item_id,
        finalized_count=finalized_count,
        finalized_topics=finalized_topics,
        trailing_topic=trailing_topic,
        fresh_from_turn=fresh_from_turn,
        last_finalized_end=last_finalized_end,
        total_turns=total_turns,
        fresh_budget_tokens=fresh_budget_tokens,
        llm_caller=llm_caller,
        profile=profile,
        freshness_token=freshness_token,
    )


def _refresh_single_call(
    *,
    summarizer,
    item_id: str,
    finalized_count: int,
    finalized_topics: list[SummaryNode],
    trailing_topic: SummaryNode | None,
    fresh_text: str,
    fresh_from_turn: int,
    last_finalized_end: int,
    total_turns: int,
    llm_caller: LLMCaller,
    profile: str | None,
    freshness_token: Any,
) -> SummaryNode | None:
    """Execute the single-call pathway. One LLM call covers the whole fresh tail."""
    from work_buddy.summarization.orchestrator import (
        build_error_provenance,
        build_provenance,
    )

    strategy = summarizer.strategy
    store = summarizer.store

    user_prompt = build_incremental_prompt(
        finalized=finalized_topics,
        trailing=trailing_topic,
        fresh_text=fresh_text,
        fresh_from_turn=fresh_from_turn,
        total_turns=total_turns,
    )

    result = llm_caller.call(
        system=strategy.system_prompt,
        user=user_prompt,
        output_schema=strategy.output_schema,
        profile=profile,
        max_tokens=2048,
        trace_id=f"summarization.{summarizer.name}.incremental.single",
    )

    if result.is_error():
        store.record_error(
            item_id,
            result.error or "llm error",
            build_error_provenance(summarizer, profile),
        )
        return None

    try:
        new_root = strategy.parse(result.structured_output, result.content)
    except Exception as exc:
        store.record_error(
            item_id,
            f"parse error: {exc}",
            build_provenance(summarizer, result, profile),
        )
        return None

    prov = build_provenance(summarizer, result, profile)
    activity_kind = new_root.extra.get("activity_kind", "unknown")
    v2_meta = _build_v2_meta(
        total_turns=total_turns,
        last_finalized_boundary=last_finalized_end,
        activity_kind=activity_kind,
        pathway="single-call",
        chunks_used=1,
        prov=prov,
    )

    store.apply_incremental(
        item_id, new_root, finalized_count, prov, freshness_token,
        v2_meta=v2_meta,
    )
    return store.load(item_id)


def _refresh_chunked(
    *,
    summarizer,
    item_id: str,
    finalized_count: int,
    finalized_topics: list[SummaryNode],
    trailing_topic: SummaryNode | None,
    fresh_from_turn: int,
    last_finalized_end: int,
    total_turns: int,
    fresh_budget_tokens: int,
    llm_caller: LLMCaller,
    profile: str | None,
    freshness_token: Any,
) -> SummaryNode | None:
    """Execute the chunked pathway. Multiple LLM calls; each chunk's output
    becomes the next chunk's `trailing_and_new_topics` context.

    Strategy: estimate average tokens per turn over the whole fresh tail,
    then pick a turn-count per chunk that fits `fresh_budget_tokens`. Loop
    through chunks calling `render_range` for each; accumulate topics; do
    a final apply_incremental with the full merged state.
    """
    from work_buddy.summarization.orchestrator import (
        build_error_provenance,
        build_provenance,
    )

    strategy = summarizer.strategy
    source = summarizer.source
    store = summarizer.store

    fresh_turn_count = total_turns - fresh_from_turn
    if fresh_turn_count <= 0:
        return store.load(item_id)

    # Estimate tokens per turn from a probe of the first ~10 turns.
    probe_to = min(fresh_from_turn + 10, total_turns)
    probe = source.render_range(item_id, fresh_from_turn, probe_to) or ""
    probe_turns = probe_to - fresh_from_turn
    tokens_per_turn = max(50, _estimate_tokens(probe) // max(probe_turns, 1))
    # Turns per chunk: floor of (budget / tokens-per-turn). Min 5 to avoid
    # pathological tiny chunks.
    turns_per_chunk = max(5, fresh_budget_tokens // tokens_per_turn)

    # Walk chunks. Accumulate topics across chunks. The first chunk uses the
    # real finalized + trailing as context; subsequent chunks use the
    # accumulator's topics-so-far (none of which are "finalized" mid-loop —
    # they're all mutable until the last chunk lands).
    accumulated_root: SummaryNode | None = None
    chunks_used = 0
    models_used: list[str] = []
    escalation_seen = False
    escalation_reasons: list[str] = []

    chunk_start = fresh_from_turn
    while chunk_start < total_turns:
        chunk_end = min(chunk_start + turns_per_chunk, total_turns)
        chunk_text = source.render_range(item_id, chunk_start, chunk_end)
        if not chunk_text:
            # Empty render — skip this chunk, advance.
            chunk_start = chunk_end
            continue

        # For the first chunk, use the real prior context.
        # For subsequent chunks, use the accumulator's children as the new
        # "trailing + finalized" mix — but since we treat all of them as
        # mutable mid-loop, feed them as "finalized + trailing" both:
        # - everything except the last child = "finalized" context
        # - last child = "trailing" context
        if accumulated_root is None:
            chunk_finalized = finalized_topics
            chunk_trailing = trailing_topic
        else:
            kids = list(accumulated_root.children)
            chunk_finalized = kids[:-1] if len(kids) > 1 else []
            chunk_trailing = kids[-1] if kids else None

        chunk_prompt = build_incremental_prompt(
            finalized=chunk_finalized,
            trailing=chunk_trailing,
            fresh_text=chunk_text,
            fresh_from_turn=chunk_start,
            total_turns=total_turns,
        )

        result = llm_caller.call(
            system=strategy.system_prompt,
            user=chunk_prompt,
            output_schema=strategy.output_schema,
            profile=profile,
            max_tokens=2048,
            trace_id=(
                f"summarization.{summarizer.name}.incremental.chunked"
                f"[{chunks_used}]"
            ),
        )
        chunks_used += 1

        if result.is_error():
            # Per PRD: on chunked failure mid-way, record an error and
            # bail. Don't half-update the store with partial state.
            store.record_error(
                item_id,
                f"chunked refresh failed at chunk {chunks_used}: {result.error}",
                build_error_provenance(summarizer, profile),
            )
            return None

        if result.model:
            models_used.append(result.model)

        try:
            chunk_root = strategy.parse(result.structured_output, result.content)
        except Exception as exc:
            store.record_error(
                item_id,
                f"chunked refresh parse error at chunk {chunks_used}: {exc}",
                build_provenance(summarizer, result, profile),
            )
            return None

        # Merge chunk_root.children into accumulator.
        if accumulated_root is None:
            # First chunk: use the chunk_root directly. Its tldr is the
            # session-tldr so far. Its children are the new topics.
            accumulated_root = SummaryNode(
                summary=chunk_root.summary,
                children=[
                    SummaryNode(
                        summary=c.summary,
                        source_ref=c.source_ref,
                        children=[],
                        extra=dict(c.extra),
                    )
                    for c in chunk_root.children
                ],
                extra=dict(chunk_root.extra),
            )
        else:
            # Subsequent chunk: the model emitted "trailing + new" — the
            # trailing IS the last child of the previous chunk's output.
            # Replace it (it may have been extended) and append new ones.
            kids = list(accumulated_root.children)
            new_kids = list(chunk_root.children)
            if kids and new_kids:
                # Drop the prior trailing (which the model just updated).
                kids = kids[:-1] + [
                    SummaryNode(
                        summary=c.summary, source_ref=c.source_ref,
                        children=[], extra=dict(c.extra),
                    ) for c in new_kids
                ]
            elif new_kids:
                kids = [
                    SummaryNode(
                        summary=c.summary, source_ref=c.source_ref,
                        children=[], extra=dict(c.extra),
                    ) for c in new_kids
                ]
            accumulated_root = SummaryNode(
                summary=chunk_root.summary,  # latest tldr wins
                children=kids,
                extra=dict(chunk_root.extra),
            )

        chunk_start = chunk_end

    if accumulated_root is None:
        # No chunks produced content.
        return store.load(item_id)

    # Final apply — finalized_count refers to the originally-finalized topics
    # (they're immutable across the whole loop).
    prov = build_provenance(summarizer, result, profile)  # noqa: F823 — last `result`
    activity_kind = accumulated_root.extra.get("activity_kind", "unknown")
    v2_meta = _build_v2_meta(
        total_turns=total_turns,
        last_finalized_boundary=last_finalized_end,
        activity_kind=activity_kind,
        pathway="chunked",
        chunks_used=chunks_used,
        prov=prov,
        models_actually_used=models_used,
    )

    store.apply_incremental(
        item_id, accumulated_root, finalized_count, prov, freshness_token,
        v2_meta=v2_meta,
    )
    return store.load(item_id)


def _build_v2_meta(
    *,
    total_turns: int,
    last_finalized_boundary: int,
    activity_kind: str,
    pathway: str,
    chunks_used: int,
    prov: Provenance,
    models_actually_used: list[str] | None = None,
    escalation_triggered: bool = False,
    escalation_reason: str | None = None,
) -> dict[str, Any]:
    """Assemble the v2 meta dict for `apply_incremental`."""
    if models_actually_used is None:
        models_actually_used = [prov.model] if prov.model else []
    return {
        "total_turns": total_turns,
        "last_finalized_boundary": last_finalized_boundary,
        "truncated": 0,
        "activity_kind": activity_kind,
        "pathway": pathway,
        "chunks_used": chunks_used,
        "model_chain": [prov.model] if prov.model else [],
        "models_actually_used": models_actually_used,
        "escalation_triggered": 1 if escalation_triggered else 0,
        "escalation_reason": escalation_reason,
    }


def _estimate_tokens(text: str) -> int:
    """Rough character-based token estimate. Good enough for budgeting."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _estimate_topic_context_tokens(
    finalized: list[SummaryNode],
    trailing: SummaryNode | None,
) -> int:
    """Estimate token count of the prior-topic context block."""
    # Each finalized topic: ~30 tokens (one line); trailing: ~60 tokens.
    n = 30 * len(finalized)
    if trailing is not None:
        n += 60
    # Add overhead for the section headers + system prompt.
    return n + 200


def _resolve_per_call_budget() -> int:
    """Resolve the per-call input token budget from config, falling back to
    the frontier_fast default. PRD §6 config block:

        conversation_observability:
          summaries:
            per_call_budget_tokens: 32000  # NEW
    """
    try:
        from work_buddy.config import load_config

        cfg = load_config()
        explicit = (
            (cfg.get("conversation_observability") or {})
            .get("summaries", {})
            .get("per_call_budget_tokens")
        )
        if isinstance(explicit, int) and explicit > 0:
            return explicit
    except Exception:
        pass
    return _DEFAULT_PER_CALL_BUDGET_TOKENS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_finalized_count(
    prior_topics: list[SummaryNode],
    total_turns: int,
    strategy: Any,
) -> int:
    """Walk prior topics and count how many are finalized.

    A topic is finalized if `strategy.is_finalized(topic.span_end, total_turns)`
    returns True. We assume topics are in chronological order, so finalized
    topics are contiguous from the front. Once we hit a non-finalized topic,
    stop counting — every later topic stays mutable too (the trailing region).
    """
    is_final = getattr(strategy, "is_finalized", None)
    if not callable(is_final):
        # Strategy lacks the heuristic — treat all as finalized except the
        # last one (the trailing topic).
        return max(0, len(prior_topics) - 1)

    count = 0
    for topic in prior_topics:
        span_end = topic.extra.get("span_end")
        if not isinstance(span_end, int):
            # Missing span_end → can't decide; stop here for safety.
            break
        if is_final(span_end, total_turns):
            count += 1
        else:
            break
    return count


def build_incremental_prompt(
    *,
    finalized: list[SummaryNode],
    trailing: SummaryNode | None,
    fresh_text: str,
    fresh_from_turn: int,
    total_turns: int,
) -> str:
    """Assemble the user message for an incremental refresh.

    Sections (in order):
      1. Finalized topic list — compact, one line per topic
      2. Trailing topic — slightly richer (full title + summary + span_range +
         keywords)
      3. Fresh raw turns
      4. Boundary metadata (fresh_from_turn, total_turns)

    The strategy's system prompt teaches the model what each section means.
    """
    parts: list[str] = []

    # 1. Finalized context
    if finalized:
        parts.append(_render_finalized_block(finalized))
    else:
        parts.append("## Existing finalized topics\n(none yet — this is the first summarization for this session)\n")

    # 2. Trailing topic
    if trailing is not None:
        parts.append(_render_trailing_block(trailing))
    else:
        parts.append("## Trailing topic\n(none — emit topics covering the new turns from scratch)\n")

    # 3. Fresh turns
    parts.append(
        f"## New raw turns (from turn {fresh_from_turn}; total session has "
        f"{total_turns} turns)\n\n{fresh_text}\n"
    )

    return "\n\n".join(parts)


def _render_finalized_block(finalized: list[SummaryNode]) -> str:
    """Compact one-line-per-topic rendering of finalized topics."""
    lines = ["## Existing finalized topics (IMMUTABLE context only — do not re-emit)"]
    for i, t in enumerate(finalized):
        title = t.extra.get("title", "(untitled)")
        s_start = t.extra.get("span_start", "?")
        s_end = t.extra.get("span_end", "?")
        kws = t.extra.get("keywords") or []
        kw_str = ", ".join(kws[:5])
        summary = (t.summary or "").strip()
        lines.append(
            f"- [{i}] {title} (turns {s_start}-{s_end}) — {summary}"
            + (f" [keywords: {kw_str}]" if kw_str else "")
        )
    return "\n".join(lines)


def _render_trailing_block(trailing: SummaryNode) -> str:
    """Slightly richer rendering of the trailing topic (it's mutable)."""
    title = trailing.extra.get("title", "(untitled)")
    s_start = trailing.extra.get("span_start", "?")
    s_end = trailing.extra.get("span_end", "?")
    kws = trailing.extra.get("keywords") or []
    summary = (trailing.summary or "").strip()
    return (
        "## Trailing topic (MUTABLE — you may extend its span_range, refine title/summary, or revise keywords)\n"
        f"- Title: {title}\n"
        f"- Span range: [{s_start}, {s_end}]\n"
        f"- Summary: {summary}\n"
        f"- Keywords: {', '.join(kws[:5])}\n"
    )
