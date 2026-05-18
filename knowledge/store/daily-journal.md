---
name: Daily Journal
kind: system
description: Daily journal lifecycle — line-range segmentation, per-thread tag/summary manifest, clustering, routing, rewrite, and update synthesis.
summary: 'Workflows that process the daily journal file: segmenting Running Notes into sections, routing captured items, handling the unprocessed backlog, and synthesizing updates. Distinct from `journal/` (which currently holds the journal-update directions unit) — the `daily-journal/` namespace groups the multi-step workflows, `journal/` the plain directions. A future consolidation pass may merge these; for now they coexist.'
tags:
- daily-journal
- journal
- notes
- segmentation
- backlog
aliases:
- daily journal
- journal lifecycle
- journal processing
---

Workflows that process the daily journal file: segmenting Running Notes into thread groups, building a per-thread manifest, clustering by topic, routing items to destinations, rewriting the section, and synthesizing daily updates. Distinct from `journal/` (which holds the journal-update directions and capability units) — the `daily-journal/` namespace groups the multi-step workflows.

The segmentation substrate is **line-range** (the LLM partitions numbered input lines into groups; ids and metadata are computed on our side). The earlier tagged-text segmentation path was retired — see `architecture/llm-runner` for the segmenter's tier escalation and `work_buddy.triage.adapters.journal._segment_with_escalation` for the implementation.
