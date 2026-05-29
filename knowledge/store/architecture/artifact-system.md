---
name: Artifact System
kind: reference
description: Shared lifecycle infrastructure for any persisted resource — pluggable Storage × Lifecycle × Provenance composition with capability declarations.
summary: 'Shared lifecycle infrastructure for every persisted resource. Composition-based: Storage × Lifecycle (Trigger + ExpiryAction + retention_predicate?) × Provenance, with construction-time coherence validation. 11 registered artifacts (filesystem, messaging, llm-queue, llm-cache, segmentation-cache, chrome-ledger, escalations-log, claude-code-usage, agent-sessions, notifications, logs-global). Single cleanup tick drives off the registry; paths.PRUNERS deprecated. MCP: artifact_save/list/get/delete/cleanup (filesystem-typed) + artifact_cleanup(name?) (cross-backend) + artifact_registry (cross-backend introspection).'
entry_points:
- work_buddy.artifacts
- work_buddy.paths
tags:
- artifacts
- storage
- lifecycle
- ttl
- cleanup
- sessions
- provenance
- composition
- backends
- registry
aliases:
- artifact store
- artifact registry
- save artifact
- list artifacts
- artifact cleanup
- backend protocol
- storage backend
- lifecycle composition
parents:
- architecture
- architecture
dev_notes: |-
  ## Adding a new pruner

  Two parts:

  1. **Define the function** in `work_buddy/artifacts.py` with the signature `(path: Path, config: dict, *, dry_run: bool) -> dict`. Return a dict with at least `bytes_before` and `bytes_after`; conventional extra fields are `pruned`, `remaining`, `pruned_files`, `rollup_groups`. Honor `dry_run` strictly — no writes, no DELETE, no VACUUM under dry-run.

  2. **Register it** in `work_buddy.paths.PRUNERS` with `(callable_path, default_config)`. The dispatcher in `_run_pruners` resolves the path two ways depending on the key:
     - If the key matches a `RESOURCES` entry (e.g. `db/messages`), it goes through `resolve()` and the pruner receives a registered file path.
     - Otherwise the key's first segment becomes a `data_dir(category)` lookup (e.g. `agents/sessions` → `data_dir("agents")`). The pruner receives the directory and walks it itself.

  The fallback's first-segment-only behavior is intentional and worth knowing: a pruner key like `runtime/service-logs` does NOT resolve to `data_dir("runtime/service_logs")`; it resolves to `data_dir("runtime")` and the pruner has to drill down. Either register a `RESOURCES` entry first, or hardcode the subdir inside the pruner function.

  ## Pruner shapes

  Three patterns in the existing set; use the closest match when adding a new one:

  - **Pure-data filter** (`prune_chrome_ledger`, `prune_llm_cache`) — read JSON, drop expired entries, atomic rewrite via `tmp + replace`. Idempotent. Cheap. Status reported via `pruned` / `remaining` / `bytes_before` / `bytes_after`.
  - **SQL purge** (`prune_messages_db`) — open SQLite, count + delete + VACUUM in one transaction. Status reporting uses `pruned` for row count and `bytes_before` / `bytes_after` to capture VACUUM's effect. **Status guards matter** — `prune_messages_db` uses a denylist (`status != 'pending' AND status IS NOT NULL`) rather than an allowlist of terminal statuses, so cleanup tolerates new status names appearing in the schema without code change. The single hard invariant is "never delete pending or unread messages."
  - **Rollup-then-delete** (`prune_claude_code_usage_db`) — group source rows past the horizon into a daily-aggregate sibling table, then delete the originals and VACUUM. The aggregate `INSERT … ON CONFLICT … DO UPDATE SET col = col + excluded.col` makes the rollup an idempotent additive merge: re-running with no eligible rows is a no-op; an overlapping pass accumulates correctly rather than `INSERT OR REPLACE`'s last-write-wins. Use this pattern when the consumer reads aggregates anyway and per-row drilldown of old data is unused.

  ## VACUUM and transactions

  VACUUM cannot run inside a transaction. SQL-purge pruners must commit before VACUUMing; sqlite3's default autocommit-after-commit handles that. Future refactors that wrap a pruner in `with conn:` will throw `cannot VACUUM from within a transaction` — keep the explicit commit.

  ## Why the cron fires twice daily

  The 03:00 + 15:00 cadence in `sidecar_jobs/artifact-cleanup.md` exists so a busy day (heavy chrome browsing, lots of agent sessions) gets cleaned before the next morning's start; nightly-only would let a full active day's worth of artifacts pile up across the user's most active hours. Tune via the cron schedule string, not by adding a second cleanup job.
