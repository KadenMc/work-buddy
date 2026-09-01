---
name: Retired Journal Backlog Processing
kind: concept
description: Historical description of the retired Markdown Running Notes backlog pipeline.
summary: The file segmentation, clustering, routing, and section-rewrite workflow is retired. Native Running Notes are edited, routed, or tombstoned through the React Journal and domain APIs.
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
---

The `/wb-journal-backlog` launcher, workflow declaration, and hourly schedule
are retired. Do not run the legacy source pipeline or recommend an Obsidian
section rewrite. Native Running Notes have stable identities, revisions,
Sources provenance, routing state, and tombstones in the Journal authority.

## Historical workflow

1. **Extract** the Running Notes section from the journal file.
2. **Segment** via line-range partition (`work_buddy.triage.adapters.journal._segment_with_escalation`). The LLM emits line-number groups; ids are generated on our side. Tier escalation (LOCAL_FAST → FRONTIER_FAST by default) handles validation failures.
3. **Manifest**: `build_thread_manifest` calls FRONTIER_FAST per thread for `{tags, summary}`. Per-thread errors don't abort the run.
4. **Cluster**: `linearize_threads` seriates by Jaccard tag similarity (break_threshold=0.15). The cluster review markdown is presented to the user.
5. **Review**: user marks MERGE / SPLIT / TAG decisions per cluster.
6. **Route**: user-confirmed routing decisions go to `execute_routing_plan` (consent-gated). Destinations: task list, consideration file, existing note (append), or delete/skip/split.
7. **Rewrite**: `rewrite_running_notes` produces a new Running Notes section with processed lines stripped (consent-gated; refuses to write if file changed on disk since the rewrite was prepared).

## Historical scheduled scan

The `journal-triage-scan` system job is disabled. The old date-keyed dedup
behavior remains documented only so archived Threads can be interpreted.

## Archive notes

- Multi-thread overlap (a line in two clusters) is handled conservatively in the rewrite: the line is kept if any of its memberships is a keep-decision. Silent data loss is the worse failure mode.
- Split actions require a `rewrite_map[id]` entry naming what to put in place of the original lines (string = replacement text, None = drop).
- The pipeline is bounded by ``triage.segment.tier_chain`` for the segmentation step — add a tier (e.g. ``frontier_balanced``) when local-only consistently fails validation.
