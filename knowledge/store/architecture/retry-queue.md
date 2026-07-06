---
name: Async Execution Queue
kind: concept
description: 'Unified disk-backed queue for three kinds of background work: retries of transient failures, deferred submissions (llm_submit), and scheduled jobs — shared sidecar sweep, per-reason policy (backoff, failure escalation)'
summary: 'Unified disk-backed queue for three kinds of background work: (1) retries of transient failures (auto-enqueued by the gateway), (2) deferred submissions from llm_submit and similar APIs, (3) scheduled cron jobs. All share the sidecar''s sweep loop; the queue_reason field drives per-category policy (backoff vs not, loud vs quiet failure). On completion the originating agent session is notified via messaging.'
tags:
- retry
- queue
- sidecar
- transient
- backoff
- operations
aliases:
- retry queue
- async queue
- execution queue
- auto retry
- transient failure
- retry sweep
- queued_for_retry
- queued
- queue_reason
- deferred submit
- llm_submit
- operation retry
- background retry
- background inference
parents:
- architecture
- architecture
dev_notes: |-
  ## Sweep-side disabled-capability recovery

  `_replay` checks `get_disabled_registry()` when the active registry lookup misses. If the capability is disabled, calls `recheck_disabled_capability(name)` (per-capability re-probe with 30s cool-down). If recheck recovers, re-fetches from the active registry. If recheck still fails, falls back to invoking the disabled entry's callable anyway — the bridge call inside will raise a typed transient exception, which the existing exception handler re-queues correctly. Strictly better than reporting `"not found in registry"`.

  Do NOT reach for `invalidate_registry()` here. Full registry rebuild is ~6–8s and re-probes every tool. Per-capability is the right tool for transient probe failures. See `architecture/capability-registry` for the picking rule.

  ## Effects-aware pre-verify in the sweep

  `_pre_verify_pwu(pwu_carrier, record)` takes the op record as a second arg so it can look up the capability's declared effects. When `entry.effects` is non-empty, calls `verify_post_write_effects(effects, params=record['params'])` instead of single-effect verify. Same verdicts; only `verified` (all effects present) skips the replay.

  The partial verdict triggers a normal replay — the sweep runs the full capability via `entry.callable(**params)`. Capabilities with declared effects are required to be idempotent under retry. Future work: declarative sub-step dispatch so partial recovery can replay only the missing effect, not the full pipeline.

  ### Wrapper-aware inner manifest resolution

  When the queued capability's own `effects` manifest is empty AND the capability name is in `_WRAPPER_CAPS` (`retry`, `obsidian_retry`), `_pre_verify_pwu` resolves the inner op via `_resolve_inner_op_for_wrapper(record)` — reads `params.operation_id`, loads `<operations_dir>/<inner_id>.json`, and looks up the inner capability's manifest in both the active registry and the disabled registry. The verifier then walks the inner manifest with the inner op's params. Falls back to single-effect verify on the carrier path only when the inner record can't be loaded or has no manifest itself.

  The disabled-registry fallback in the lookup is load-bearing: a transient tool probe failure can leave the inner capability (e.g. `task_create`) in `_DISABLED_REGISTRY` while its effects manifest is unchanged — bypassing that fallback would force single-effect-verify in exactly the conditions where multi-effect was needed.

  The same wrapper-aware branch lives in `mcp_server/tools/gateway.py`'s CP5 exception path, so a fresh `obsidian_retry` call (not just sweep replays) gets the same manifest-aware verify.

  ## Verified-dict inspection in `_replay`

  `_partial_verified_fields(result.get('verified'))` returns the keys whose value is not `True` / `"verified"`. Called after the inner result clears the existing `error` / `success: False` checks; a non-empty list short-circuits the success path and returns `{success: False, transient: True, error: "..."}` so the outer sweep re-enqueues via `_schedule_next`. The helper accepts both the legacy boolean shape and the string-verdict shape so capabilities can migrate independently. Capabilities that don't return a `verified` field are unaffected — the helper returns `[]` on missing or non-dict input.

  Why this matters: the sweep's success criterion was historically "no `error` field, `success` is not False." A capability could return `{success: True, verified: {task_line: False, store: True, note: True}}` and the sweep would fire `retry_success` despite the partial write. With this check the sweep treats partial verification as the same signal class as a transient bridge error — re-enqueue and try again on a fresh sweep tick.

  ## Why op-record `originating_session_id` is the consent reference

  The op record already carries `originating_session_id` (set by `_save_operation` from `WORK_BUDDY_SESSION_ID`). The sidecar's `_replay` binds a `replay_of(originating_session_id)` consent principal (and sets the `set_originating_session` contextvar for cost/artifact attribution). The consent layer resolves against the principal's session at check time, riding individual grants only. Net: zero schema changes, zero new persistence, full cross-session inheritance for replays with no workflow-grant time-travel.

  ## Out-of-band enqueue seam (`enqueue_capability_for_retry`)

  `work_buddy.mcp_server.tools.gateway.enqueue_capability_for_retry(capability, params, *, error, error_kind=None, originating_session_id=None) -> str | None` is the public seam for callers that do NOT dispatch through `wb_run` but still want transient-failure recovery — e.g. the Telegram capture handler, which runs in a separate sidecar process and calls the capability function directly. It creates a failed op record via `_save_operation` (carrying the capability's declared `retry_policy`, default `verify_first`) and enqueues it via `_enqueue_for_retry` with `error_class='transient'`, so the sidecar sweep replays the capability from the registry on backoff and re-reads vault state fresh each attempt.

  The caller is responsible for only enqueuing genuinely transient failures — gate on `work_buddy.errors.classify_error(exc) == 'transient'` before calling. Returns the op_id, or `None` if persistence failed (the caller should then surface the original error rather than claim a queued retry). `originating_session_id` defaults to the op record's `session_id` (`WORK_BUDDY_SESSION_ID`), bound by the sweep as the `replay_of` consent principal — so a grant held by that session authorizes the replay without re-prompting. Consumer: `work_buddy/telegram/handlers.py::_do_capture`.

  ## Idempotency refresh on replay

  All three replay paths — gateway `retry_operation`, `obsidian_retry`, and the sidecar `_replay` — call `work_buddy.obsidian.tasks.mutations.refresh_idempotency_on_replay(capability, params)` immediately before re-invoking the capability. For `task_create` it re-stamps the create-idempotency cache entry (reading the raw file and bumping `ts`, IGNORING the entry's TTL) so an actively-retrying chain keeps its minted `(task_id, note_uuid)` alive past the cache's wall-clock TTL (`mutations._IDEMPOTENCY_TTL_SEC`, 300s). Without it, a consent-delayed retry whose approval lands after the TTL would miss the cache and mint a fresh note UUID — orphaning the note the first attempt already wrote. The hook is a no-op for non-idempotent capabilities and when no cache entry exists, so it is safe to call unconditionally on every replay. The TTL still governs genuinely distinct identical-text creates (only an active replay re-stamps), so dedup semantics for unrelated creates are unchanged.
---

## Agent-facing quick rules

- **When `wb_run` returns `{queued_for_retry: true}`**: do NOT treat as success. Wait for either a `retry_success` or `retry_exhausted` notification. For mutating operations, verify the side effect landed (file content, store record) before reporting back. Retries CAN fail.
- **When a call returns `{status: "timeout", operation_id}`**: retry with `mcp__work-buddy__wb_run("retry", {"operation_id": "op_xxx"})`. The retry replays the original call without re-sending parameters because the op record stores the capability + params. For Obsidian-bridge-dependent ops use `obsidian_retry` instead (bridge health-checks between attempts).
- **`llm_submit`**: the return payload includes `operation_id` and a `hint` explaining how to retrieve the result via `wb_run("wb_status", {operation_id})`. The originating session also receives a messaging ping on completion.
- **Op records carry `error_kind` for typed failures** (post-CP4): when an `ObsidianError` (or other typed exception with an `error_kind` attribute) is caught, the gateway persists it on the op record. Useful for post-incident analysis: grep `data/agents/operations/op_*.json` for `error_kind: "obsidian_post_write_uncertain"` to count how often the post-write-uncertain recovery path fires.
- **Per-effect verification short-circuits success**: a capability that returns `result.verified` (a dict per declared effect) is treated as a partial-failure when any field is not `True` or `"verified"` — the sweep re-enqueues rather than reporting success. `task_create` is the canonical caller; the verdict vocabulary is `"verified" | "absent" | "indeterminate" | "partial"`. Capabilities without a `verified` field are unaffected.

## How it works

1. **Canonical fields on the op record**: `queued: bool`, `queue_reason: "retry" | "deferred_submit" | "scheduled_job"`, `error_kind` (post-CP4, optional), `originating_session_id`. The legacy `queued_for_retry: bool` is still written and read as an alias for transitional compatibility. `lease_seconds` on a record overrides the default 90s lease for long-running work (LLM ops set 600s).

2. **Error classification** (work_buddy/errors.py): classify_error(exc) → transient | permanent | unknown. is_transient_result(result) checks soft failures in return dicts.
   - **Typed-exception fast-path**: an `ObsidianError` is classified by its `error_kind`, not by type matching. The "user must act out of band" kinds — `obsidian_refused`, `obsidian_not_running`, `obsidian_plugin_missing`, `obsidian_plugin_disabled` — are **permanent** (retrying without user action never succeeds, so they are not auto-enqueued); every other ObsidianError kind (startup race, timeout, 5xx, editor conflict, post-write-uncertain) is transient. The same `error_kind` drives the result-dict path, so a raised exception and a `bridge_failure` return classify identically.
   - **error_kind in result dicts (post-CP3)**: when a result dict carries `error_kind` (set by the gateway for typed exceptions, or by capabilities that catch and translate), is_transient_result keys off it directly. Falls back to legacy `_TRANSIENT_PATTERNS` for non-Obsidian capabilities only.

3. **Gateway auto-enqueue for retries** (gateway.py): When wb_run catches a transient exception AND the capability's retry_policy is 'replay' or 'verify_first', _enqueue_for_retry() sets queued=True, queue_reason='retry' on the operation record with a retry_at timestamp. error_kind is propagated into the record + retry_history entry for the attempt.

4. **Post-write-verify (post-CP5)**: when the caught exception is `ObsidianPostWriteUncertain`, the gateway calls `work_buddy.obsidian.post_write_verify.verify_post_write` BEFORE deciding to enqueue. If the filesystem confirms the write landed, the op is marked completed (success-with-warning); otherwise it falls through to the normal enqueue path. Same logic in `tools/gateway.py::retry_workflow_step` and `sidecar/retry_sweep.py::_replay`.

   For capabilities that declare an effects manifest (`Capability.effects` — see `architecture/capability-registry`), the recovery path uses `verify_post_write_effects` which walks every declared effect and can return `partial` (some landed, some missing). Partial is treated like absent at the queue level (enqueue retry); the capability is required to be idempotent under retry so the replay heals the half-written state. The agent-visible response shape is unchanged.

   **Wrapper-aware pre-verify**: when the queued capability is a retry wrapper (`retry` / `obsidian_retry`) whose `params.operation_id` references an inner op, the verifier walks the **inner** capability's manifest with the inner op's params rather than single-effect-verifying just the carrier path. Without this, a PWU carrier pointing at the first of several inner effects would single-effect-verify as `verified` even when later effects never landed — silent partial-state success. Falls back to single-effect verify only when the inner record can't be loaded.

5. **Result-dict verified inspection in `_replay`**: after the inner capability returns success, the sweep checks `result.verified` (when present) for partial-failure signals before marking the op completed. Any field whose value is not `True` (legacy boolean shape) or `"verified"` (string-verdict shape) is treated as a transient failure and re-enqueued. Capabilities without a `verified` field are unaffected. The complementary signal to the pre-verify path above — pre-verify catches the case where the capability never got to run all its effects; post-verify catches the case where it did but a later effect's verification reports absent/indeterminate.

6. **Sidecar sweep recovery from disabled-capability state**: when `_replay` resolves the capability and finds it's NOT in the active registry but IS in the disabled registry (a transient tool probe failure left it disabled), the sweep calls `work_buddy.recovery.recheck_disabled_capability(name)` to re-probe just that capability's missing tools. If the probe now passes, the capability is restored to the active registry and the replay proceeds. If the probe still fails, the sweep invokes the disabled entry's callable directly — the bridge call inside will raise a typed transient exception which the existing exception handler treats as re-queue-worthy. Strictly better than reporting "not found in registry" and exhausting.

7. **Cross-session consent during sidecar replay**: before invoking a replayed callable, `_replay` binds a `replay_of(originating_session_id)` **consent principal** (and also sets the `_originating_session` contextvar for non-consent attribution). The consent layer's `is_granted` / `get_mode` then resolve against the originating session's DB directly — and, being a REPLAY principal, ride individual op-grants only: a workflow grant that was live when the op was queued does NOT time-travel to authorize the replay (see `notifications/consent`, "The three consent principals"). Closes the failure mode where a consented op queued by the user's session would fail `ConsentRequired` on replay, without opening a workflow-blanket time-travel hole.

8. **Deferred submit path** (work_buddy/llm/submit.py): llm_submit() directly writes a record with queued=True, queue_reason='deferred_submit', status='failed', retry_at=now, max_retries=1, lease_seconds=600. Returns immediately with {operation_id, status: 'queued', hint}.

9. **Sidecar sweep** (sidecar/retry_sweep.py): RetrySweep.sweep() runs every cycle of the sidecar's dispatch loop (a background thread, so a long replay delays only other dispatch work, not supervision or state publishing). Scans operation records for queued ops where retry_at <= now, acquires a lease (honoring per-op lease_seconds), and invokes entry.callable(**params). Before the call it binds a `replay_of` consent principal (so per-session consent grants resolve to the originating agent, individual-grants-only) and sets the originating-session contextvar (so per-session artifacts like the LLM cost log resolve to the right agent). The replay path also catches `ObsidianPostWriteUncertain` and verifies before re-enqueueing.

10. **Backoff strategies** (retry-reason only): 'adaptive' (default: 10s, 20s, 45s, 90s, 120s), 'fixed_10s', 'exponential' (10s * 2^n, capped 120s). Deferred submits use 'none' — no backoff, one attempt.

11. **Failure notification** varies by reason:
   - `retry` exhausted → messaging ping to originating session AND loud user notification via all surfaces (Obsidian, Telegram, Dashboard)
   - `deferred_submit` / `scheduled_job` exhausted → messaging ping to originating session only. Agent decides whether to escalate.

12. **Workflow integration**: TaskStatus.RETRY_PENDING blocks dependents without killing the workflow. On success → conductor.resume_after_retry() completes the step and unblocks dependents. On exhaustion → conductor.fail_after_retry_exhaustion() fails the step.

## Agent perspective

- Retry enqueued: wb_run returns {queued_for_retry: true, retry_hint: ..., error_kind?: ...}. Wait for `retry_success` or `retry_exhausted` messaging notification before reporting back. Do NOT treat queued as success.
- Post-write-verify recovered (single-effect): wb_run returns {status: 'ok', post_write_recovery: true, warning: ..., path: ...}. The bridge timed out client-side but the file on disk has the content. No retry needed.
- Post-write-verify recovered (multi-effect): same shape additionally with `effects_verified: <count>`. Every declared effect landed.
- Disabled capability auto-recovered: wb_run returns the normal capability response with `registry_auto_recovered: true`. The capability was in the disabled registry at dispatch time; the gateway or sweep re-probed and restored it before invoking.
- Deferred submit: wb_run("llm_submit", ...) returns {operation_id, status: 'queued', hint, queue_reason: 'deferred_submit'}. Check with wb_status(operation_id); messaging ping lands when it completes.
- `retry_success` payload includes the full inner result. For multi-effect capabilities, inspect `result.verified` per-effect to confirm every effect landed; the sweep already re-enqueued any partial state, so a `retry_success` you receive should show all effects verified.

## Configuration

config.yaml → sidecar.retry_queue: enabled, max_retries (default 5), default_backoff ('adaptive'), max_retry_age_minutes (30).

## Key files

- work_buddy/errors.py — error classification (typed-exception fast-path + legacy patterns)
- work_buddy/obsidian/errors.py — typed Obsidian exception hierarchy (post-CP1)
- work_buddy/obsidian/post_write_verify.py — post-write-uncertain recovery (single- and multi-effect)
- work_buddy/obsidian/effects.py — EffectSpec schema for the multi-effect manifest
- work_buddy/sidecar/retry_sweep.py — RetrySweep class (queue handling, disabled-cap recovery, effects-aware pre-verify, wrapper-aware inner-manifest resolution, verified-dict inspection)
- work_buddy/recovery.py — recheck_disabled_capability / recheck_tool (per-capability re-probe)
- work_buddy/mcp_server/tools/gateway.py — _enqueue_for_retry(), _is_queued() helper, operation record writes (with error_kind post-CP4), effects-aware PWU handler with the same wrapper-aware fallback
- work_buddy/llm/submit.py — llm_submit() async submission
- work_buddy/agent_session.py — set_originating_session / get_originating_session contextvar
- work_buddy/llm/cost.py — _cost_log_path() honors originating-session override
- work_buddy/sidecar/daemon.py — sweep wired into the dispatch loop
- work_buddy/workflow.py — TaskStatus.RETRY_PENDING
- work_buddy/mcp_server/conductor.py — resume_after_retry(), fail_after_retry_exhaustion()
- work_buddy/consent.py — ConsentCache cross-session lookup

## Observability

wb_status() includes retry_queue summary (queued count, next_retry_at). _list_recent_operations() shows 'queued_retry' / 'queued_deferred_submit' / 'queued_scheduled_job' statuses with retry_at and max_retries. error_kind on op records lets you grep for specific failure categories across history.
