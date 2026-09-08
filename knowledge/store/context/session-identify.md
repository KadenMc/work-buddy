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
- allow-transient-labels
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

### 2. Rank first, then drill the winner — via `summary_search`

**Rank-first.** Run `summary_search` with `drill=False` (the default) for the *locating* pass. It ranks the cheap compressed layer (per-session TLDRs + topic titles/summaries/keywords) and returns only `stage1_hits` + `candidate_items` (~10 KB) — enough to pick the session. Do **not** drill in this pass: the drill stage inlines raw transcript and, over verbose sessions, can be 100×+ larger and blow the result budget (oversized results are capped to a marker you'd then have to retrieve anyway).

```
mcp__work-buddy__wb_run("summary_search", {
  "query": "<topic phrase>",
  "scope": "conversation_session",
  "top_k": 12
})
```

For multi-keyword handoffs, fire 2-3 of these `drill=False` calls **in parallel** with distinct phrasings (specific identifier, conceptual phrase, file-path needle). Aggregate `candidate_items` across calls — this is cheap because nothing is drilled.

Returned shape (rank-first):
- **`stage1_hits`** — per-node summary hits. Each entry has `item_id` (the session id), `level` (0 = root tldr; 1 = topic), `title`, `summary` (preview), `score`, `source_ref`, `drill_node_id`, `generated_at`, `model`.
- **`candidate_items`** — deduplicated per-session aggregates, ranked by `best_score`, each with `namespace`, `item_id`, `best_score`, `n_hits`, `top_titles`, `drill_node_id`.
- **`drilled`** — empty (`{}`) when `drill=False`.
- **`error`** present only when stage 1 failed (e.g. embedding service down).

**Then drill the winner.** Once the ranking points at ONE (or two) candidate(s), pull their raw spans — either re-run `summary_search` with `drill=True, drill_top_k=1` for just that candidate, or hand the hit's `drill_node_id` to `drill_tree`/`session_expand` (the find→walk→read flow). Drilling a single chosen winner keeps the raw payload bounded; drilling the whole top-N up front is what causes oversized returns.

```
mcp__work-buddy__wb_run("summary_search", {
  "query": "<topic phrase>",
  "scope": "conversation_session",
  "drill": true,
  "drill_top_k": 1,
  "drill_per_item_top_k": 5
})
```

`find(source="summary", ...)` is the same funnel under the universal verb name — both default `drill=False`; use whichever reads naturally.

### 3. Fallback: raw-span sweep when summaries miss

Choose the raw-span search method based on how `summary_search` failed.

If it returned an empty `candidate_items` list or weak top scores without a service error, use the default hybrid method:

```
mcp__work-buddy__wb_run("context_search", {
  "query": "<topic phrase>",
  "source": "conversation",
  "top_k": 15,
  "recency": false
})
```

`source="conversation"` is mandatory because omitting it lets tabs and docs dilute the ranking. Use `recency=false` for older targets because the default recency bias can bury multi-month-old hits.

If `summary_search` returned an error, or embedding-backed search is timing out or unavailable, do not use the default `context_search` method. It calls the same embedding-service search path. Use two or three separate in-process substring probes instead:

```
mcp__work-buddy__wb_run("context_search", {
  "query": "<short literal term>",
  "source": "conversation",
  "method": "substring",
  "top_k": 15,
  "recency": false
})
```

`method="substring"` matches the entire query string literally. It does not tokenize or interpret a natural-language question. Probe with one distinctive identifier, file-path fragment, error string, or short exact phrase per call, then combine the returned session candidates. A long conceptual query will usually match nothing even when the target session is present.

For either raw-span path:

- Returned hits are tagged `[<cwd-name>]` and a short session-ID prefix. The same conversation often appears under multiple short IDs because resumed-session forks have distinct JSONL files. Treat clustered IDs as a single conversation.
- After the default hybrid sweep, drill into clustered candidates with `session_search(session_id=..., query=...)`.
- During an embedding-service outage, keep the drill local too: call `session_search(session_id=..., query="<short literal term>", method="substring")`. Do not switch back to the default hybrid method until the service path recovers.

Raw-span search is the right path for very recent sessions, sessions whose summarization has `status='error'`, and exact strings that compressed summaries may omit. The substring method is also the outage-safe path because it reads the local IR store without calling the embedding service.

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

- The rank-first `summary_search` (`drill=False`) is the load-bearing step. Spend phrasing effort there; drill only the winner, never the whole top-N.
- Resumed-session forks share content but have distinct UUIDs. Pick the longest / latest fork as the canonical one to cite.
- The 8-char session prefix is canonical for display; the full UUID is canonical for storage. Always show the full UUID once in the report.
- Do not diagnose a search outage with `ir_index(action='status')` in this flow. It calls the same service with the same client timeout, so congestion can make a healthy service look unreachable. Use the explicit substring fallback above.
- The drill stage is per-item bounded; if a single session has many topic hits, increase `drill_per_item_top_k` rather than `drill_top_k`.
- Cached summaries (`conversation_observability_summary_get`) are not authoritative for content matching — they exist as a read shortcut, not as a replacement for drill confirmation.
