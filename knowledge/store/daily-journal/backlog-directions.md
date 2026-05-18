---
name: Journal Backlog Processing Directions
kind: directions
description: How to run Running Notes backlog pipeline — cluster review, routing proposals, rewrite presentation
summary: Default to today's journal unless a date is specified. Requires active user participation. Review clusters before routing (bad grouping cascades). Never auto-route without confirmation.
trigger: user wants to process or clean up their Running Notes backlog
command: wb-journal-backlog
workflow: daily-journal/process-backlog
capabilities:
- journal/running_notes
- journal/vault_write_at_location
tags:
- journal
- backlog
- routing
- clustering
- directions
aliases:
- process backlog
- running notes backlog
- segment and route notes
- clean up running notes
parents:
- daily-journal
- daily-journal
---

Default to today's journal unless a date is specified. Requires active user participation — review clusters before routing (bad grouping cascades), and confirm the rewrite preview before granting consent for the file write. Never auto-route without confirmation.

## What the workflow does

1. **Extract** the Running Notes section from the journal file.
2. **Segment** via line-range partition (`work_buddy.triage.adapters.journal._segment_with_escalation`). The LLM emits line-number groups; ids are generated on our side. Tier escalation (LOCAL_FAST → FRONTIER_FAST by default) handles validation failures.
3. **Manifest**: `build_thread_manifest` calls FRONTIER_FAST per thread for `{tags, summary}`. Per-thread errors don't abort the run.
4. **Cluster**: `linearize_threads` seriates by Jaccard tag similarity (break_threshold=0.15). The cluster review markdown is presented to the user.
5. **Review**: user marks MERGE / SPLIT / TAG decisions per cluster.
6. **Route**: user-confirmed routing decisions go to `execute_routing_plan` (consent-gated). Destinations: task list, consideration file, existing note (append), or delete/skip/split.
7. **Rewrite**: `rewrite_running_notes` produces a new Running Notes section with processed lines stripped (consent-gated; refuses to write if file changed on disk since the rewrite was prepared).

## Scheduled scan dedup

The hourly `journal-triage-scan` cron invokes `run_source_pipeline(source="journal_backlog")`. The runner short-circuits the spawn when an open umbrella for the same `journal_date` already exists (key: `journal_backlog:<journal_date>`, stored in the umbrella's `inciting_event_summary.dedup_key`). One umbrella per day until it reaches a terminal state. Items captured into the running notes after the umbrella was spawned are NOT auto-routed into it — that requires a manual re-run after the existing umbrella is resolved (done / dismissed), at which point the next scan produces a fresh umbrella.

## Operator notes

- Multi-thread overlap (a line in two clusters) is handled conservatively in the rewrite: the line is kept if any of its memberships is a keep-decision. Silent data loss is the worse failure mode.
- Split actions require a `rewrite_map[id]` entry naming what to put in place of the original lines (string = replacement text, None = drop).
- The pipeline is bounded by ``triage.segment.tier_chain`` for the segmentation step — add a tier (e.g. ``frontier_balanced``) when local-only consistently fails validation.
