---
name: Context Block
kind: capability
description: Collect and render native context sources plus explicitly enabled compatibility sources. Obsidian, Obsidian wellness/tasks, Day Planner, and Datacore sources are skipped before cache or collector access when Obsidian is opted out; filesystem Vault and provider-neutral Calendar remain independent. Supports per-source depth, target-date windows, max-chars budgets, Markdown or JSON, and cache reuse.
capability_name: context_block
category: context
op: op.wb.context_block
schema_version: wb-capability/v1
parameters:
  sources:
    type: list
    description: 'Source names to include. Default: all registered sources permitted by feature preferences.'
    required: false
  exclude:
    type: list
    description: Source names to drop from the default set.
    required: false
  depth:
    type: str
    description: brief | normal | deep (default normal).
    required: false
  per_source_depth:
    type: dict
    description: '{source: depth} overrides for individual sources.'
    required: false
  target_date:
    type: str
    description: 'YYYY-MM-DD. Default: today (no time window shift).'
    required: false
  window_days:
    type: int
    description: Window size around target_date (default 1).
    required: false
  max_chars:
    type: int
    description: Rendering budget. Truncates respecting section boundaries when possible.
    required: false
  max_age_seconds:
    type: int
    description: Use cached fetch if younger than this. None (default) = always fresh. 0 = use any cached.
    required: false
  custom:
    type: dict
    description: Per-source ad-hoc params forwarded to each source's collect().
    required: false
  format:
    type: str
    description: markdown (default) or json.
    required: false
tags:
- context
- block
aliases:
- user context
- what is the user working on
- active tasks projects commits
- context packet
- assemble context
- build context block
parents:
- context
---
