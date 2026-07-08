---
name: Live Testing Directions
kind: directions
description: How to drive a live end-to-end test of an in-progress code change — distinct from unit tests; verifies wiring across MCP server, sidecar, and surfaces with the user in the loop.
trigger: user invokes /wb-dev-live-testing or asks to verify a change live
command: wb-dev-live-testing
tags:
- allow-transient-labels
- dev
- live
- testing
- verification
- directions
aliases:
- live test
- end-to-end
- verify in MCP
- exercise the surface
parents:
- dev
---

When the user invokes `/wb-dev-live-testing`, drive an end-to-end live test of an in-progress code change. Distinct from automated testing (`pytest`) — this is real MCP / sidecar / surfaces actually exercising the new code path with the user in the loop where their judgment is needed.

Live testing exists because some changes only manifest correctly in a running multi-process system: anything touching FastMCP tool registration, the gateway's request handlers, surface dispatchers, sidecar message routing, or session-scoped storage. Unit tests catch logic bugs; live tests catch wiring bugs.

## Two-process model — what restart picks up what

work-buddy spans two long-running processes that load Python code at different times:

- **Sidecar** — restarts when the user does a sidecar-only reset. Re-reads code at startup. Hosts cron jobs, the retry queue, the dashboard, the messaging service, the sidecar MCP gateway.
- **Claude Code Desktop's MCP server** — restarts only on **Ctrl+R** in the Desktop app. This is the process that handles `wb_run` calls from the agent. Its `wb_run` and `wb_search` tool functions are frozen function references held by FastMCP at startup. `reload_capability_data` refreshes the registry those functions *read* — so declaration / workflow / param-schema changes go live without a restart — but it cannot replace the frozen function objects themselves, so changing their code needs a Ctrl+R.

