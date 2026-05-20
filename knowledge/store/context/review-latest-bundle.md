---
name: Review Latest Bundle
kind: workflow
description: Read the most recent existing context bundle without re-collecting. Faster than collect-and-orient when a recent bundle already exists.
workflow_name: review-latest-bundle
execution: main
steps:
- id: find-bundle
  name: Find the latest context bundle
  step_type: code
  depends_on: []
  invokes: []
- id: check-freshness
  name: Check bundle freshness and warn if stale
  step_type: code
  depends_on:
  - find-bundle
  invokes: []
- id: read-and-synthesize
  name: Read bundle files and synthesize orientation
  step_type: reasoning
  depends_on:
  - check-freshness
  invokes: []
tags:
- context
- review
- latest
- bundle
parents:
- context
---

## find-bundle

Find the most recent context bundle in your session directory:

```
ls -t agents/<your-session>/context/ | head -1
```

This returns the latest timestamped bundle directory (e.g., `2026-04-02T18-35-49`). If no bundles exist, tell the user and suggest running `/wb-context-collect` instead.

## check-freshness

Read `agents/<your-session>/context/<latest-timestamp>/bundle_meta.json` for the `collected_at` timestamp. If older than 24 hours, warn:

"The latest bundle is from [time]. It may be stale. Want me to collect a fresh one?"

## read-and-synthesize

Agentic step. Read the bundle context files and synthesize an orientation. Behavioral instructions (synthesis rules, priority order, flags, contract cross-reference, output format) are in the /wb-context-review slash command.
