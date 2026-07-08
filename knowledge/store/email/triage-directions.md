---
name: Email triage directions
kind: directions
description: Run one source-pipeline pass over recent email; spawns Threads carrying the agent's per-cluster proposals.
trigger: When the user invokes /wb-email-triage or asks to triage / scan / sweep their email inbox
command: wb-email-triage
workflow: email/email-triage
capabilities:
- run_source_pipeline
- email_health
- email_accounts
- email_get
- email_display
- email_close
- email_create_tasks
- email_create_umbrella_task
- email_record_into_task
tags:
- email
- triage
- thunderbird
- inbox
- directions
aliases:
- triage email
- email triage
- scan inbox
- run email triage
- check unread email
parents:
- email
- email
dev_notes: Email triage routes through the unified source pipeline and surfaces on Threads (`run_source_pipeline` with `source='email_triage'`); there is no separate `email_triage_run` capability. The hourly cron at `sidecar_jobs/email-triage-scan.md` invokes the same path. Per-cluster actions resolve through the email action library declared on `EmailTriagePipeline.action_library`.
---

## Goal

Run a single email-triage pass — fetch recent messages from the configured email provider (Thunderbird bridge by default), cluster them, refine cluster boundaries with a local-LLM call, and spawn an umbrella Thread + group sub-threads carrying per-cluster action proposals. The user reviews and approves on the dashboard's Threads tab.

## Steps

1. **Probe.** Call `email_health` first. If it returns `ok: false`, surface the `error_kind` to the user — most common cause is the bridge being down or no accounts being allowed in the extension's options.

2. **Account-allow check.** If `email_health` is OK but `accessible_accounts == 0`, tell the user to open Thunderbird → Add-ons → Work Buddy Bridge options and tick at least one account. Do not silently produce zero candidates.

3. **Run.** Two equivalent forms — pick whichever is convenient:
   - **Workflow form:** `wb_run('email-triage')` — invokes the `email/email-triage` workflow.
   - **Direct form:** `wb_run('run_source_pipeline', {'source': 'email_triage', 'days_back': 2, 'max_messages': 50, 'unread_only': True})`.

   The pipeline runs collect → annotate → precluster → refine → spawn end-to-end and returns the umbrella thread id + child group thread ids + per-cluster action proposals.

4. **Report.** The returned dict has keys `pipeline_name`, `umbrella_id`, `child_thread_ids`, `item_count`, `cluster_count`, `action_proposals`. Prefer the umbrella_id + sender/cluster counts over dumping the proposals dict.

## What happens on the dashboard

After the pipeline finishes, the umbrella appears on the Threads tab with N group sub-threads as children. Each child carries:
- The cluster's emails as ContextItems (drag-droppable between siblings).
- The agent's proposed action from the email action library: `email_close` (advisory dismiss), `email_create_tasks` (one task per email), `email_create_umbrella_task` (one task for the cluster), or `email_record_into_task` (file the cluster as a context section on an existing task's linked note).

The user picks per-group actions via the column-header action chip; approving the umbrella runs each child's chosen action through the standard FSM dispatch.

## What you don't do

- **Don't compose / reply / forward / send.** Not exposed in v1 of the Thunderbird bridge. Tell the user to open the message in Thunderbird (`email_display`) and reply there.
- **Don't bypass the pipeline substrate.** If the user says "check my email" without specifying triage, ask whether they want a manual scan (one-shot via the workflow) or recurring triage (the hourly cron under `sidecar_jobs/email-triage-scan.md` — disabled by default; enable with the steps in the `email/` integration unit).

## Failure modes

| `error_kind`                  | Meaning                                                   | Action |
|------------------------------|-----------------------------------------------------------|--------|
| `email_provider_disabled`    | `email.enabled: false` in config                          | Tell user; suggest config flip |
| `email_bridge_unreachable`   | Thunderbird closed or extension not running               | User must open TB / install ext |
| `email_bridge_unauthorized`  | Stale connection-file token (race with TB restart)        | Restart TB to refresh token |
| `email_message_not_found`    | Follow-up call for a moved/deleted message                | Surface, don't retry |
| `email_provider_error`       | Generic 4xx/5xx from the bridge                           | Surface verbatim |
