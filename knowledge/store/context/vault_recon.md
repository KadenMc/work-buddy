---
name: Vault Recon
kind: capability
description: 'Diagnostic-grade vault reconnaissance. Returns cross-tabs an agent can reason over to spot recurring conventions: frontmatter state machines (type x status), tag families (depth-3 tree), path-by-type distribution, recent activity by region, cardinality-capped frontmatter values. Single page walk with anti-noise caps.'
capability_name: vault_recon
category: context
op: op.wb.vault_recon
schema_version: wb-capability/v1
parameters:
  path_prefix:
    type: str
    description: 'Optional vault-relative path prefix to scope the walk (e.g. ''repos/electricrag/''). Default: full vault.'
    required: false
  activity_days:
    type: int
    description: 'Lookback window in days for recent_activity_by_path. Default: 30.'
    required: false
tags:
- context
- vault
- recon
aliases:
- vault reconnaissance
- vault recon
- vault structure analysis
- frontmatter state machine
- type by status
- tag tree
- vault cross-tab
- vault patterns
- discover vault conventions
parents:
- context
requires:
- obsidian
- datacore
---
