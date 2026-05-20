---
name: Consent System
kind: directions
description: How consent-gated operations work — auto-request in gateway, pre-flight bundling, session scope, risk levels
summary: Gateway handles consent transparently for wb_run ops. Pre-flight, context-nesting, and retry-on-timeout all automatic. All grants session-scoped in consent.db.
trigger: agent calls a capability that touches @requires_consent functions (handled transparently by the gateway)
capabilities:
- consent_request
- consent_request_resolve
- consent_grant
- consent_revoke
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
  **Implied consent for thread approvals.** `work_buddy/dashboard/service.py:_post_thread_action` and `work_buddy/threads/group.py:_run_child_accept` wrap `engine.transition` in `user_initiated()` for user-click triggers. The trigger allowlist `_THREAD_USER_INITIATED_TRIGGERS` lives at the top of service.py. To add a new user-click trigger to the bypass, add it to that set; non-listed triggers (e.g. agent-initiated `begin_inference`, `inference_done`) take the bare `engine.transition` path. Tests live in `tests/unit/test_consent_user_initiated.py` (8 cases covering reentrancy, exception cleanup, USER_INITIATED audit emission). `_consent_ctx` is `threading.local`-backed so cross-thread leaks aren't a concern; the side-effect dispatcher runs side effects in the same thread that called `engine.transition`, so the context propagates naturally through the transition machinery.

  **Failure mode the wrapper guards against.** Without `user_initiated`, clicking Approve on a `task_create` action chip fires `ConsentRequired: tasks.create_task (moderate)` and dumps the thread into `AWAITING_REDIRECT` — the user is prompted twice for the same decision (once via the action card showing parameters and risk metadata, again via a fresh consent notification). The action card itself IS the consent surface; the wrapper makes the click count.

  **If you add a new dashboard endpoint** that fires a state-entry side effect invoking a `@requires_consent` capability and bypasses `_post_thread_action`, you must add the wrapper yourself. The capability dispatcher running inside `EXECUTING`'s side-effect handler runs synchronously in the same thread as `engine.transition`, so a `user_initiated` context wrapping the transition covers the entire downstream chain.

  ## Workflow-consent orphan reconciliation

  A workflow grants a `__workflow_consent__` blanket into the agent's `consent.db` (on disk, up to a 3h TTL) and pins the run's DAG in `conductor._ACTIVE_RUNS` (in memory). The two have mismatched lifetimes: an MCP-server restart wipes `_ACTIVE_RUNS` but leaves the blanket live, so it would silently authorize every consent-gated call until TTL expiry.

  `conductor.reconcile_workflow_consent(session_id)` re-couples them. It runs from `gateway.py:_register_session` — the single chokepoint all three session-registration paths funnel through (`wb_init` tool, `wb_run` wb_init branch, header-based auto-init), each also a post-restart reconnect point. If the session's DB holds an active blanket but no `_ACTIVE_RUNS` entry has a matching `agent_session_id`, the blanket is orphaned and revoked. A genuinely in-flight workflow keeps its DAG in `_ACTIVE_RUNS` (entries leave only on completion), so a non-restart re-registration finds the run and leaves the blanket intact. `is_workflow_consent_active(session_id=...)` is the per-session check — it delegates to `_is_granted_in_session`, skipping the originating-session fallback so a blanket in another session's DB is not counted. The function is guarded internally and never raises, so it cannot break session registration. Tests: `tests/test_workflow_consent.py`.

  ## Modal-fallback message routing

  The Obsidian plugin's `ObsidianModal.dispatchConsentGrant` (handlers.ts) posts a `consent_grant` message to the messaging service whenever the user clicks an Allow option. Body shape: `{operation, mode, ttl_minutes, notification_id}`. The `notification_id` is the load-bearing field — without it the sidecar cannot resolve the originating agent's session and falls back to writing the grant in its own session DB (a legacy behavior preserved with a WARN log so an out-of-sync plugin doesn't strand the user, but the routing is broken until the plugin is rebuilt + reloaded).

  The sidecar router's `MessagePoller._handle_message` special-cases `subject == "consent_grant"` before the generic capability-dispatch path. `_handle_consent_grant_message(body)` parses the body, calls `resolve_consent_request(notification_id, approved=True, mode=mode, ttl_minutes=ttl_minutes)`, and returns the dispatch status. The `resolve_consent_request` path is the canonical site for both the cross-session routing (it reads `notification.callback_session_id` and threads it through `grant_consent(..., session_id=...)`) and the bundle unbundling (it reads `consent_meta.context.operations` and calls `grant_consent_batch`).

  Already-resolved notifications (the gateway's in-window poll won the race) raise `ValueError` from `respond_to_notification`; the handler catches it and returns a no-op success. This keeps the modal-click → message-dispatch path idempotent without depending on the plugin to know whether the gateway already grabbed the response.

  **`grant_consent` and `grant_consent_batch` accept `session_id` as a keyword.** Plumbs through to `ConsentCache.grant(..., session_id=...)` — the same mechanism workflow blanket grants use to write to a different session's DB than the calling process. The audit log includes the truncated session id in the `GRANTED` details column so cross-session writes are auditable post-hoc.

  **The bundle key (`bundle:<capability>`) is granted alongside individual ops.** It serves as audit-log readability — `GRANTED | bundle:task_create | once` is more informative than three separate GRANTED rows for the underlying ops. No decorator checks the bundle key, so leaving it in the DB doesn't satisfy any gate by accident.
---

Some `work_buddy` functions are protected by a `@requires_consent` decorator. **The gateway handles consent transparently** — when you call `wb_run` on a consent-gated capability, the gateway automatically requests consent from the user, waits for approval, and retries the operation. You do not need to manually orchestrate consent.

## How it works

1. **Pre-flight check** — capabilities declare `consent_operations` listing operations they may trigger. The gateway checks all upfront and bundles missing grants into ONE notification. This list enriches the notification body (UX) but is not required for correctness.
2. **Consent context** — when a consent-gated function executes, it establishes a thread-local context. Nested `@requires_consent` calls (e.g., `toggle_task` → `bridge.write_file`) pass through automatically; the outer consent subsumes inner ones. No manual bookkeeping or `*_raw` function variants needed.
3. **Fallback** — if a `ConsentRequired` fires at runtime (unanticipated gate not covered by pre-flight or context), the gateway auto-requests and retries (max 2 retries).
4. **You see**: success (normal result), denied (`{status: "denied"}`), or timeout (`{status: "timeout", operation_id: "op_xxx"}`).
5. **On timeout** — the request stays pending on all surfaces. Once the user approves, retry with `mcp__work-buddy__wb_run("retry", {"operation_id": "op_xxx"})` to replay the original call without re-sending parameters. **For Obsidian-bridge operations, use `obsidian_retry` instead** — it accepts the same `{"operation_id": "op_xxx"}` shape and adds bridge health-checks between attempts. The gateway's timeout return tells you which to use.

## What not to do

**Do NOT manually call `consent_request`** for `wb_run` operations — the gateway does it for you. You still need manual `consent_request` for sidecar operations not routed through `wb_run` (e.g., `agent_spawn` consent) or custom flows.

**Do NOT use `AskUserQuestion` for consent.** The notification system is the canonical consent surface — it reaches the user on their phone, in Obsidian, and on the dashboard. `AskUserQuestion` only works when the user is actively watching the terminal.

**Do NOT use `consent_grant` to bypass consent.** `consent_grant` is a low-level primitive for deferred resolution (e.g., user approved on Telegram after the poll timed out). Agents must NEVER self-grant consent.

## Grant scope and lifetime

Grants are stored session-scoped in a SQLite database at `data/agents/<session>/consent.db`. New sessions start with a clean slate — no grants carry over between user sessions. "Always" means "always within this session" (max 24h TTL).

When the sidecar's retry sweep replays a previously-consented operation, the consent check ALSO consults the originating user-session's grants (looked up by reference to the op record's `originating_session_id`). This means a consented operation that hits PWU and gets queued for retry will not fail with `ConsentRequired` on replay. Revocation in the originating session immediately disables future replays.

**Cross-session routing on out-of-band approval.** When a user approves a consent prompt on the Obsidian modal *after* the gateway's in-window poll has already returned `{status: "timeout"}`, the plugin posts a `consent_grant` message to the messaging service. The sidecar's MessagePoller picks it up and routes the grant via `resolve_consent_request`, which looks up the notification's `callback_session_id` and writes grants to **that** session's DB — not the sidecar's. The originating agent's subsequent `obsidian_retry` (or fresh capability call) then sees the grant and proceeds. Without this routing, modal-approved grants would land in the sidecar's bootstrap-session DB where no agent could see them.

**Bundle unbundling.** When the gateway requests consent for a multi-op capability, the notification's `operation` field is a label of the form `bundle:<capability_name>` and the underlying ops live in `consent_meta.context.operations`. `resolve_consent_request` writes grants for **each underlying op individually** in addition to the bundle label, because the `@requires_consent` decorators check the individual operation names (e.g. `tasks.create_task`, `obsidian.write_file`), not the bundle. The bundle label survives as audit metadata.

**Workflow-level blanket consent:** Starting a workflow grants blanket consent for all its steps, stored as a `__workflow_consent__` grant in the agent's `consent.db`. The blanket is revoked when the workflow completes; a step can opt out via `requires_individual_consent: true` in the workflow definition, which temporarily suspends the blanket and requires per-step consent; and it is reconciled away on session re-registration when no matching active run exists — the case where an MCP-server restart orphaned a mid-flight workflow (the in-memory run map is wiped, but the on-disk blanket would otherwise survive its TTL and silently authorize calls). The 3-hour default TTL is only a backstop. Agents don't need to manage any of this — the conductor handles it automatically (both auto-run and main-execution steps).

## Risk levels

Risk must be one of: `"low"`, `"moderate"`, `"high"` (validated by the `Risk` enum).

## Call-stack-aware risk reduction (`@reduces_risk_for`)

A function decorated with `@reduces_risk_for("some.op", "low")` declares itself a safe invoker of `some.op`. While it is on the call stack, inner `@requires_consent("some.op", ...)` checks auto-pass (for `"low"`) or prompt at the reduced risk (for `"moderate"`). Direct agent calls to the primitive — outside any safe-caller scope — still gate at the original risk.

This is the mechanism that lets read-only capabilities (e.g. `daily_briefing`) internally call `obsidian.eval_js` (registered at `risk=high`) without spamming prompts, while preserving high-risk gating for direct `eval_js` invocations from agents or the local-model tool preset. Declarations are module-level code (not config) and inspectable via `list_risk_reducers()` — adding or expanding one is a reviewed PR, not a runtime grant.

## UI-click bypass (`user_initiated`)

The consent gate exists for **autonomous agent operations** — cron-fired scans, sidecar workflows, LLM-initiated actions where the user isn't watching. UI clicks are the inverse case: **the user already consented by clicking the affordance**. Pre-emptively re-prompting them for the action they explicitly initiated is bureaucratic UX and a known bug pattern.

`work_buddy.consent.user_initiated(operation)` is a context manager for that case. Wrap the critical section of a UI-driven endpoint (Flask handler reached via a button click, slash-command handler, CLI script the user invoked explicitly) and nested `@requires_consent` gates pass through, with audit-log entries (`USER_INITIATED`, `USER_INITIATED_COVERED`) distinguishing UI-driven actions from autonomous ones.

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
