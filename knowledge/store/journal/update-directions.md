---
name: Journal Update Directions
kind: directions
description: How to detect activity and append journal Log entries — format, synthesis rules, approval flow
summary: 'Create one native record per distinct activity with a minute-level timestamp. Prefix the description with #projects/<slug> only after matching a registered project. Before writing, dedupe against journal_state for every target day and obtain explicit user approval. journal_write commits each accepted record to Sources before Journal SQLite.'
trigger: user wants to update their journal with recent activity
command: wb-journal-update
workflow: daily-journal/update-journal
capabilities:
- journal/journal_state
- journal/journal_write
- context/context_bundle
tags:
- journal
- update
- log
- synthesis
- directions
aliases:
- update journal
- journal log entries
- append journal
- detect activity journal
parents:
- journal
dev_notes: |-
  The journal-update workflow's `collect` step runs `work_buddy.journal.collect_scoped_context`, which calls `collect_bundle`. The `git` source walks every repo at depth 1 under `cfg['repos_root']` and buckets commits under `#### <project>` headings in the rendered `git_summary.md`. Per-commit attribution uses registered project `repo_paths` when present.

  Tests: `tests/unit/test_git_source_multirepo.py` covers discovery, single-repo scoping, `dirty_only`, session annotation, `is_stale` HEAD detection, legacy cache-shape fallback, and drill-down cross-repo lookup.
---

Start via mcp__work-buddy__wb_run("update-journal"). Advance with wb_advance after each step.

Target date: Defaults to the backend-resolved logical Journal day. Do not ask a civil-midnight today/yesterday question; the configured timezone and day-boundary policy is authoritative. Ask only when the user supplies an ambiguous explicit date reference that the policy cannot resolve.

## Synthesis instructions

The Log is a near-real-time event log, not a high-level summary. Each entry anchors a thing that happened at a specific time.

Capability tuple (attributed):     `["<TIME>", "#projects/<slug> — <description>."]`
Capability tuple (unattributable): `["<TIME>", "<description>."]`
- One tuple per record; do not pre-render a Markdown bullet or hidden marker.
- Single line per entry -- no sub-bullets, no nesting.
- Terse, specific -- match the user's journal voice
- Attribution is mandatory effort, but the tag is conditional output. For EVERY entry you MUST attempt attribution: run `project_list`, check aliases, and actively decide which registered project the activity belongs to. When a project matches, prefix `#projects/<slug>` using the canonical registry slug from `project_list` (NOT an alias). Only after a genuine, attempted-and-failed match may an entry be left untagged — omission is the conclusion of attribution work, never a shortcut to skip it. Never use `#projects/unknown` or any placeholder slug. Every untagged entry MUST be surfaced to the user at approval (see "Approval + dedup" below).

Rules:
1. One entry per distinct activity -- a commit, a completed task, a bug fix. If 6 things happened, produce ~4-6 entries.
2. Timestamps from the data -- minute-level. 1:11 PM not 1:00 PM. Never round.
3. Describe the work, not the tools -- 'Fixed vault path normalization' not '1 commit to work-buddy'
4. Aggregate only when truly redundant -- 3 commits fixing same bug = one entry. But bug fix + feature + docs = three entries.
5. Commits and task completions are primary evidence.
6. Keep entries short -- one line, one topic.
7. Cover the full window -- if last entry timestamp is well before collect_until, something was missed.

## Sources you MUST consult before synthesizing

The `collect` step writes a bundle directory. Read EVERY relevant file before drafting entries — a single source is never sufficient. Gaps here are how entire days of paper-lane work get silently dropped.

1. `git_summary.md` — multi-repo scan across ALL repos under `repos_root`. Each commit is bucketed under `#### <project>` subheadings. This is the primary git evidence; trust it for commit-level activity in every registered project.
2. `chat_summary.md` — in a bundle this carries only **SpecStory + CLI history** (the agent-harness conversations live in `agent_session_summary.md`, source 3). It may be **absent entirely** when nothing falls in the window — that is expected, not a gap.
3. `agent_session_summary.md` — the **interpreted** surface, and your primary conversation evidence. Agent-session activity (Claude Code, Codex, …) from the conversation_observability DB, one block per project. Each session lists its **tldr**, a **topic timeline with wall-clock time ranges**, commits, uncommitted files, and PR activity. When a session has no summary yet (opted out, errored, or not yet generated) a `first message:` line stands in. A session with a multi-hour span and no commits is exploration you should still log; if it appears here with uncommitted files but no row in `git_summary.md`, that is real unfinished work, not a gap.
4. `session_activity_summary.md` — MCP gateway events for THIS session only; useful for sanity-checking your own actions but not project-wide activity.
5. `journal_state` — native records and typed fields already present for the target day. This is the authority for dedupe; do not expect an Obsidian bundle file.