---

## Overview

Shared lifecycle infrastructure for **any** persisted resource in work-buddy. Replaces what was previously a filesystem-only artifact store + ad-hoc per-module pruners with a single composition-based system: every consumer (filesystem artifacts, messaging DB, caches, sessions, queue, notifications, logs) registers an `Artifact` describing how its data is stored, when records expire, what happens at expiry, and which agent-facing operations are exposed via MCP. The cleanup tick iterates the registry and calls `.prune()` on each — one orchestrator for every persisted resource.

> Historical note. Until t-aade2f16 (May 2026), the system was framed as 'centralized storage for agent-produced output' and was filesystem-only; all non-filesystem cleanup lived in a parallel `paths.PRUNERS` registry of opaque callables. Same `<` vs `<=` boundary bug had been copy-pasted into multiple cache modules (t-96e45c67), evidence the lifecycle pattern needed unification. The unification absorbed PRUNERS, plus brought notifications and the LLM call queue under the same lifecycle (both had been leaking).

## Composition model

Three orthogonal axes; any combination subject to construction-time coherence checks.

```python
Artifact(
    name="...",                       # registered key for the registry
    storage=Storage,                  # where data lives + record shape
    lifecycle=Lifecycle(
        trigger=Trigger,              # WHEN does expiry fire
        action=ExpiryAction,          # WHAT happens at expiry (default Delete)
        retention_predicate=callable, # optional: skip records matching
    ),
    provenance=Provenance | None,     # optional: session tagging / audit
    exposed_operations=frozenset[Operation],  # MCP exposure (forward-compat to per-principal)
)
```

Construction validates coherence: e.g. `PerRecordTtl` paired with `FilesystemStorage` (no records) raises `IncoherentComposition`; `TransformAndDelete` paired with non-records storage same. Calling a method whose required `StorageTrait` is absent (e.g. `.delete_where()` on filesystem) raises `UnsupportedOperation` naming the artifact and the missing trait.

### Storage backends (six)

| Backend | StorageTraits | Used by |
|---|---|---|
| `FilesystemStorage` | ATOMIC_BLOBS, LISTABLE, DELETABLE | filesystem artifacts (the original 6 types) |
| `SqliteRowsStorage` | RECORDS, TYPED_COLUMNS, LISTABLE, DELETABLE, BULK_PRUNEABLE | messaging, llm-queue |
| `JsonRecordsStorage` | RECORDS, BULK_PRUNEABLE | llm-cache, segmentation-cache, chrome-ledger |
| `JsonlStorage` | RECORDS, APPEND_ONLY, BULK_PRUNEABLE | escalations-log |
| `SqliteRollupStorage` | RECORDS, TYPED_COLUMNS, BULK_PRUNEABLE | claude-code-usage |
| `DirectoryTreeStorage` | RECORDS, LISTABLE, DELETABLE | agent-sessions, notifications, logs-global |

### Lifecycle: triggers × actions × retention

The lifecycle is itself composed of three orthogonal pieces — separating them avoids bundling a *trigger* (when to fire) with an *operation* (what happens) into one component. New combinations are additive — pairing `TransformAndDelete` with `PerRecordTtl` requires no new code.

**Triggers (4):** `PerTypeTtl` (filesystem TTL by type), `PerRecordTtl` (each record carries its own `expires_at` or has a TTL computed from a creation timestamp), `TimeWindow` (drop records older than `now - window_days`), `MtimeWindow` (filesystem mtime, optionally with an activity-check callable).

**Actions (2 today):** `Delete` (default) and `TransformAndDelete` (rollup-then-delete; only claude-code-usage). Speculative actions (Archive, Compact, Snapshot, …) are **not** built per the one-consumer rule — added when a real consumer needs them.

**Retention predicate (modifier):** Optional callable that returns `True` to keep a record despite the trigger marking it expired. Used by messaging (preserve `status=='pending'`), llm-queue (preserve `pending`/`in_flight`), notifications (preserve non-terminal).

### Provenance (1 flavor today)

