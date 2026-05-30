---
name: Obsidian Bridge
kind: integration
description: HTTP bridge to Obsidian — eval_js, latency handling, timeout retry rules
summary: HTTP bridge on 27125 exposing bridge.eval_js; intermittent ~4s latency spikes; on failure retry per 60/60/60 protocol (or obsidian_retry), then admit failure. NEVER bypass to direct vault writes or REST.
ports:
- 27125
entry_points:
- work_buddy.obsidian.bridge
- work_buddy.obsidian.errors
- work_buddy.obsidian.post_write_verify
tags:
- obsidian
- bridge
- eval_js
- port-27125
- latency
- typed-exceptions
- error-kind
aliases:
- eval_js
- obsidian HTTP
- bridge timeout
- obsidian plugin
parents:
- obsidian
- obsidian
dev_notes: |-
  ## eval_js_internal vs eval_js_for_write

  Both bypass the `obsidian.eval_js` consent gate. The difference shows up on TIMEOUT:

  - `eval_js_internal` returns `None` silently on timeout (uses `_request`).
  - `eval_js_for_write(code, *, write_path, content_hint, write_mode='insert', timeout=15)` raises typed `ObsidianPostWriteUncertain` (uses `_request_with_status`).

  For any JS body that mutates the vault, ALWAYS use `eval_js_for_write`. The silent-None path lets atomic-write helpers fall through to non-atomic paths without signal, which can clobber concurrent user typing in the editor. The PWU translation routes recovery through the gateway's verify-then-decide handler so the partial-write is detected.

  ## write_mode="absent" decision matrix

  For delete-style operations the witness's ABSENCE proves the delete landed. The verifier:

  - file doesn't exist on disk → verified (strongest absent state)
  - file exists, witness IS in content → absent (delete didn't land)
  - file exists, witness NOT in content → verified

  Without this mode, atomic-delete operations get inverted verify semantics: a successful delete substring-verifies as "absent" → enqueues retry, retry idempotent-no-ops as "file already clean." End state correct but with wasted work.

  ## Substring vs sha256 witness trade-off

  For concurrently-modified files (master task list, archive, journals), substring witness is robust to unrelated changes. sha256 verify gives false negatives any time anything else touches the file between PUT and verify — trips spurious retry-exhausted notifications. Reach for substring whenever the inserted content has a unique-by-construction marker (task_id, timestamp, header line).
---

HTTP bridge on 27125 exposing bridge.eval_js; intermittent ~4s latency spikes; on failure the framework's `RetryStrategy` retries via `@bridge_retry` (max 3 attempts, jittered backoff with `base≈60s`, capped at `max(wait, 30s)`) — or `obsidian_retry` does the same explicitly — then admit failure. NEVER bypass to direct vault writes or REST.

## Typed exception hierarchy (post-CP1–CP9)

The bridge layer raises subclasses of `work_buddy.obsidian.errors.ObsidianError` on failure. The gateway classifies via `isinstance` (not substring matching). Each instance carries an `error_kind` string that survives serialization (op records, result dicts, notifications).

```
ObsidianError                          error_kind = 'obsidian_unknown'
├── ObsidianUnreachable                error_kind = 'obsidian_unreachable'
│   ├── ObsidianNotRunning             error_kind = 'obsidian_not_running'
│   ├── ObsidianPluginMissing          error_kind = 'obsidian_plugin_missing'
│   ├── ObsidianPluginDisabled         error_kind = 'obsidian_plugin_disabled'
│   └── ObsidianStartupRace            error_kind = 'obsidian_startup_race'
├── ObsidianTimeout                    error_kind = 'obsidian_timeout'
│   └── ObsidianPostWriteUncertain     error_kind = 'obsidian_post_write_uncertain'
│       carries (path, content_hint, write_mode)
└── ObsidianHTTPError                  carries (status, body)
    ├── ObsidianEditorConflict         error_kind = 'obsidian_editor_conflict'
    ├── ObsidianRefused                error_kind = 'obsidian_refused' (PERMANENT)
    └── ObsidianServerError            error_kind = 'obsidian_server_error'
```

Every kind classifies as `transient` EXCEPT `obsidian_refused` (4xx other than 409 — structural refusal, no retry will help).

## Four-state taxonomy (legacy strings still propagated)

The pre-typed-exception four-state classification still flows through `get_last_bridge_state()` and the dashboard sparkline (`work_buddy/dashboard/api.py::get_bridge_status`). The bridge module sets `_last_failure_kind` to `'timeout' | 'unreachable' | 'http_error' | ''` on every failure path, derived from the typed exception class. The dashboard frontend (`scripts/tabs/settings.py`, the `loadActivity` function) maps these strings to bar classes (`bar-ok`, `bar-slow`, `bar-fail`, `bar-unreachable`). This contract is preserved.

Every bridge-dependent capability's failure response also carries:
- `_bridge_state`: one of `ok`, `timeout`, `obsidian_not_running`, `plugin_not_installed`, `plugin_disabled`, `obsidian_startup_race`, `http_error`, `unknown`
- `_bridge_state_detail`: human-readable explanation
- `_bridge_terminal`: `true` when the state is one that retrying will never fix (obsidian_not_running / plugin_not_installed / plugin_disabled). `obsidian_startup_race` (Obsidian + plugin up but the port hasn't bound yet) is explicitly NON-terminal — the transient startup window, worth retrying. `@bridge_retry` short-circuits on terminal states via `_BridgeHealthGate`'s terminal-classification path. The typed-exception path uses the analogous `_TERMINAL_OBSIDIAN_ERROR_KINDS` set in `work_buddy.obsidian.retry`. The string `_bridge_state` and the typed `error_kind` are kept in lock-step: both `get_last_bridge_state` and `_refine_unreachable_kind` derive from one shared `_classify_unreachable` decision, so a startup race classifies non-terminal in both representations.

## Post-write-uncertain recovery (CP5)

A client-side timeout AFTER a PUT body has been sent is ambiguous: the plugin may have committed the write before the response failed to arrive. `write_file_raw` raises `ObsidianPostWriteUncertain(path, content_hint, write_mode)`. The gateway's exception handler catches it and dispatches to `work_buddy.obsidian.post_write_verify.verify_post_write`, which reads the file from FILESYSTEM (not bridge) and decides:

- `verified` → success-with-warning (no retry; closes the latent double-write hazard)
- `absent` → fall through to normal failure path (enqueue retry)
- `indeterminate` → same as absent

`write_mode` controls verifier semantics: `"replace"` matches a sha256 hint; `"insert"` / `"append"` checks the hint as a substring; `"absent"` (delete-style operations) inverts — verified iff the hint is NOT in the file.

For capabilities that produce multiple external effects (declared via `Capability.effects`), the recovery path uses `verify_post_write_effects` which walks every declared effect and can return `partial` (some landed, some not). See `architecture/retry-queue` for the recovery semantics and `work_buddy.obsidian.effects.EffectSpec` for the schema.

Wired in three places: `tools/gateway.py` wb_run dispatch, `tools/gateway.py` retry_workflow_step, `sidecar/retry_sweep.py::_replay`. All write paths benefit.

## Race-safe line mutations

For master-task-list and other concurrently-edited files, prefer the atomic helpers over `bridge.read_file` + `bridge.write_file`:

- `bridge.atomic_replace_line_by_task_id(file_path, task_id, expected_old_line, new_line)` — atomically rewrites the line containing `🆔 {task_id}` via Obsidian's `app.vault.process()`. Returns `{found, conflict, replaced, line_number, old_line, new_line}`. Sets `conflict=True` (without writing) when the in-vault line content differs from `expected_old_line` — caller decides whether to retry, escalate, or accept.
- `bridge.atomic_delete_line_by_task_id(file_path, task_id)` — same shape with `removed` instead of `replaced`.
- `bridge.write_file(path, content, *, write_mode="replace", content_hint=None)` — the consent-gated wrapper accepts `write_mode` and `content_hint`. For files that change concurrently, prefer `write_mode="insert"` with a unique substring witness; full-file sha256 is fragile against unrelated concurrent writes.

## Diagnostic helpers

Classification is cheap: `get_last_bridge_state()` reads module-level counters set by `_request_with_status`, consults `is_obsidian_running()` (process check) and `get_work_buddy_plugin_state()` (filesystem check on `.obsidian/plugins/obsidian-work-buddy/manifest.json` + `community-plugins.json`). On Windows, closed TCP ports often surface as socket timeouts rather than ECONNREFUSED; `_probe_port_open()` disambiguates via a direct TCP probe so timeouts on closed ports reclassify as `unreachable`. The `unreachable` disambiguation (not-running / plugin-missing / plugin-disabled / startup-race) lives in one place — `_classify_unreachable()` — which both `get_last_bridge_state` (string + detail) and `_refine_unreachable_kind` (typed exception) derive from.

Entry points: `work_buddy.obsidian.errors` (typed hierarchy), `work_buddy.obsidian.bridge.get_last_bridge_state`, `work_buddy.obsidian.bridge._request_with_status`, `work_buddy.obsidian.bridge.write_file_raw`, `work_buddy.obsidian.bridge.atomic_replace_line_by_task_id`, `work_buddy.obsidian.bridge.atomic_delete_line_by_task_id`, `work_buddy.obsidian.post_write_verify.verify_post_write`, `work_buddy.obsidian.post_write_verify.verify_post_write_effects`, `work_buddy.obsidian.effects.EffectSpec`, `work_buddy.obsidian.retry.bridge_failure` (auto-enriches), `work_buddy.obsidian.retry.bridge_retry` (decorator — a thin shim that runs `RetryStrategy → _BridgeHealthGate → call` via `guarded_call_sync`; see `architecture/resilience`), `work_buddy.obsidian.retry.obsidian_retry` (capability), `work_buddy.health.requirement_checks.get_work_buddy_plugin_state`.

## What was removed in CP9

- The legacy `EditorConflict` exception class and the `EditorConflict = ObsidianEditorConflict` alias. Use `ObsidianEditorConflict` directly.
- The Obsidian-specific entries in `work_buddy/errors.py::_TRANSIENT_PATTERNS` (`bridge`, `editor_dirty`, `urlopen error`, `winerror 10061`). Obsidian failures take the typed-exception fast-path; the residual list serves only non-Obsidian transient failures.