Every bundle file opens with a `*Window: …*` banner stating the exact window it covers; the sources are scoped to the journal's activity window, so what you see already belongs to the target day.

**Drill when a session is opaque.** If `agent_session_summary.md` shows a long session with no commits and its topic titles don't tell you what happened, call `conversation_observability_get(session_id, include_topics=true, include_writes=true)` for the full per-session picture before logging (or leaving out) that exploration. `summary_search` finds sessions across the store by topic.

If you produce a draft Log that mentions only one project across an active multi-hour day, treat that as a smell and re-check sources 1, 2, and 3 before presenting it to the user.

### Timestamp semantics across the bundle

- **All bundle timestamps are local wall-clock time** (the configured `timezone` / `USER_TZ`), with no "UTC" label. Times in `git_summary.md`, `chat_summary.md`, and `agent_session_summary.md` sit on one local timeline and align with the Journal logical-day window. Place events at the local time shown.
- **Chat and SpecStory sessions are windowed by real conversation time**, not file mtime. A Claude Code session's window membership comes from its message-derived start/end; a SpecStory session's from its filename stamp. A session resumed today but whose conversation happened days ago will NOT appear in today's window — and `chat_summary.md` labels every session with its real start/end, so a session header's date is the date the conversation actually happened.

## Approval + dedup — REQUIRED before any write

Two gates stand between a draft and `journal_write`. Both are mandatory every run, including backfills:

1. **Dedupe against what is already there.** Before presenting, call `journal_state` for **each target day** and read its existing Log entries. Drop any draft entry whose activity is already logged (same activity/time). When backfilling a multi-day window, whole days are often already covered — skip those days entirely. The write must be idempotent and safe to re-run.
2. **Get explicit user approval.** Present the deduped entries (grouped by day; name the days you are skipping as already-covered) and wait for an explicit go-ahead. The user may edit, reword, add, or remove. Do NOT call `journal_write` until they approve — a verbal "looks good / proceed" counts; silence does not. When presenting, you MUST also call out every entry you left **unattributed** ("N entries I couldn't attribute to a project: …") so the omissions are visible and auditable — never omit a project tag silently.

`journal_write` remains consent-gated by Work Buddy's normal capability policy, separate from the in-chat approval above. For a multi-day backfill, the user can approve the repeated operation for the current session. No Obsidian prompt or file write occurs.

## Calling journal_write

The `entries` parameter is a JSON string containing a list of `[time, description]` tuples. Each complete rendered `time - description` record is retained as an agent-output Source and then written to the active profile's record module with `ai`/`unreviewed` provenance. Each `description` should include the `#projects/<slug>` prefix when attributed; a genuinely unmatched entry has no project prefix.

Example:
```
mcp__work-buddy__wb_run("journal_write", {
  "target": "YYYY-MM-DD",
  "entries": "[[\"6:08 PM\", \"#projects/work-buddy — Fixed consent nesting bug.\"], [\"7:52 PM\", \"#projects/ecg-inquiry — Added grader run for 32 ECG batch.\"]]"
})
```

Do not pass pre-formatted Markdown lines. Pass `[time, description]` pairs and reuse a stable `client_mutation_id` if a response is lost.

## Don'ts
- Don't fabricate activity not in the signals
- Don't repeat information already in existing Log entries
- Don't include raw git hashes or file paths unless they add meaning
- Don't bypass the active profile's resolved record module
- Don't synthesize from a single source — always cross-reference git + chat + agent-session evidence + native Journal state
- Don't omit a project tag out of laziness — you MUST run `project_list` and attempt resolution first; omit only after a genuine failed match, and always flag the omission at approval
- Don't invent `#projects/unknown` or any placeholder slug for non-project activity — a genuine life event simply carries no project tag
