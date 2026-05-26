---
name: Session Identify
kind: directions
description: Locate a prior Claude Code conversation by topic, then drill into it for the specific turns that matter
trigger: user wants to find a past conversation they vaguely remember (topic, decision, agent persona, time window) and re-surface its contents for re-indexing or re-reading
command: wb-session-identify
capabilities:
- context_search
- session_search
- session_expand
- session_get
- session_locate
tags:
- context
- session
- search
- identify
- conversation
- recall
- cross-session
aliases:
- find that conversation
- which session was
- recall prior agent
- surface old chat
- dig up a session
- conversation about
parents:
- context
dev_notes: The IR index must be built (`ir_index`) for `context_search` to work — sessions added since the last index build won't be found. If a search comes back empty for a known recent conversation, suspect index staleness before suspecting bad keywords.
---

Find and re-surface a prior Claude Code conversation. The user recalls *that* it happened (topic, decision, an agent persona, a rough time window) but not *where* — your job is to locate it, drill in, and return ranked candidates plus the load-bearing turns.

## When this fits

- "Find the session where we worked out X."
- "What did the previous <project> agent decide about Y?"
- "Surface the conversation that motivated commit Z / file W."
- "Which chat had me asking about <thing>?"

Not a fit (use a different tool):
- *Within* the current conversation → just scroll back / use your context.
- Recently active resumable sessions → `remote_session_list`.
- Specific commit's authoring session → `session_commits` / `context_git annotate=true`.

## Procedure

### 1. Get the user's hand-off

Ask for, or extract from their message:
- **Topic** — what was decided, debated, or worked through.
- **Time window** — exact dates if known, otherwise a range.
- **Tier-1 keywords** — phrases unique to that conversation (file paths, identifiers, distinctive jargon, error strings, code symbols). The more specific, the fewer false positives.
- **Tier-2 keywords** — supporting context (subsystem names, collaborators, project names).
- **Known false positives** — conversations the user already knows about and wants excluded.

If the user already wrote a structured handoff, use it verbatim — don't re-elicit.

### 2. Coarse-to-fine via `summary_search` (a.k.a. `find` with `source="summary", drill=true`)

Run `summary_search` with the user's strongest topic phrase. This is the **load-bearing step** — it ranks the cheap compressed layer (per-session TLDRs + topic titles/summaries/keywords) and then drills into the top items' raw spans in one call:

```
mcp__work-buddy__wb_run("summary_search", {
  "query": "<topic phrase>",
  "scope": "conversation_session",
  "top_k": 12,
  "drill": true,
  "drill_top_k": 4,
  "drill_per_item_top_k": 5
})
```

The equivalent under the universal verb name:

```
mcp__work-buddy__wb_run("find", {
  "query": "<topic phrase>",
  "source": "summary",
  "scope": "conversation_session",
  "drill": true,
  "top_k": 12,
  "drill_top_k": 4,
  "drill_per_item_top_k": 5
})
```

Both ops return the same funnel-shape dict — use whichever name reads naturally.

Returned shape (every key always present):
- **`stage1_hits`** — per-node summary hits. Each entry has `item_id` (the session id), `level` (0 = root tldr; 1 = topic), `title`, `summary` (preview), `score`, `source_ref` (the span pointer for level-1 topics), `generated_at`, `model`.
- **`candidate_items`** — deduplicated per-session aggregates, ranked by `best_score`. Each has `namespace`, `item_id`, `best_score`, `n_hits`, `top_titles`.
- **`drilled`** — dict mapping each top-`drill_top_k` `item_id` to its `session_search` result (`hits` with `turn_range` + message text).
- **`error`** present only when stage 1 failed (e.g. embedding service down).

For multi-keyword handoffs, fire 2-3 `summary_search` calls **in parallel** with distinct phrasings (specific identifier, conceptual phrase, file-path needle). Aggregate `candidate_items` across calls before drilling further.

### 3. Fallback: raw-span sweep when summaries miss

If `summary_search` returned an `error`, an empty `candidate_items`, or weak top scores (single-digit hits across queries, no token co-occurrence), fall back to the raw-span sweep:

- Use `context_search(query=..., source="conversation", top_k=15, recency=false)` to scan raw spans across all sessions. `source="conversation"` is mandatory — otherwise tabs and docs dilute the ranking. `recency=false` for older targets (default `true` biases toward this week and buries multi-month-old hits).
- Returned hits are tagged `[<cwd-name>]` and a short session-ID prefix. The same conversation often appears under multiple short IDs — resumed-session forks of the same JSONL. Treat clustered IDs as a single conversation.
- Drill into clustered candidates via `session_search(session_id=..., query=...)`.

The fallback is the right path for:
- Very recent sessions (the summarization cron runs every 2 hours; today's session may not be summarized yet).
- Sessions whose summarization landed with `status='error'` (the framework records the failure without overwriting prior good summaries; the row exists but has no nodes to search).
- Queries needing exact-string match (use `method="substring"`).

### 4. Score and sanity-check candidates

A strong match has:
- Multiple tier-1 keywords co-occurring in the same session.
- Hits in **both** the summary layer (stage 1) AND the raw-span drill (stage 2 / `drilled`).
- Tight time clustering inside the session's date range.
- Snippets that show *the decision being made*, not just downstream usage of the artifact.

De-prioritise:
- Sessions the user explicitly listed as already-known false positives.
- Today's conversation (your own) unless explicitly in scope.
- Summary-only hits without drill confirmation: the LLM that wrote the summary may have used adjacent vocabulary; drill before promoting.

### 5. Re-rank or expand if needed

- `session_expand(session_id=..., message_index=N, span=5)` for full context around a turn surfaced in `drilled[item_id]['hits']`.
- `session_get(session_id=..., offset=..., limit=...)` for linear browsing.
- `session_locate(session_id, span_index)` to jump from a stage-1 hit's `source_ref.span_start` to its corresponding message index.
- `conversation_observability_summary_get(session_id)` for the full session tldr + every topic at once — cheaper than re-running `summary_search` when you just want to read the recap.

### 6. Report

Return:

- **Primary match** — full session UUID, cwd tag, start/end timestamps, message count, tldr from `summary_search` stage 1 (or `conversation_observability_summary_get`).
- **Secondary / earlier seed** — if the topic was first articulated in a different (usually older) session, include it too.
- **Key turns** — a small table of (turn index, speaker, what happened) drawn from `drilled[item_id]['hits']`.
- **Memory-worthy facts** — bulleted list of decisions, rules, or preferences worth saving.
- **Open offer** — ask whether to write the memory-worthy facts as auto-memory entries.

## Tips

- `summary_search` is the load-bearing step. Spend phrasing effort there; don't over-drill into a weak candidate.
- Resumed-session forks share content but have distinct UUIDs. Pick the longest / latest fork as the canonical one to cite.
- The 8-char session prefix is canonical for display; the full UUID is canonical for storage. Always show the full UUID once in the report.
- If `summary_search` returns nothing, check whether the IR `summary` index is built via `ir_index(action='status', source='summary')`. If `dense_eligible_docs` is 0, the embedding service is down; fall back to `context_search` (raw-span sweep).
- The drill stage is per-item bounded; if a single session has many topic hits, increase `drill_per_item_top_k` rather than `drill_top_k`.
- Cached summaries (`conversation_observability_summary_get`) are not authoritative for content matching — they exist as a read shortcut, not as a replacement for drill confirmation.
