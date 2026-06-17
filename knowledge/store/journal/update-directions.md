---
name: Journal Update Directions
kind: directions
description: How to detect activity and append journal Log entries — format, synthesis rules, approval flow
summary: 'Format: ''* <TIME> - #projects/<slug> — <description>. #wb/journal/log''. One entry per distinct activity, minute-level timestamps. REQUIRED before writing: dedupe against existing Log entries (journal_state per target day) AND get explicit user approval; journal_write is consent-gated by the Obsidian bridge. Near-real-time event log, not summaries.'
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
  The journal-update workflow's `collect` step runs `work_buddy.journal.collect_scoped_context`, which calls `collect_bundle`. The `git` source is multi-repo since the Phase-A migration — walks every repo at depth 1 under `cfg['repos_root']`, buckets commits under `#### <project>` headings in the rendered `git_summary.md`. Per-commit project attribution layers on top when t-3d733f68 (`repo_paths` on projects) lands.

  Tests: `tests/unit/test_git_source_multirepo.py` covers discovery, single-repo scoping, `dirty_only`, session annotation, `is_stale` HEAD detection, legacy cache-shape fallback, and drill-down cross-repo lookup.
---

Start via mcp__work-buddy__wb_run("update-journal"). Advance with wb_advance after each step.

Target date: Defaults to today. If past midnight (00:00-04:00) and no date specified, ask whether they mean today or yesterday.

## Synthesis instructions

The Log is a near-real-time event log, not a high-level summary. Each entry anchors a thing that happened at a specific time.

Format: * <TIME> - #projects/<slug> — <description>. #wb/journal/log
- Bullet is '* ' (asterisk space), NOT '- '
- Single line per entry -- no sub-bullets, no nesting
- Terse, specific -- match the user's journal voice
- Each entry MUST be prefixed with `#projects/<slug>` where `<slug>` is the project the activity belongs to. The slug must come from `project_list` (canonical registry slug). If activity cannot be attributed to a registered project, prefix `#projects/unknown` and flag for the user during approval.

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
2. `chat_summary.md` — each session header is tagged `[<repo-or-project>]` (e.g. `[electricrag]`, `[agentic-experiments]`, `[work-buddy]`). Multi-hour sessions in non-work-buddy projects are a strong signal of activity even when no commits landed. Cross-reference with the git summary; if a session is long and the matching repo has no commits, the work is real but unfinished — still log it as exploration.
3. `claude_session_summary.md` — **interpreted** Claude Code session activity from the conversation_observability DB. One block per project, listing each session with its commits and uncommitted files. The interpretation makes it easier to spot multi-hour sessions whose work never reached a commit — those are usually exploration that should still be logged. If a session appears here with uncommitted files but no row in `git_summary.md`, that is real unfinished work, not a gap.
4. `obsidian_summary.md` — Running Notes additions, task completions, journal sign-in.
5. `session_activity_summary.md` — MCP gateway events for THIS session only; useful for sanity-checking your own actions but not project-wide activity.

If you produce a draft Log that mentions only one project across an active multi-hour day, treat that as a smell and re-check sources 1, 2, and 3 before presenting it to the user.

### Timestamp semantics across the bundle

- **All bundle timestamps are local wall-clock time** (the configured `timezone` / `USER_TZ`), with no "UTC" label. Times in `git_summary.md`, `chat_summary.md`, `claude_session_summary.md`, and `obsidian_summary.md` sit on one local timeline, so they can be compared and ordered directly — and they line up with the journal's own local Sign-In, office-arrival, and Log times. Place events at the local time shown.
- **Chat and SpecStory sessions are windowed by real conversation time**, not file mtime. A Claude Code session's window membership comes from its message-derived start/end; a SpecStory session's from its filename stamp. A session resumed today but whose conversation happened days ago will NOT appear in today's window — and `chat_summary.md` labels every session with its real start/end, so a session header's date is the date the conversation actually happened.

## Approval + dedup — REQUIRED before any write

Two gates stand between a draft and `journal_write`. Both are mandatory every run, including backfills:

1. **Dedupe against what is already there.** Before presenting, call `journal_state` for **each target day** and read its existing Log entries. Drop any draft entry whose activity is already logged (same activity/time). When backfilling a multi-day window, whole days are often already covered — skip those days entirely. The write must be idempotent and safe to re-run.
2. **Get explicit user approval.** Present the deduped entries (grouped by day; name the days you are skipping as already-covered) and wait for an explicit go-ahead. The user may edit, reword, add, or remove. Do NOT call `journal_write` until they approve — a verbal "looks good / proceed" counts; silence does not.

Note: `journal_write` is itself **consent-gated by the Obsidian bridge** — each call raises a surface consent prompt (Obsidian / dashboard / Telegram), separate from the in-chat approval above. For a multi-day backfill, tell the user they can approve once with "Allow always (this session)" so the writes do not stack one prompt per day.

## Calling journal_write

The `entries` parameter is a JSON string containing a list of `[time, description]` tuples — NOT pre-formatted vault lines. The function handles vault formatting (bullets, tags) internally. Each `description` should INCLUDE the `#projects/<slug>` prefix.

Example:
```
mcp__work-buddy__wb_run("journal_write", {
  "target": "2026-04-15",
  "entries": "[[\"6:08 PM\", \"#projects/work-buddy — Fixed consent nesting bug.\"], [\"7:52 PM\", \"#projects/electricrag — Added grader run for 32 ECG batch.\"]]"
})
```

Do NOT pass pre-formatted strings like `* 6:08 PM - Description. #wb/journal/log` — pass raw `[time, description]` pairs and let the function format them.

## Don'ts
- Don't fabricate activity not in the signals
- Don't repeat information already in existing Log entries
- Don't include raw git hashes or file paths unless they add meaning
- Don't add entries to sections other than Log
- Don't synthesize from a single source — always cross-reference git + chat + claude_session_summary + obsidian
- Don't omit the `#projects/<slug>` prefix — if you don't know the slug, run `project_list` and resolve it

--- context from: obsidian/bridge ---
