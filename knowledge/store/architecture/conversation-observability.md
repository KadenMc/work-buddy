---
name: Conversation Observability
kind: system
description: 'Durable session-attributed activity DB for Claude Code: commits, file writes, uncommitted work, observed-session metadata, and optional LLM topic summaries. Replaces ad-hoc per-call JSONL scans in sessions/inspector.py.'
tags:
- conversation_observability
- sessions
- commits
- writes
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

  `db._migrate_schema(conn)` runs on every connect and adds new columns via `ALTER TABLE`. The `commits_scanned_mtime` and `writes_scanned_mtime` columns were added after the initial schema; the helper makes upgrade-in-place transparent so existing DBs pick up new columns without intervention.

  ## The three scan-mtime columns are NOT interchangeable

  If you find yourself reading `source_mtime` to decide whether to skip a commit-extraction scan, you're about to recreate the cross-refresher bug we already shipped a fix for. Each refresher reads/writes only its own column:
  - `refresh_observed_sessions` — owns `source_mtime`
  - `refresh_session_commits` — owns `commits_scanned_mtime`
  - `refresh_session_writes` — owns `writes_scanned_mtime`

  The `INSERT … ON CONFLICT DO UPDATE` statements preserve untouched columns. Never write `source_mtime` from a non-observed-sessions refresh path.

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

Centralizes session-derived facts that previously lived ad hoc in `work_buddy/sessions/inspector.py`. The subsystem is a thin SQLite store plus refresh functions; raw JSONL parsing stays in the inspector (single source of truth for the parser), and Git context source continues to own commit/status collection.

## Why

`sessions/inspector.py` accumulated five orthogonal responsibilities: raw browsing, span mapping, commit extraction, write extraction, uncommitted attribution. The last three were a private cache for GitSource's session annotation, recomputed per call with a process-local mtime dict. Restarts wiped the cache; other consumers (journal, context bundle, dashboard) couldn't read it independently.

## Surface

Five tables in `<data_root>/conversation_observability/conversation_observability.db` (path overridable via `conversation_observability.db_path`):

- `observed_sessions` — per-JSONL ledger. Carries metadata (start/end, message_count, span_count, tool_names) plus three per-concern scan-mtime columns: `source_mtime` (metadata load), `commits_scanned_mtime` (commits refresh), `writes_scanned_mtime` (writes refresh). Each refresher owns its column so running them in any order doesn't conflate staleness state.
- `session_commits` — one row per git commit attributed to a session. Keyed by full SHA, indexed on `short_sha` for GitSource lookups.
- `session_file_writes` — one row per (session, file_path). Carries the tool that wrote it, the latest write timestamp, an optional `committed_sha` cross-reference, and a `currently_dirty` snapshot (best-effort — git state is mutable, so consumers should treat this as not authoritative without a refresh).
- `topic_summaries` — LLM-generated topic chunks for a summarized session, each with title, summary, span range, turn range, and keywords.
- `session_summaries` — per-session tldr + full provenance (model, profile, backend, prompt_version, summary_schema_version, selection_version, cache_version). Status flag + error column track failures.

Foreign-key cascades use `SqliteRowsStorage.post_delete_sql` rather than SQLite's FK enforcement. Deleting an `observed_sessions` row removes every child in the same transaction.

## Refresh model

Two sidecar crons keep the DB fresh independent of caller demand:

- `conversation-observability-refresh.md` — every 5 minutes (offset from `ir-index-rebuild` by 2 minutes), `max_sessions=5`, `stale_only=true`. Runs all three non-LLM refreshers.
- `conversation-observability-summarize.md` — every 2 hours, `max_sessions=3`, feature-gated on `conversation_observability.summaries.enabled` (default off).

The `claude_session_summary` context source also triggers a stale-only refresh inline before rendering so bundle collections never read a cold DB. The `/ir/index` endpoint is deliberately NOT hooked — stale-only DB-backed scans are cheap enough that an independent cron is cleaner than embedding-service coupling.

## Lifecycle

The artifact uses `INFINITE_LIFECYCLE` (paired with the new `NeverExpires` lifecycle trigger): every row is derived from JSONL session files that may have been deleted, so losing the DB means losing data that cannot be recovered. The sweep tick will see the artifact but never remove rows. Bumping any version constant in `summaries.py` invalidates cached summaries (re-summarize on next refresh); the row schema otherwise has no automatic eviction.

## Consumers

- `work_buddy/context/sources/claude_session_summary.py` — context source rendering one block per project, listing each session's commits and uncommitted files. Sibling to `chat` (raw inventory) and `session_activity` (current MCP session ledger).
- `work_buddy/collectors/git_collector.py` — receives `{short_sha: full_session_id}` via `inspector.build_session_map()`, which now reads from the DB instead of computing per-call.
- MCP capabilities: `conversation_observability_refresh`, `conversation_observability_uncommitted`, `conversation_observability_get`, `conversation_observability_list`, `conversation_observability_summarize`, `conversation_observability_summary_get`.
- Journal directions (`journal/update-directions`) require `claude_session_summary.md` alongside `git_summary.md` / `chat_summary.md` so multi-hour sessions without commits get logged as exploration rather than silently dropped.
