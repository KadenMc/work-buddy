---
name: Conversation Observability
kind: system
description: 'Durable session-attributed activity DB for Claude Code: commits, file writes, GitHub PR activity, uncommitted work, observed-session metadata, and optional LLM topic summaries. Replaces ad-hoc per-call JSONL scans in sessions/inspector.py.'
tags:
- conversation_observability
- sessions
- commits
- writes
- prs
- summaries
- durable
- ir
aliases:
- session attribution db
- claude session observability
- conversation observability subsystem
- session activity ledger
parents:
- architecture
- architecture
dev_notes: |-
  ## Schema migrations

  `db._migrate_schema(conn)` runs on every connect and adds new columns via `ALTER TABLE`. The `commits_scanned_mtime`, `writes_scanned_mtime`, and `prs_scanned_mtime` columns were added after the initial schema; the helper makes upgrade-in-place transparent so existing DBs pick up new columns without intervention.

  ## The scan-mtime columns are NOT interchangeable

  If you find yourself reading `source_mtime` to decide whether to skip a commit-extraction scan, you're about to recreate the cross-refresher bug we already shipped a fix for. Each refresher reads/writes only its own column:
  - `refresh_observed_sessions` — owns `source_mtime`
  - `refresh_session_commits` — owns `commits_scanned_mtime`
  - `refresh_session_writes` — owns `writes_scanned_mtime`
  - `refresh_session_prs` — owns `prs_scanned_mtime`
  - `refresh_session_note_reads` — owns `note_reads_scanned_mtime`

  The `INSERT … ON CONFLICT DO UPDATE` statements preserve untouched columns. Never write `source_mtime` from a non-observed-sessions refresh path.

  ## PR detection is structural, and `created` dominates

  `_extract_prs_single_pass` keys on the `gh pr (create|merge|close|review)` *verb* in a Bash tool_use, then pulls the canonical PR URL from command-or-output. `created` is reliably captured (`gh pr create` prints the URL on stdout). `merged`/`closed`/`reviewed` are only captured when the invocation carries the PR *URL* — a merge done via the GitHub UI, or a bare-number `gh pr merge 92`, yields no row (no URL → can't satisfy the NOT-NULL `pr_url`/`repo`). In practice most merges are UI-driven, so the table skews heavily to `created`. This is a known tradeoff of structural detection, not a bug; the alternative (resolving repo from the Bash cwd for bare-number merges) was deliberately left out of scope.

  ## LLM summary versioning

  Four version stamps live as module constants in `summaries.py`:
  - `PROMPT_VERSION` — bump when SYSTEM_PROMPT changes meaningfully.
  - `SUMMARY_SCHEMA_VERSION` — bump when SUMMARY_OUTPUT_SCHEMA changes.
  - `SELECTION_VERSION` — bump when `_select_candidates` heuristics change.
  - `CACHE_VERSION` — bump for any change that invalidates outputs without fitting the above.

  Bumping any one invalidates every cached summary on the next refresh. The bump is the only sanctioned invalidation path; do NOT manually delete `session_summaries` rows.

  ## INFINITE_LIFECYCLE

  The artifact uses `work_buddy.artifacts.INFINITE_LIFECYCLE` rather than a sentinel TTL because intent matters: `grep INFINITE_LIFECYCLE` enumerates every durable artifact in one shot. The trigger advertises an empty capability set so the artifact's capability union truthfully reports no expiry policy.

  ## Compatibility wrappers in sessions/inspector.py

  `session_commits`, `build_session_map`, and `session_uncommitted` are thin wrappers over the new subsystem. They exist so GitSource, journal directions, and `/wb-session-identify` keep their import surface stable. `_extract_commits_single_pass`, `_recent_sessions`, `_extract_writes_from_jsonl` are pure parsers and remain the single source of truth.

  `_committed_files_per_session` is dead code post-migration; safe to remove once a follow-up confirms no external test depends on it.

  ## Failure semantics

  - Refresh functions per-row swallow file-stat / parser errors and continue to the next session. A single corrupt JSONL doesn't take down the batch.
  - `summarize_session` records `status='error'` on invalid LLM output but preserves the prior good `tldr` unless a future success overwrites it. The error column carries the exception message.
  - `currently_dirty` is best-effort. The `uncommitted_report` helper re-queries `git status --porcelain` during assembly so the surfaced status codes reflect current state; storing the codes at refresh time would lie about a mutable resource.
---

A SQLite-backed store of session-derived facts for Claude Code: commits, file writes, uncommitted-work attribution, observed-session metadata. The subsystem is a thin SQLite store plus refresh functions; raw JSONL parsing stays in `work_buddy/sessions/inspector.py` (single source of truth for the parser), and Git context source continues to own commit/status collection. Per-session LLM topic summaries are produced and stored by the summarization framework (`architecture/summarization-framework`); this subsystem owns the discovery side (observed-session enumeration) and the conv_obs-shaped read API that maps the framework's tree storage to a flat row.

## Why

`sessions/inspector.py` accumulated five orthogonal responsibilities: raw browsing, span mapping, commit extraction, write extraction, uncommitted attribution. The last three were a private cache for GitSource's session annotation, recomputed per call with a process-local mtime dict. Restarts wiped the cache; other consumers (journal, context bundle, dashboard) couldn't read it independently.

## Surface

Five tables in `<data_root>/conversation_observability/conversation_observability.db` (path overridable via `conversation_observability.db_path`):

- `observed_sessions` — per-JSONL ledger. Carries metadata (start/end, message_count, span_count, tool_names, and `first_user_message` — the captured opening user turn, used as an interpretive fallback when a session has no LLM summary) plus per-concern scan-mtime columns: `source_mtime` (metadata load), `commits_scanned_mtime` (commits refresh), `writes_scanned_mtime` (writes refresh), `prs_scanned_mtime` (PR refresh), `note_reads_scanned_mtime` (note-reads refresh). Each refresher owns its column so running them in any order doesn't conflate staleness state.
- `session_commits` — one row per git commit attributed to a session. Keyed by full SHA, indexed on `short_sha` for GitSource lookups. A read-only `query_commits_for_task(task_id)` scans `session_commits.message` for a task-id reference — *subject-line scope*: ids that appear only in a commit body or PR body are not captured — giving the structural basis for task→developer ("developed-by") attribution in `tasks/task_provenance`.
- `session_file_writes` — one row per (session, file_path). Carries the tool that wrote it, the latest write timestamp, an optional `committed_sha` cross-reference, and a `currently_dirty` snapshot (best-effort — git state is mutable, so consumers should treat this as not authoritative without a refresh).
- `session_prs` — one row per (session, PR, action) GitHub pull-request event, attributed by detecting `gh pr create|merge|close|review` Bash invocations in the JSONL (structural detection, not commit-message `Closes #NNN` parsing). Carries `pr_number`, `pr_url`, `repo`, `action`, and the invocation `ts`. `UNIQUE(session_id, pr_number, action, ts)` makes re-ingestion idempotent.
- `session_task_note_reads` — one row per (session, task, source) note-read event. `source` is one of three explicit "pulled this task's content" actions: `read_tool` (native `Read` of `tasks/notes/<uuid>.md`), `task_read_mcp`, `task_assign_mcp`. The collector reuses `provenance._scan_session_for_task` (the single shared detector — no drift) and skips orphan notes whose task is gone. This is the durable, O(1)-query basis for the *inverse* of developed-by — `tasks/task_provenance.sessions_who_read_task`, the Rung-3 "read it, did the work, never referenced the task id in a commit" surface behind the `note_reader` role in `/wb-task-completeness`. The weaker `saw_id` signal (id text without an explicit read) is deliberately not persisted.

Per-session tldr + ordered topic segments live in the summarization framework's `<data_root>/summarization/summarization.db` under namespace `conversation_session` (see `architecture/summarization-framework`). The legacy read API (`session_summary_get`, plus the deprecated alias `conversation_observability_summary_get`) is preserved via thin shims in `session_summary_row.py` that map between the framework's tree-shaped storage and the flat row shape consumers expect (dashboard `/api/chats/<id>/topics`, the `agent_session_summary` context collector, `/wb-session-identify`'s tldr triage).

Foreign-key cascades use `SqliteRowsStorage.post_delete_sql` rather than SQLite's FK enforcement. Deleting an `observed_sessions` row removes every child in the same transaction within conversation_observability.db; the corresponding `summary_items` + `summary_nodes` rows in summarization.db are dropped explicitly by the orphan-prune in `refresh_observed_sessions` as a best-effort follow-up.

## Refresh model

Two sidecar crons keep the DB fresh independent of caller demand:

- `conversation-observability-refresh.md` — every 5 minutes (offset from `ir-index-rebuild` by 2 minutes), `max_sessions=5`, `stale_only=true`. Runs all five non-LLM refreshers (observed-sessions, commits, writes, PRs, note-reads) and auto-enqueues changed sessions while Session Summaries is active. Automatic summaries are on by default; `features.conversation_summaries.wanted: false` opts out. Because the cron uses a 7-day window, older history enters the pipeline through `summarization_backfill`.
- `summarization-worker.md` — every 5 minutes (offset 3 minutes from observability-refresh). Drains cooldown-eligible active rows under the daily cost budget. It remains dormant without a plausible backend, rotates failures behind waiting work, and excludes dead letters while preserving them for status and revival. See `summarization/failure-handling`.

The `agent_session_summary` context source also triggers a stale-only refresh inline before rendering so bundle collections never read a cold DB. The `/ir/index` endpoint is deliberately NOT hooked — stale-only DB-backed scans are cheap enough that an independent cron is cleaner than embedding-service coupling.

## Lifecycle

The artifact uses `INFINITE_LIFECYCLE` (paired with the `NeverExpires` lifecycle trigger): every row is derived from JSONL session files that may have been deleted, so losing the DB means losing data that cannot be recovered. The sweep tick will see the artifact but never remove rows.

Summary invalidation lives on the framework side: bumping any of the four version constants on the active strategy/store (`prompt_version`, `schema_version`, `selection_version`, `cache_version`) invalidates cached summaries. The framework's composer re-bridges the strategy's versions into the store on every refresh, so a bump takes effect on the next sidecar fire.

## Consumers

- `work_buddy/collectors/agent_session_summary_collector.py` — context source rendering one block per project. Each session lists its tldr + a topic timeline with wall-clock time ranges (gated on the summaries feature being active), commits, uncommitted files, and PR activity, with the captured first user message as a fallback when there is no summary. It reads a **conversation-time** window (since/until, or `days`) rather than observation time, so a resumed old session is not resurfaced. Sibling to `chat` (raw inventory) and `session_activity` (current MCP session ledger).
- `work_buddy/collectors/git_collector.py` — receives `{short_sha: full_session_id}` via `inspector.build_session_map()`, which now reads from the DB instead of computing per-call.
- MCP capabilities: `conversation_observability_refresh`, `conversation_observability_uncommitted`, `conversation_observability_get`, `conversation_observability_list`, `session_summary_get`, `session_prs_get`, `summarization_worker_tick`. `conversation_observability_get` is a **composite**: a bare call returns the observed row, and opt-in `include_summary`/`include_commits`/`include_writes`/`include_prs`/`include_topics` flags join the rest for a one-call per-session picture. It stays separate from `session_summary_get` (the LLM summary alone) because their status/error track independently — transcript-parse health vs summary-generation health. The `conversation_observability_summarize` and `conversation_observability_summary_get` capabilities remain as deprecated aliases routed through legacy shims. The reverse session→tasks linkage is exposed via the tasks-domain `session_tasks_get` capability (reads `task_sessions`, enriched from the SQLite task store — bridge-independent); the tasks-domain `task_provenance` capability also reads `session_commits` (via `query_commits_for_task`) to derive a task's developed-by sessions. The tasks-domain `task_note_readers` capability reads `session_task_note_reads` (via `note_reads.query_reads_for_task`) for the inverse — sessions that *read* a task's note.
- Dashboard Chats view (`/api/chats` → `_load_observability_for_sessions`): aggregates per-session PR counts (authored/merged) and task-assignment counts into the chat cards' badge row, alongside the existing commit badge. In the chat *detail* view these stream into a **Topics | Git | Tasks** selector on the left rail: the **Git** panel lists commits + per-session **Pull requests**; the **Tasks** panel lists this session's task interactions with their roles (created / assigned / developed), backed by `/api/chats/<id>/tasks` → `provenance.build_session_task_roles` (richer than the card's assigned-only count). Per-stream colors: commits green, PRs purple, tasks orange. PR rows are enriched with title + current merge state (OPEN/MERGED/CLOSED) via a best-effort, TTL-cached `gh pr list` per repo (`_load_pr_meta_for_repos`) — the JSONL only yields number/url/action, so title/merge-state come from GitHub at the display layer; offline/un-authenticated `gh` simply omits them.
- Journal directions (`journal/update-directions`) require `agent_session_summary.md` alongside `git_summary.md` / `chat_summary.md` so multi-hour sessions without commits get logged as exploration rather than silently dropped.
