---
name: Consent System
kind: directions
description: How consent-gated operations work — auto-request in gateway, pre-flight bundling, session scope, risk levels
summary: Gateway handles consent transparently for wb_run ops. Ordinary consent uses session-scoped grants; exact-review operations use single-use per-invocation authority with no reusable grant.
trigger: agent calls a capability that touches @requires_consent functions (handled transparently by the gateway)
capabilities:
- consent_list
tags:
- consent
- requires_consent
- risk
- session-scoped
aliases:
- requires_consent
- consent_request
- consent grant
- risk levels
- auto-consent
- pre-flight consent
- consent bundling
parents:
- notifications
- notifications
dev_notes: |-
  ## The three consent principals

  Consent resolution is **principal-scoped**: every `is_granted` / `get_mode` / `diagnose_carry` check resolves against exactly ONE session's `consent.db`, named by a `ConsentPrincipal` (`work_buddy/consent_principal.py`). There is NO implicit "process-default DB" for an agent's check — relying on one is the bug class this model eliminates (an agent's check resolving against the *sidecar's* DB and riding a stale `workflow_run` blanket that was never the agent's).

  | Principal | Bound by | Resolves against | Workflow-carry |
  |---|---|---|---|
  | `human_agent(sid)` | gateway `_invoke_with_session` + `_check_missing_consent` | the agent's own session DB | yes — its own live workflow grants |
  | `sidecar_self()` | sidecar Role-B self-checks (`agent_spawn`, vault-recon spawn gate) | the sidecar's own session DB | yes |
  | `replay_of(sid)` | retry sweep, `obsidian_retry`, gateway retry branch | the originating agent's DB | NO — individual grants only (no time-travel) |

  **INVARIANT:** a new consent check or dispatch path MUST bind a principal via `consent_principal(...)`, or the gate cannot resolve correctly. With no principal bound, `is_granted` falls back to the legacy process-default resolution and emits a one-shot `CONSENT_NO_PRINCIPAL` audit line; flip `consent.set_fail_closed_no_principal(True)` to deny instead. Default is off — binding a principal at every production boundary (gateway / sidecar / retry) is what closes the hole; fail-closed is the durable end-state once every caller is confirmed to bind one.

  The carry policy is a property of the principal *kind* (`ConsentPrincipal.allows_workflow_carry`), not a free-form flag — it is what the older `from_originating` boolean encoded for the replay path. The sidecar's synthetic `WORK_BUDDY_SESSION_ID` (set in `work_buddy/sidecar/__main__.py` "for logging") IS the sidecar principal's session; consult it for consent ONLY via `sidecar_self()`, never as an ambient default for an agent's check.

  **Orphan prevention.** Headless (sidecar-scheduled) workflow runs get an isolated per-run session (`<run_id>-srun`) and a TTL-bounded `workflow_run` grant via `conductor.start_workflow(headless=True)`, so a scheduled run can neither carry-authorize a concurrent sidecar op nor orphan forever. `daemon.run()` reconciles the sidecar's own session at boot to sweep any leftover orphan a hard-killed run left behind. (`get_session_dir` keys session directories on `session_id[:8]`, so per-run ids lead with the uuid-based `run_id` to stay unique — a `sidecar-*` prefix would collide.)

  **Implied consent for thread approvals.** `work_buddy/dashboard/service.py:_post_thread_action` and `work_buddy/threads/group.py:_run_child_accept` wrap `engine.transition` in `user_initiated()` for user-click triggers. The trigger allowlist `_THREAD_USER_INITIATED_TRIGGERS` lives at the top of service.py. To add a new user-click trigger to the bypass, add it to that set; non-listed triggers (e.g. agent-initiated `begin_inference`, `inference_done`) take the bare `engine.transition` path. Tests live in `tests/unit/test_consent_user_initiated.py` (8 cases covering reentrancy, exception cleanup, USER_INITIATED audit emission). `_consent_ctx` is `threading.local`-backed so cross-thread leaks aren't a concern; the side-effect dispatcher runs side effects in the same thread that called `engine.transition`, so the context propagates naturally through the transition machinery.

  **Failure mode the wrapper guards against.** Without `user_initiated`, clicking Approve on a `task_create` action chip fires `ConsentRequired: tasks.create_task (moderate)` and dumps the thread into `AWAITING_REDIRECT` — the user is prompted twice for the same decision (once via the action card showing parameters and risk metadata, again via a fresh consent notification). The action card itself IS the consent surface; the wrapper makes the click count.

  **If you add a new dashboard endpoint** that fires a state-entry side effect invoking a `@requires_consent` capability and bypasses `_post_thread_action`, you must add the wrapper yourself. The capability dispatcher running inside `EXECUTING`'s side-effect handler runs synchronously in the same thread as `engine.transition`, so a `user_initiated` context wrapping the transition covers the entire downstream chain.

  ## Modal-fallback message routing

  The Obsidian plugin's `ObsidianModal.dispatchConsentGrant` (handlers.ts) posts a `consent_grant` message to the messaging service whenever the user clicks an Allow option. Body shape: `{operation, mode, ttl_minutes, notification_id}`. The `notification_id` is the load-bearing field — without it the sidecar cannot resolve the originating agent's session and falls back to writing the grant in its own session DB (a legacy behavior preserved with a WARN log so an out-of-sync plugin doesn't strand the user, but the routing is broken until the plugin is rebuilt + reloaded).

  The sidecar router's `MessagePoller._handle_message` special-cases `subject == "consent_grant"` before the generic capability-dispatch path. `_handle_consent_grant_message(body)` parses the body, calls `resolve_consent_request(notification_id, approved=True, mode=mode, ttl_minutes=ttl_minutes)`, and returns the dispatch status. The `resolve_consent_request` path is the canonical site for both the cross-session routing (it reads `notification.callback_session_id` and threads it through `grant_consent(..., session_id=...)`) and the bundle unbundling (it reads `consent_meta.context.operations` and calls `grant_consent_batch`).

  Already-resolved notifications (the gateway's in-window poll won the race) raise `ValueError` from `respond_to_notification`; the handler catches it and returns a no-op success. This keeps the modal-click → message-dispatch path idempotent without depending on the plugin to know whether the gateway already grabbed the response.

  **`grant_consent` and `grant_consent_batch` accept `session_id` as a keyword.** Plumbs through to `ConsentCache.grant(..., session_id=...)` — the same mechanism workflow blanket grants use to write to a different session's DB than the calling process. The audit log includes the truncated session id in the `GRANTED` details column so cross-session writes are auditable post-hoc.

  **The bundle key (`bundle:<capability>`) is granted alongside individual ops.** It serves as audit-log readability — `GRANTED | bundle:task_create | once` is more informative than three separate GRANTED rows for the underlying ops. No decorator checks the bundle key, so leaving it in the DB doesn't satisfy any gate by accident.

  ## Response → grant pipeline (finalize_consent_response)

  When a user responds to a consent prompt — on any surface — the system has to do two distinct things:

  1. **Record the response** on the notification (status: `pending` → `responded`, persist the chosen mode and surface).
  2. **Translate that response into grants** in the right session's `consent.db` (individual op grants for capability bundles; `workflow_class:<name>` grant for workflow-consent prompts).

  The first is `notifications.store.respond_to_notification`. The second is `consent.finalize_consent_response(notification_id)` — extracted from `resolve_consent_request` so any surface can call it after recording the response. Every response-recording path calls them in that order:

  | Surface | Call site | Pattern |
  |---|---|---|
  | Telegram inline button | `work_buddy/telegram/handlers.py:on_button` | `respond_to_notification` → `finalize_consent_response` → `dispatch_callback` → `_dismiss_others` |
  | Telegram `/reply` command | `work_buddy/telegram/handlers.py:on_reply` | same shape as on_button |
  | Obsidian modal (out-of-band) | sidecar `MessagePoller._handle_consent_grant_message` | routes through `consent.resolve_consent_request` which calls `finalize_consent_response` internally |
  | Dashboard "Approve" endpoint | `work_buddy/dashboard/service.py` | calls `resolve_consent_request` directly |
  | Gateway in-window poll | `_auto_consent_request` / `_auto_workflow_consent_request` | records the polled response, reloads the durable first-response winner, then either writes ordinary grants or returns an ephemeral per-invocation authorization |

  `finalize_consent_response` is the canonical out-of-band translator. It writes ordinary grants, including bundle members and `workflow_class:<name>` when applicable. For `grant_policy == "per_invocation"`, it records approval metadata but deliberately returns `grant_written: false`; only the gateway's still-active invocation can bind that response to its immediate retry. Any surface that doesn't go through `resolve_consent_request` (Telegram is the notable one) must call `finalize_consent_response` directly. Tests covering ordinary paths live in `tests/unit/test_consent_composable.py`; exact-review and response-race coverage lives in `tests/unit/test_consent_per_invocation.py`.

  ## Agent-session routing for callback_session_id

  `create_consent_request` accepts a `callback_session_id` parameter — the session whose `consent.db` should receive grants when the response lands. **This must be the agent's session, not the MCP server's bootstrap session.** The two are different processes with different `WORK_BUDDY_SESSION_ID` env vars: the bootstrap session is the long-running MCP server process; the agent session is the Claude Code Desktop session that called `wb_run`.

  Both `_auto_consent_request` and `_auto_workflow_consent_request` in `gateway.py` accept an explicit `session_id` parameter and pass it to `create_consent_request`. The `wb_run` dispatch site threads `_agent_sid` (from `_resolve_session(ctx)`); the retry path threads `record.get("originating_session_id")` (the session that originally requested the op). The env-var fallback inside the helpers exists only for direct Python callers that bypass the gateway surface — production code paths pass the session id explicitly.

  If you add a new helper that creates consent notifications, pass `callback_session_id=<agent_session_id>` explicitly. Reading `os.environ.get("WORK_BUDDY_SESSION_ID")` for this purpose is wrong — it returns the bootstrap session, and out-of-band approvals will route grants to a DB the agent's `is_granted` check never queries.

  The registry behind `_resolve_session(ctx)` (gateway `_SESSION_REGISTRY`) keys on the MCP **session object** through a `weakref.WeakKeyDictionary`, never on `id(ctx.session)`. Keying on `id()` would be unsafe: a Python `id()` is a memory address CPython reuses once the object is garbage-collected, so an integer-keyed map that never evicts can resolve a reconnected connection — whose fresh session object happens to land on a freed address — to a *different, already-dead* agent session. That connection's consent requests would then carry the dead session's `callback_session_id`, and out-of-band approvals would write grants to a `consent.db` the live agent never reads. Object-identity keying makes a reused address a distinct key, and the weak reference auto-evicts when the session object dies, so a connection that never registered resolves to `None` and `_require_init` forces a fresh `wb_init` rather than inheriting a stale mapping. (`ServerSession` is weak-referenceable — `BaseSession` provides `__weakref__`.)

  Defense-in-depth: when no agent session id reaches `_auto_consent_request` / `_auto_workflow_consent_request` (a direct Python caller, or an unregistered connection), they call `_warn_consent_session_fallback`, which emits a `logging` warning plus a `CONSENT_SESSION_FALLBACK` audit line — so a bootstrap-session mis-route is greppable instead of silent.

  ## Listing grants in the agent's session

  `consent_list` (capability `op.wb.consent_list`) accepts `agent_session_id` and routes the SQLite read to that session's `consent.db`. The gateway auto-injects the caller's session id when dispatching the capability via `wb_run`. Direct Python callers (tests, scripts) can pass it explicitly; passing `None` falls back to the cache's default-path resolution (which uses whatever session was first connected — typically the process's bootstrap session, which is rarely what a tool caller wants to see).

  The underlying `ConsentCache.list_all(*, session_id=None)` mirrors the routing pattern used by `grant`, `revoke`, and `_is_granted_in_session`: explicit session id when set, default-path fallback when not.
---

Some `work_buddy` functions are protected by a `@requires_consent` decorator. **The gateway handles consent transparently** — when you call `wb_run` on a consent-gated capability, the gateway automatically requests consent from the user, waits for approval, and retries the operation. You do not need to manually orchestrate consent.

## How it works

1. **Pre-flight check** — capabilities declare `consent_operations` listing operations they may trigger. The gateway checks all upfront and bundles missing grants into ONE notification. This list enriches the notification body (UX) but is not required for correctness.
2. **Consent context** — when a consent-gated function executes, it establishes a thread-local context. Nested `@requires_consent` calls (e.g., `toggle_task` → `bridge.write_file`) pass through automatically; the outer consent subsumes inner ones. No manual bookkeeping or `*_raw` function variants needed.
3. **Fallback** — if a `ConsentRequired` fires at runtime (unanticipated gate not covered by pre-flight or context), the gateway auto-requests and retries (max 2 retries).
4. **You see**: success (normal result), denied (`{status: "denied"}`), or timeout (`{status: "timeout", operation_id: "op_xxx"}`).
5. **On timeout** — the request stays pending on all surfaces. Once the user approves, retry with `mcp__work-buddy__wb_run("retry", {"operation_id": "op_xxx"})` to replay the original call without re-sending parameters. **For Obsidian-bridge operations, use `obsidian_retry` instead** — it accepts the same `{"operation_id": "op_xxx"}` shape and adds bridge health-checks between attempts. The gateway's timeout return tells you which to use.

## What not to do

**The gateway handles consent for `wb_run` operations automatically.** There is no agent-facing path that requires manual consent orchestration. Sidecar operations that need consent (e.g., `agent_spawn`) check via internal Python helpers (`work_buddy/sidecar/dispatch/executor.py:_check_agent_spawn_consent`), not through `wb_run`.

**Do NOT use `AskUserQuestion` for consent.** The notification system is the canonical consent surface — it reaches the user on their phone, in Obsidian, and on the dashboard. `AskUserQuestion` only works when the user is actively watching the terminal.

**Agents cannot self-grant consent.** The Python functions `consent.grant_consent`, `consent.revoke_consent`, and `consent.resolve_consent_request` are internal — they are called by the sidecar router (Obsidian out-of-band path), Telegram and dashboard handlers, and the gateway's own auto-consent flow. They are not exposed as agent-callable capabilities. The only way an agent gets consent is the user approving on a surface; the gateway handles the rest.

**Interpreting Python-side grant/revoke return values.** `consent.grant_consent`, `consent.grant_consent_batch`, and `consent.revoke_consent` are **side-effect functions that return `None`** — any call routed through `wb_run` records `result_summary: null` in the activity ledger.  A `null` ledger entry is the *expected* return shape; it does NOT signal that the write failed. To verify a grant actually landed, call `list_consents()` (with `agent_session_id` for cross-process callers) — that is the canonical success check.

The cross-process subtlety: if a writer calls `grant_consent` without `session_id`, the grant lands in the cache's default session DB (typically the process's bootstrap session) and is invisible to a reader checking under a different `agent_session_id`.  Same-process writer/reader pairs are safe because both resolve to the same default; cross-process writers (e.g. an MCP gateway dispatch on behalf of an agent in a different session) MUST pass the explicit `session_id`.  Tests at `tests/unit/test_grant_consent_session_routing.py` characterise both shapes.

## Grant scope and lifetime

Grants are stored session-scoped in a SQLite database at `data/agents/<session>/consent.db`. New sessions start with a clean slate — no grants carry over between user sessions. "Always" means "always within this session" (max 24h TTL).

### Ordinary versus per-invocation consent

Ordinary, cacheable consent keeps the four standard choices: **Allow once**, **Allow for N min**, **Allow always (this session, 24h)**, and **Deny**. An approval writes the corresponding session-scoped grant to `consent.db`; a timed-out ordinary request stays pending so a later approval can write that grant and authorize a retry.

Operations declared with `grant_policy="per_invocation"` use a stricter exact-review boundary. Their server-composed prompt is bound to an operation and canonical fingerprint, and offers only **Allow once** or **Deny**. Existing individual, workflow-run, workflow-class, legacy, time-window, or `user_initiated()` authority cannot satisfy the gate. Approval writes no entry to `consent.db`. Instead, while the original gateway call is still waiting, it creates one ephemeral authorization for the matching immediate retry. The decorator checks the operation and fingerprint, consumes that authorization before the guarded function begins, and cannot reuse it if the function fails or another call follows.

The notification record is the durable **first-response-wins** authority for both ordinary and per-invocation prompts. After polling, the gateway reloads the stored response and follows that winner even if another surface returned a conflicting answer or the poll returned no answer. A later response cannot replace the recorded winner.

Per-invocation timeout is fail-closed in an additional way: once the gateway returns `status: timeout`, a later approval may be recorded for audit but cannot authorize the timed-out execution, a retry of its operation record, or any future execution, and still writes no reusable grant. The caller must invoke the capability again to obtain a fresh exact-review prompt. This restriction does not change the ordinary pending-request behavior described above.

When the sidecar's retry sweep replays a previously-consented operation, the consent check ALSO consults the originating user-session's grants (looked up by reference to the op record's `originating_session_id`). This means a consented operation that hits PWU and gets queued for retry will not fail with `ConsentRequired` on replay. Revocation in the originating session immediately disables future replays.

**Workflow grants do NOT time-travel through the retry queue.** The originating-session fallback considers individual op-grants only — `workflow_class:*` / `workflow_run:*` / legacy `__workflow_consent__` keys are deliberately skipped on the replay path. The rationale: a workflow grant active when an op was queued may have been revoked, or have a class TTL that long expired by the time the sweep replays the op days later; the user's *temporally-bounded* trust in the workflow does not generalize to a *later* replay. Replays succeed only on individual op grants the user explicitly authorized for that op (or that exist in the current replay-time session).

**Cross-session routing on out-of-band approval.** When a user approves a consent prompt on the Obsidian modal *after* the gateway's in-window poll has already returned `{status: "timeout"}`, the plugin posts a `consent_grant` message to the messaging service. The sidecar's MessagePoller picks it up and routes the grant via `resolve_consent_request`, which looks up the notification's `callback_session_id` and writes grants to **that** session's DB — not the sidecar's. The originating agent's subsequent `obsidian_retry` (or fresh capability call) then sees the grant and proceeds. Without this routing, modal-approved grants would land in the sidecar's bootstrap-session DB where no agent could see them.

**Bundle unbundling.** When the gateway requests consent for a multi-op capability, the notification's `operation` field is a label of the form `bundle:<capability_name>` and the underlying ops live in `consent_meta.context.operations`. `resolve_consent_request` writes grants for **each underlying op individually** in addition to the bundle label, because the `@requires_consent` decorators check the individual operation names (e.g. `tasks.create_task`, `obsidian.write_file`), not the bundle. The bundle label survives as audit metadata.

## Composable workflow consent

Starting a workflow may prompt the user once to authorize the workflow's component operations. Two grant levels live in the session's `consent.db`:

- `workflow_class:<name>` — the "Allow for 15 min" or "Allow always" key. Set by the gateway's pre-flight prompt when the user approves a non-`once` mode. TTL-bounded (15 min for `temporary`, 24h for `always`). While live, future invocations of the same workflow skip the pre-flight prompt.
- `workflow_run:<name>:<run_id>` — minted by `start_workflow` for every active run. Authorizes the workflow's sub-operations *as constituents of that run*. No TTL; revoked when the run completes (or via cascade when the class grant is explicitly revoked).

The gateway pre-flight prompt fires when ALL of the following hold:

1. The workflow's `workflow_class:<name>` grant is NOT live in this session.
2. The dispatch is NOT inside a `user_initiated()` context.
3. The workflow's declared `consent_operations` include at least one moderate- or high-risk op (low-only workflows auto-bypass under the hybrid migration policy — see below).

User choices in the prompt: **Allow once** (no class grant; only the run grant covers this invocation), **Allow for 15 min** (class grant minted with 15-min TTL), **Allow always (this session, 24h)** (class grant minted with 24-h TTL), or **Deny** (the workflow does not start; `start_workflow` is short-circuited and the operation completes with a `consent denied` error).

### Decorator carry path

The `@requires_consent` check inside a workflow run consults, in order:

1. Individual op grant for `operation` (highest priority).
2. Any live `workflow_run:*` key in this session.
3. Any live `workflow_class:*` key in this session.
4. Legacy `__workflow_consent__` (deprecation-logged once per op per process).

Capabilities tagged with `consent_weight="high"` on their `@requires_consent` decorator (and mirrored on the `Capability.consent_weight` field) BYPASS the workflow-grant carry entirely — they always re-prompt individually, even inside an approved workflow run. This mirrors Cursor's destructive-command carve-out and OpenAI's `isConsequential` flag for GPT Actions. The default `consent_weight` mirrors the declared `risk` value, so the legacy behavior of "workflow blanket carries everything" is preserved for low-risk ops while high-risk ops are properly gated.

### `requires_individual_consent: true` step flag

A workflow step can opt out of the run-grant carry via `requires_individual_consent: true` in the workflow definition. The conductor revokes the run grant before dispatching such a step and re-mints it after the step completes (or after the agent advances the workflow for a reasoning step). This lets a workflow author force a per-step prompt for one specific operation without affecting the rest of the DAG.

### Low-weight workflow auto-bypass

Workflows whose constituent capabilities declare only low-weight `consent_operations` (or none at all) auto-bypass the pre-flight prompt. The gateway audit-logs each bypass as `WORKFLOW_AUTO_BYPASS_LOW_WEIGHT | workflow=<name>` so the `scripts/audit_workflow_consent.py` script can enumerate which workflows ride the bypass and which prompt. The bypass keeps read-only routines like `task-search` frictionless while still prompting for any workflow that touches moderate or high-risk operations.

### Orphan reconciliation

A workflow's `workflow_run:<name>:<run_id>` key lives in the agent's `consent.db` while the run is active. An MCP-server restart wipes `_ACTIVE_RUNS` but leaves the on-disk grant; without cleanup, the orphaned key would silently authorize subsequent calls until something else revoked it. `conductor.reconcile_workflow_consent(session_id)` runs at session registration and revokes any `workflow_run:*` keys whose run_id is not present in `_ACTIVE_RUNS` for that session. Class grants are intentionally left alone — their TTL is the bound. The function also still handles the legacy `__workflow_consent__` key for back-compat (preserves the historical return-shape contract: `{"swept": True}` / `{"swept": False, "reason": "active_run_present"}` / `{"swept": False, "reason": "no_blanket"}`).

### Explicit cascade revoke

`conductor.cascade_revoke_workflow(name, session_id=...)` revokes the `workflow_class:<name>` key AND walks `_ACTIVE_RUNS` to revoke every in-flight run grant for that workflow. Used when the user explicitly withdraws trust mid-run — the ocap-CDT model where revoking the parent class grant propagates to all derived run grants. Without this, withdrawing trust would leave the in-flight runs riding their independent run grants until completion.

### Legacy `__workflow_consent__` (deprecated)

The legacy blanket key remains as a **read-side** fallback: `_is_granted_in_session` honors a live legacy blanket when no individual / `workflow_run` / `workflow_class` grant matches, and emits a single `LEGACY_WORKFLOW_BLANKET_USED` audit entry per operation per process so unconverted call sites stay greppable. The **write-side** uses only the new keys; the conductor and gateway no longer write `__workflow_consent__`. The read fallback is scheduled for removal once the audit log shows no LEGACY_WORKFLOW_BLANKET_USED hits in a release cycle.

## Risk levels

Risk must be one of: `"low"`, `"moderate"`, `"high"` (validated by the `Risk` enum).

## Call-stack-aware risk reduction (`@reduces_risk_for`)

A function decorated with `@reduces_risk_for("some.op", "low")` declares itself a safe invoker of `some.op`. While it is on the call stack, inner `@requires_consent("some.op", ...)` checks auto-pass (for `"low"`) or prompt at the reduced risk (for `"moderate"`). Direct agent calls to the primitive — outside any safe-caller scope — still gate at the original risk.

This is the mechanism that lets read-only capabilities (e.g. `daily_briefing`) internally call `obsidian.eval_js` (registered at `risk=high`) without spamming prompts, while preserving high-risk gating for direct `eval_js` invocations from agents or the local-model tool preset. Declarations are module-level code (not config) and inspectable via `list_risk_reducers()` — adding or expanding one is a reviewed PR, not a runtime grant.

## UI-click bypass (`user_initiated`)

The consent gate exists for **autonomous agent operations** — cron-fired scans, sidecar workflows, LLM-initiated actions where the user isn't watching. UI clicks are the inverse case: **the user already consented by clicking the affordance**. Pre-emptively re-prompting them for the action they explicitly initiated is bureaucratic UX and a known bug pattern.

`work_buddy.consent.user_initiated(operation)` is a context manager for that case. Wrap the critical section of a UI-driven endpoint (Flask handler reached via a button click, slash-command handler, CLI script the user invoked explicitly) and nested `@requires_consent` gates pass through, with audit-log entries (`USER_INITIATED`, `USER_INITIATED_COVERED`) distinguishing UI-driven actions from autonomous ones. The gateway's workflow pre-flight prompt is ALSO suppressed when dispatching inside a `user_initiated()` context.

```python
from work_buddy.consent import user_initiated

@app.post("/api/user_jobs")
def api_user_job_create():
    payload = request.get_json() or {}
    with user_initiated("dashboard.user_job_create"):
        result = create_user_job(payload)
    return jsonify(result)
```

### Canonical wiring: thread-approve clicks

The dashboard's thread-action endpoints (Approve, Confirm, Review-accept, Redirect, etc.) all funnel through `work_buddy/dashboard/service.py:_post_thread_action`, which wraps `engine.transition` in `user_initiated()` for any trigger in the `_THREAD_USER_INITIATED_TRIGGERS` set (`execute`, `confirmed`, `review_accepted`, `provided`, `redirected`, `retry_cleanup`, `accept_cleanup_failure`). The same wiring lives in `work_buddy/threads/group.py:_run_child_accept` for the cluster-umbrella Approve-All cascade — each child's accept-equivalent trigger runs inside `user_initiated("thread.cascade_approve.<trigger>")`.

This is the right shape because thread actions are inferred by the LLM, surfaced on the confirmation card (with risk metadata, rationale, parameters all visible to the user), and only fire when the user clicks Approve. That click IS the consent boundary; the side-effect handler that dispatches the capability runs synchronously inside the transition, so the `user_initiated` context covers the entire downstream chain (action dispatch → @requires_consent decorator → underlying capability).

If you add a new dashboard endpoint that fires a state-entry side effect invoking a `@requires_consent` capability, follow this pattern. If you bypass `_post_thread_action` and call `engine.transition` directly from a Flask handler, you must add the wrapper yourself or the user will see a `ConsentRequired` re-prompt after they've already clicked Approve.

**Use sparingly elsewhere.** Outside the thread-approve path, the right callers are: dashboard POST handlers reachable only via a button click; CLI scripts the user invoked explicitly; slash-command handlers. Do NOT use this in code an agent can reach without a user click — that defeats the consent model. Reentrant; thread-local; restores depth on exception.