If you change code inside `wb_run` / `wb_search` directly (e.g. the gateway's workflow pre-flight branch), only a Ctrl+R picks it up. If you change code that's *lazy-imported* by those functions (e.g. the conductor, the consent layer, capability callables), that's still code — a Ctrl+R (or a sidecar reset for sidecar-hosted code) is the reliable way to load it. `reload_capability_data` only refreshes *data* (declarations, workflows, param schemas), not Python.

Before driving any live test: confirm with the user that they have restarted whichever process owns the changed code. The user prompt 'I reset the sidecar/MCP!' typically means a sidecar reset; ask whether they also did Ctrl+R if the change touches gateway entry-point functions.

## Protocol for designing the test

A good live test:

1. **Has one clear assertion** — what behavior, observably, distinguishes the new code from the old.
2. **Touches the actual user-facing surface** the change affects (Telegram, Obsidian modal, dashboard, MCP tool response shape).
3. **Specifies who does what** at each step — the agent or the user — explicitly.
4. **Uses an operation that has real intent**, when possible. A test that doubles as work the user wanted done anyway is self-justifying and avoids 'why are we creating this test task' friction.

When the change is in the consent / notification layer, the **handoff task creation** is often the perfect test driver: it requires moderate-risk consent (`tasks.create_task`, `obsidian.write_file`), it's a real piece of work the user wants saved, and it exercises the full consent prompt → response → grant → execution flow.

## When the test is verifying a bug fix

A live test can have one of two motivations: **validate new behavior** (a feature being added) or **verify a bug fix** (something that was broken now works). These differ in what counts as success, and confusing them is how fix verifications quietly turn into feature smoke-tests that pass for the wrong reason.

This subsection applies *only* when the test exists because a bug was diagnosed and a fix was applied. If the test is for a green-field feature, skip ahead.

A fix verification needs three things on top of the standard protocol:

1. **A reproduction of the broken behavior** — captured as a one-sentence assertion of what *would have happened* without the fix. Make it concrete and falsifiable: 'Without the fix, calling `task_create` while grants exist in the agent DB would still raise `ConsentRequired` because the decorator reads the bootstrap session DB'. Not 'consent didn't work right'.

2. **A delta assertion** — what observation, specifically, distinguishes the fixed code from the broken code. The delta should be visible at the surface the test exercises. Examples:
   - 'Same operation now succeeds where it previously raised ConsentRequired.'
   - 'The duplicate notification that fired pre-fix no longer fires.'
   - 'Audit log line carries `via=originating_session` where it carried `via=none` pre-fix.'

3. **Honest framing of confidence** — a green test result is necessary but not sufficient to declare the fix correct. State explicitly what the test did NOT cover (other code paths, race conditions, edge cases) so the user can decide whether more verification is wanted.

If the broken state is easily reproducible (e.g. by NOT restarting the MCP server to pick up the fix, or by reverting one file), running the test once against the broken state and once against the fixed state is the gold standard. It rules out 'the test passed for a reason unrelated to the fix.' This is heavyweight, though — only worth doing when the fix is subtle or the failure mode was hard to diagnose.

If the broken state is not easily reproducible, lean harder on the delta assertion: state in advance what behavior would prove the fix didn't actually work, and watch for that. A test that 'passes' but can't articulate what failure would look like isn't proving anything.

## The standard four-phase live test

Phase 1. **Pre-check (agent)**
- Call any state-inspection capability relevant to the change (e.g. `consent_list`, `wb_status`, `agent_docs`). Verify the starting state is what the test assumes.

Phase 2. **Trigger (agent)**
- Invoke the operation under test. If it blocks waiting for a user response (consent prompt, request_send), the agent's call blocks too — make this explicit to the user: 'I'm going to call X. It will block for up to 90 seconds waiting for you. Either approve / answer on a surface, OR deliberately wait > 90 seconds so we can verify the timeout path.'

Phase 3. **User action (user)**
- Tell the user precisely what to do (which button to click, which mode to choose) AND what to observe on adjacent surfaces (does the other surface dismiss? does the dashboard update? does the audit log fire?).
- For consent flows, the choice of mode matters: 'Allow once' tests the run-grant-only path; 'Allow for 15 min' tests the class-grant carry; 'Allow always' tests the long-TTL class grant; 'Deny' tests the rejection path.

Phase 4. **Verify (agent)**
- Re-check state (re-call the same inspection capability from Phase 1) AND check the audit log for the expected events. Relay the exact JSON / line content back to the user — never paraphrase: 'the grant landed' is not as useful as 'consent_list returned `{workflow_class:task-new: {mode: temporary, expires_at: ...}}`'.
- If the test had a follow-up assertion (e.g. 're-running the operation should now skip the prompt'), execute that too.

## Reporting

After each phase, report back to the user in a tight format:

```
Phase 2: triggered task_create — blocking for up to 90s on your response
Phase 3 (you): wait > 90s, then approve on Telegram with "Allow 5 min"
Phase 4: consent_list shows tasks.create_task | mode=temporary | expires_at=17:54Z. Audit log: REQUEST_APPROVED, GRANTED, EXECUTED.
```

After the full test:

- One-line verdict (`all checks passed` / `phase N failed: $reason`).
- Cleanup: cancel any in-flight workflow runs, revoke any time-limited grants the test minted unless they're useful to leave for follow-up testing.

## What NOT to do

- **Don't skip the pre-check.** It catches stale state that would confuse the verification step.
- **Don't paraphrase the user's observation.** If they say 'the modal disappeared', record that verbatim, don't translate to 'dismiss worked'. The translation hides ambiguity.
- **Don't test multiple changes in one live run.** Each test verifies one assertion. If two changes need verification, run two tests; the cost is small and the diagnosis on failure is sharper.
- **Don't use synthetic operations when a real one fits.** A test that creates a real task the user wanted saved is better than one that creates a `test_xxx` task they have to clean up.
- **Don't manually mint grants to work around bugs DURING the test.** If the test reveals a bug, that's the test working. Report the bug; don't paper over it. (Outside the test, when you're trying to unblock the user, manual grants are fine — just don't conflate the two.)

## Example: composable workflow consent live test

The exact protocol that validated the workflow-consent pre-flight prompt and the Telegram out-of-band grant-writing:

1. **Pre-check**: `consent_list` returns `{}`.
2. **Trigger**: `wb_run("task-new")` (a moderate-risk workflow).
3. **User**: ignore prompt for > 90s, then approve on Telegram with 'Allow for 15 min'.
4. **Verify**:
   - Agent receives `{status: "timeout", request_id, operation_id}` after 90s.
   - After user's Telegram tap: Obsidian modal disappears (visual confirmation).
   - `consent_list` shows `workflow_class:task-new` with `mode: temporary` and `expires_at: ~15 min`.
   - Audit log shows `REQUEST_APPROVED | workflow:task-new | mode=temporary`.
   - Follow-up: `wb_run("task-new")` again returns the first workflow step immediately (no prompt — class grant carried it).
5. **Cleanup**: cancel the second workflow run; revoke `workflow_class:task-new` to leave a clean slate.

This is a single end-to-end run that simultaneously verifies: the pre-flight prompt fires, the timeout payload is correct, out-of-band Telegram approval triggers the grant write (via `finalize_consent_response`), sibling-surface dismissal works, the class grant is queryable, and the re-run silence promise is honored.
