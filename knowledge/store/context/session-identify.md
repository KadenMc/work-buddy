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

### 2. Cross-session search — broad sweep, run in parallel

Fire 3-5 `context_search` calls **in parallel** in one tool-use block. One per high-signal keyword cluster. Use:

- `source="conversation"` (mandatory — otherwise you'll mix in tabs and docs)
- `top_k=15` (surface enough to see clustering)
- `recency=false` for older targets (default `true` biases toward this week, which buries multi-month-old hits)
- Default `method` (keyword+semantic RRF-fused) is usually right; only switch to `method="substring"` for exact-match needles

Returned hits are tagged `[<cwd-name>]` and a short session-ID prefix. The same conversation often appears under multiple short IDs — these are resumed-session forks of the same JSONL. Treat clustered IDs as a single conversation.

### 3. Score and sanity-check candidates

A strong match has:
- Multiple tier-1 keywords co-occurring in the same session.
- Hits in **both** keyword and semantic columns.
- Tight time clustering inside the session's date range.
- Snippets that show *the decision being made*, not just downstream usage of the artifact.

De-prioritise:
- Sessions the user explicitly listed as already-known false positives.
- Today's conversation (your own) unless explicitly in scope.
- Sessions where the keyword appears only in tool output / file paths but not in dialogue.

**Optional sanity-check via cached summaries.** When the LLM-summary feature is enabled (`conversation_observability.summaries.enabled: true`), look up the cached tldr for each candidate session via `conversation_observability_summary_get(session_id=<full UUID>)` before drilling. The tldr is one sentence; if it doesn't match what you're looking for, demote that candidate without spending tokens on `session_search`. Missing summaries return `None`, not an error — safe to attempt even when summaries are disabled.

### 4. Drill into top candidates — parallel again

For each plausible session (typically 2-3), run `session_search` in parallel with the most discriminating tier-1 query. This returns:

- Session metadata (message count, duration, start/end timestamps, tools used).
- Hit spans with `turn_range` and full message text — enough to read the actual dialogue without expanding further.

If you need more context around a single message, use `session_expand` with the message_index. If you need to browse linearly, use `session_get` with offset/limit.

### 5. Report

Return:

- **Primary match** — full session UUID, cwd tag, start/end timestamps, message count. Include the cached tldr if available.
- **Secondary / earlier seed** — if the topic was first articulated in a different (usually older) session, include it too.
- **Key turns** — a small table of (turn index, speaker, what happened).
- **Memory-worthy facts** — bulleted list of decisions, rules, or preferences worth saving.
- **Open offer** — ask whether to write the memory-worthy facts as auto-memory entries.

## Tips

- The first cross-session sweep is the load-bearing step. Spend keyword effort there; don't over-drill into a weak candidate.
- Resumed-session forks share content but have distinct UUIDs. Pick the longest / latest fork as the canonical one to cite.
- The 8-char session prefix is canonical for display; the full UUID is canonical for storage. Always show the full UUID once in the report.
- If `context_search` returns nothing useful, re-check `recency` — flipping it to `false` often surfaces month-old hits the default ordering buried.
- The `source="conversation"` filter is mandatory; without it you also get document and tab hits which dilute the ranking.
- Cached summaries (`conversation_observability_summary_get`) are not authoritative for content matching — they exist to triage candidates cheaply, not replace the drill step.
