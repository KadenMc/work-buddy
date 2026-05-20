---
name: Typed Obsidian Exception Hierarchy
kind: concept
description: Typed ObsidianError subclasses raised by the bridge layer; classified by isinstance + error_kind rather than substring matching
tags:
- obsidian
- bridge
- errors
- typed-exceptions
- error-kind
- classification
aliases:
- ObsidianError
- ObsidianTimeout
- ObsidianPostWriteUncertain
- ObsidianEditorConflict
- error_kind
- typed exceptions
- obsidian error hierarchy
parents:
- obsidian
- obsidian
---

## The hierarchy

Defined in `work_buddy/obsidian/errors.py`. Mirrors the four-state taxonomy in `bridge.get_last_bridge_state`. Each subclass carries an `error_kind` string that survives serialization (op records, result dicts, notifications, dashboard sparkline).

```
ObsidianError                          error_kind = 'obsidian_unknown'
├── ObsidianUnreachable                error_kind = 'obsidian_unreachable'
│   ├── ObsidianNotRunning             error_kind = 'obsidian_not_running'        TERMINAL
│   ├── ObsidianPluginMissing          error_kind = 'obsidian_plugin_missing'     TERMINAL
│   ├── ObsidianPluginDisabled         error_kind = 'obsidian_plugin_disabled'    TERMINAL
│   └── ObsidianStartupRace            error_kind = 'obsidian_startup_race'
├── ObsidianTimeout                    error_kind = 'obsidian_timeout'
│   └── ObsidianPostWriteUncertain     error_kind = 'obsidian_post_write_uncertain'
│       carries (path, content_hint, write_mode)
└── ObsidianHTTPError                  carries (status, body)
    ├── ObsidianEditorConflict         error_kind = 'obsidian_editor_conflict'
    ├── ObsidianRefused                error_kind = 'obsidian_refused'            PERMANENT
    └── ObsidianServerError            error_kind = 'obsidian_server_error'
```

## Naming convention: `pwu` / `PWU`

Throughout the codebase — variable names, op record fields, log messages, test class names, comments — the abbreviation `pwu` (lowercase) and `PWU` (uppercase) refers to `ObsidianPostWriteUncertain`. You'll see things like:

- `pwu_carrier` on op records: the `{path, content_hint, write_mode}` triple persisted by the gateway when an `ObsidianPostWriteUncertain` was enqueued, so the retry sweep can pre-verify before replaying (see CP-A7 below).
- `_pre_verify_pwu` in `work_buddy.sidecar.retry_sweep`: the helper that re-reads the file before each sweep replay attempt.
- Log lines like `_pre_verify_pwu: VERIFIED for path=...` or `bridge_retry: propagating ObsidianPostWriteUncertain ...`.

The shorthand exists because `ObsidianPostWriteUncertain` is one of the most-referenced types in the recovery infrastructure (CP5 + CP-A6 + CP-A7) and writing it out everywhere makes lines unwieldy. It is NOT a term of art outside this codebase. New readers should treat `pwu` as a literal alias for the full class name.

## Discipline

**Bridge layer raises typed exceptions.** `_request_with_status` and `write_file_raw` translate urllib failures and HTTP statuses into typed exceptions at the boundary. They no longer return False/None for failure (CP6 removed the transitional shim).

**Capabilities do NOT try/except by default.** The pre-CP6 sprawl of 'wrap bridge call, translate to result dict' was removed. Capabilities call bridge functions and let exceptions propagate. The `@bridge_retry` decorator catches transient subclasses, retries, and translates to `bridge_failure(...)` dicts at exhaustion. The gateway's outer try/except classifies via `isinstance` and enqueues for retry.

**Critical exception** (CP-A6): `@bridge_retry` and `obsidian_retry` MUST NOT catch and retry `ObsidianPostWriteUncertain`. Blind retry causes double-writes (each attempt re-reads the file, including any late-committed plugin write, and inserts again). Both raise it through to the gateway, which decides via verify-then-decide.

**Capabilities catch selectively** for genuine domain-specific recovery:
- `vault_writer.vault_write` catches `ObsidianUnreachable` for the filesystem fallback.
- `inline_todos.cleanup_handled_todos` catches `ObsidianError` per-file because it's a best-effort batch operation that aggregates per-file errors.
- `obsidian/tasks/mutations.py::delete_task` catches `ObsidianError` to record partial-state in its `removed` dict.

**Gateway classifies once** at the top of `wb_run` dispatch (`mcp_server/tools/gateway.py`). `classify_error` uses `isinstance(exc, ObsidianError)` first; `is_transient_result` checks `error_kind` in result dicts before falling back to legacy string matching. The retry sweep (`sidecar/retry_sweep.py::_replay`) does the same.

## Policy table

