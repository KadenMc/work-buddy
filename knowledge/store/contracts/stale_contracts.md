---
name: Stale Contracts
kind: skill
description: List Contracts SQLite records not reviewed in N days (default 7), without requiring Obsidian.
category: contracts
op: op.wb.stale_contracts
schema_version: wb-skill/v1
parameters:
  stale_days:
    type: int
    description: Days since last review (default 7)
    required: false
skill_name: stale_contracts
tags:
- contracts
- stale
aliases:
- forgotten contracts
- not reviewed recently
- stale commitments
- unvisited contracts
- dormant work
- contracts needing review
- neglected contracts
parents:
- contracts
---