`SessionTagged` extracts the creating session id from a record. Accepts either a single field name (filesystem `session_id`, agent-sessions) or an ordered list of candidate columns (messaging `sender_session` / `recipient_session`, first non-null wins). Justifies the `list_by_session(sid)` operation on the Artifact composer.

## Eleven registered artifacts

Each consumer registers one `Artifact` from its own module at import time (or at the end of `work_buddy.artifacts.default_registrations` for the two backends without a natural consumer module). `sweep_all` and `artifact_registry_dump` lazily import all consumer modules so the registry is fully populated by the first cleanup tick.

| Name | Storage | Lifecycle | Provenance | Notes |
|---|---|---|---|---|
| `filesystem` | FilesystemStorage | PerTypeTtl(ARTIFACT_TYPES) + Delete | SessionTagged(session_id) | Exposes save/get/list/delete/cleanup via MCP. Default registration. |
| `chrome-ledger` | JsonRecordsStorage(LIST) | TimeWindow(captured_at, 7d) + Delete | — | |
| `llm-cache` | JsonRecordsStorage(DICT) | PerRecordTtl(expires_at) + Delete | — | Legacy-schema eviction stays in standalone prune for now. |
| `segmentation-cache` | JsonRecordsStorage(DICT) | PerRecordTtl(expires_at) + Delete | — | |
| `escalations-log` | JsonlStorage | TimeWindow(timestamp, 30d) + Delete | — | Malformed lines preserved. |
| `agent-sessions` | DirectoryTreeStorage(SESSION_DIRS) | MtimeWindow(created_at, 14d, activity_check) + Delete | SessionTagged(session_id) | activity_check defers eviction when files modified within cutoff. |
| `claude-code-usage` | SqliteRollupStorage | TimeWindow(timestamp, 90d) + TransformAndDelete(rollup_old_turns) | — | Wraps existing rollup function unchanged. |
| `messages` | SqliteRowsStorage | PerRecordTtl(created_at, 30d) + Delete + retention(keep pending) | SessionTagged(sender_session, recipient_session) | post_delete_sql cleans orphaned message_reads + VACUUM. |
| `logs-global` | DirectoryTreeStorage(LOG_FILES) | MtimeWindow(_mtime, 7d) + Delete | — | Default registration. |
| `notifications` | DirectoryTreeStorage(JSON_FILES) | PerRecordTtl(expires_at) + Delete + retention(keep PENDING/DELIVERED) | — | NEW: previously had no scheduled pruner; ~370 expired records were piling up. |
| `llm-queue` | SqliteRowsStorage | PerRecordTtl(completed_at, 30d) + Delete + retention(keep pending/in_flight) | — | NEW: previously had no DELETE path at all; rows accumulated indefinitely. |

## Cleanup orchestration

`sidecar_jobs/artifact-cleanup.md` runs the `artifact_cleanup` MCP capability twice daily (03:00 and 15:00 in the configured timezone) via the sidecar scheduler. The capability calls `FilesystemStorage.cleanup()` which delegates to `registry.sweep_all()`. Every registered Artifact's `.prune()` runs; results aggregate into the legacy result-dict shape so existing callers see no breaking change.

One tick now does everything in a single uniform pass:
* per-type TTL on filesystem blobs (via the registered `filesystem` Artifact)
* per-record TTL on SQLite tables (messaging, llm-queue) and JSON caches
* time-window cutoffs on the chrome ledger and escalations log
* mtime+activity checks on session dirs and global logs
* rollup-then-delete on the claude-code-usage DB

`paths.PRUNERS` is now empty (deprecated). The standalone `prune_*` callables in `work_buddy.artifacts.meta_pruners` remain importable so existing tests that exercise them with custom paths keep working — they will be removed in a future cleanup PR.

## File layout (filesystem backend)

```
<data_root>/<type>/<YYYYMMDD-HHMMSS>_<slug>.<ext>         # the artifact
<data_root>/<type>/<YYYYMMDD-HHMMSS>_<slug>.meta.json     # sidecar metadata
```

Metadata captures: creating session id, tags, description, expiry, original artifact id. Per-type TTL: `context` 7d, `export` 90d, `report` 30d, `snapshot` 14d, `scratch` 3d, `commit` 90d. Unregistered types get the 14-day default.

## MCP capabilities