| Kind | classify_error | bridge_retry | Vault fallback safe? |
|---|---|---|---|
| obsidian_not_running | transient | SHORT-CIRCUIT (terminal) | yes (body not sent) |
| obsidian_plugin_missing | transient | SHORT-CIRCUIT (terminal) | yes |
| obsidian_plugin_disabled | transient | SHORT-CIRCUIT (terminal) | yes |
| obsidian_startup_race | transient | retry then exhaust | yes |
| obsidian_unreachable (base) | transient | retry | yes |
| obsidian_timeout | transient | retry | NO (writes; reads ok) |
| obsidian_post_write_uncertain | transient | **PROPAGATE (CP-A6)** — never retry | NO (gateway verifies first) |
| obsidian_editor_conflict | transient | retry | NO (would clobber typing) |
| obsidian_refused | PERMANENT | re-raise immediately | NO (wouldn't help) |
| obsidian_server_error | transient | retry | NO (cache divergence) |

## Post-write-verify (CP5)

`ObsidianPostWriteUncertain` (aka `pwu`) is the most semantically interesting type. It carries `(path, content_hint, write_mode)` so `work_buddy.obsidian.post_write_verify.verify_post_write` can read the file from FILESYSTEM and decide:

- `verified` → success-with-warning (no retry; closes the latent double-write hazard)
- `absent` → fall through to normal failure path (enqueue retry, with carrier persisted on the op record — see CP-A7)
- `indeterminate` → same as absent

Wired in three places:
- `tools/gateway.py` wb_run dispatch (around L1020)
- `tools/gateway.py` retry_workflow_step (around L1492)
- `sidecar/retry_sweep.py::_replay`

All write paths benefit. The `(write_mode, content_hint)` shape:
- `replace` → content_hint is `'sha256:<hex>'` of full content; verifier hashes file and compares
- `insert` / `append` → content_hint is the first 256 chars of inserted payload; verifier substring-searches

## Cross-tick double-write closure (CP-A7)

CP5 alone closed the double-write hazard for the FIRST gateway dispatch. CP-A6 closed it inside `@bridge_retry`. CP-A7 closes it across sweep ticks. Together the three layers handle every retry path:

1. **Inside `@bridge_retry`** (CP-A6): exception propagates immediately, no decorator-driven retries.
2. **At gateway dispatch** (CP5): `verify_post_write` decides verified vs. enqueue.
3. **Across sweep ticks** (CP-A7):
   - Gateway persists `pwu_carrier = {path, content_hint, write_mode}` on the op record when verify says absent/indeterminate (Fix A).
   - `retry_sweep._replay` calls `_pre_verify_pwu(carrier)` BEFORE invoking the capability (Fix B). If verified, mark complete and skip the replay. If absent/indeterminate, strip the stale carrier and proceed with normal replay.
   - `_replay`'s except-PWU branch persists a fresh carrier when its own bridge call raises PWU — so the NEXT sweep tick can pre-verify too.

The race that CP-A7 closes: gateway verify reads filesystem at T1 (says absent); plugin late-commits the write at T2; sweep replays at T3. Without pre-verify, the sweep's read-modify-write reads the now-late-committed file and adds another insertion. With pre-verify, the sweep sees the late commit and short-circuits.

## Dashboard contract preserved

The dashboard sparkline (`work_buddy/dashboard/api.py::get_bridge_status`) consumes module-level `_last_failure_kind` strings (`'timeout' | 'unreachable' | 'http_error' | ''`). The bridge layer maps typed exceptions to these strings BEFORE raising:

- `ObsidianTimeout` and subclasses → `'timeout'`
- `ObsidianUnreachable` and subclasses → `'unreachable'`
- `ObsidianHTTPError` and subclasses → `'http_error'`

Frontend (`scripts/tabs/settings.py`, the `loadActivity` function) maps these to bar classes (`bar-ok`, `bar-slow`, `bar-fail`, `bar-unreachable`) unchanged.

## Why typed > substring matching

Pre-CP1, `work_buddy/errors.py::_TRANSIENT_PATTERNS` was the load-bearing classification mechanism. It substring-matched error messages against ('timed out', 'bridge', 'editor_dirty', 'urlopen error', 'winerror 10061', ...). This had a concrete bug: `vault_write_at_location` returned `'Failed to write note: <path>'` on bridge failures — NONE of the transient keywords appeared. Gateway classified as permanent, never enqueued, the user saw 'failed' even though the write had succeeded server-side. (See op_34ab708a investigation in DECISIONS.md.)

The typed system fixes this at the source: type IS the signal. Messages can say anything; the gateway keys off `isinstance(exc, ObsidianError)` and `exc.error_kind`. CP9 removed the Obsidian-specific patterns from `_TRANSIENT_PATTERNS` since they're no longer load-bearing.

## What was removed in CP9

- The legacy `EditorConflict` exception class and the `EditorConflict = ObsidianEditorConflict` alias. Use `ObsidianEditorConflict` directly.
- The Obsidian-specific entries in `_TRANSIENT_PATTERNS`. The remaining list serves only non-Obsidian transient failures.