* `artifact_save(content, type, slug, ext?, tags?, description?, ttl_days?)` — filesystem-typed save. Returns the new ArtifactRecord.
* `artifact_list(type?, since?, tags?, session?, include_expired?, limit?)` — filesystem-typed list with filters.
* `artifact_get(id)` — filesystem-typed read; metadata + inline content for files <50 KB.
* `artifact_delete(id)` — filesystem-typed delete.
* `artifact_cleanup(dry_run?, name?)` — sweep registered artifacts. With no `name`, sweeps all 11. With `name="llm-cache"` etc., scopes to a single artifact. Note: `name` is deliberately distinct from `artifact_save`'s `type` field (which means filesystem subtype).
* `artifact_registry()` — returns the cross-backend introspection map: every artifact's name, storage_kind, lifecycle_kind, provenance_kind, capabilities (i.e. its declared `StorageTrait` set), exposed_operations. Replaces grep'ing paths.py for resource definitions.
* `commit_record(...)` — record commit metadata as a filesystem artifact (specialised convenience).

### Exposure-declaration trajectory

Every registered Artifact carries an `exposed_operations: frozenset[Operation]` field (today a flat set, e.g. `{SAVE, GET, LIST, DELETE, CLEANUP}` for filesystem). When the permissions model arrives, this evolves into a per-principal map (`{Principal.AGENT: ..., Principal.PROGRAMMATIC: ...}`) with consent gating per operation. The shape is additive — when permissions become real, declarations extend; existing call sites don't move.

## User-/agent-facing time formatting

`work_buddy.artifacts.expiry.format_for_user(dt)` is the centralized helper for any datetime surfaced to a user or agent. It converts to the timezone configured in `display.timezone` (config.yaml; falls back to system local) and includes the timezone abbreviation in the formatted output. ArtifactRecord.to_dict() now emits `created_at_display` and `expires_at_display` alongside the raw ISO fields. Wider sweep across messaging / notification / journal surfacings is a follow-up task.

## Config

* `paths.data_root` (in `config.yaml`, with optional `config.local.yaml` overlay) controls where data lives. Shipped default `".data"`.
* `display.timezone` (optional, in config.yaml) sets the user's display timezone for `format_for_user`.

## Key files

```
work_buddy/artifacts/
  protocol.py         StorageTrait/Operation enums, Storage/Lifecycle/Provenance protocols,
                      Ref + SweepResult dataclasses, Artifact composer, exceptions.
  registry.py         register_artifact, sweep_all (lazy-imports consumer modules),
                      artifact_registry_dump, _CONSUMER_MODULES tuple.
  expiry.py           is_expired (UTC-aware, boundary-inclusive), expires_at_iso,
                      format_for_user (display-tz formatter).
  io.py               atomic_write_bytes / atomic_write_text shared by every backend.
  default_registrations.py    Registers filesystem + logs-global at import time.
  meta_pruners.py     Transitional home for standalone prune_* callables. Removable.
  backends/           filesystem, sqlite_rows, json_records, jsonl, sqlite_rollup,
                      directory_tree.
  lifecycle/
    composer.py       (Lifecycle dataclass actually lives in protocol.py.)
    triggers/         per_type_ttl, per_record_ttl, time_window, mtime_window.
    actions/          delete (default), transform_and_delete.
  provenance/
    session_tagged.py
```

Consumer modules where Artifacts are registered at import time:
* `work_buddy/collectors/chrome_ledger.py` (chrome-ledger)
* `work_buddy/llm/cache.py` (llm-cache)
* `work_buddy/journal_backlog/segmentation_cache.py` (segmentation-cache)
* `work_buddy/llm/escalation_log.py` (escalations-log)
* `work_buddy/agent_session.py` (agent-sessions)
* `work_buddy/llm/claude_code_usage/rollup.py` (claude-code-usage)
* `work_buddy/messaging/models.py` (messages)
* `work_buddy/notifications/store.py` (notifications)
* `work_buddy/llm/queue.py` (llm-queue)

Other entry points:
* `work_buddy/paths.py` — `RESOURCES` (path registry) + `PRUNERS` (now empty, deprecated)
* `work_buddy/mcp_server/registry.py` — artifact_* + artifact_registry MCP capability declarations
* `sidecar_jobs/artifact-cleanup.md` — scheduled cleanup job (no change needed; calls artifact_cleanup MCP capability)
* `tests/unit/test_artifact_protocol.py` — end-to-end Lifecycle smoke tests
* `tests/unit/test_artifact_backends.py` — per-backend Storage protocol tests
