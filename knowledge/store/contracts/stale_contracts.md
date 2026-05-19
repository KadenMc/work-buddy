---
name: Stale Contracts
kind: capability
description: List contracts not reviewed in N days (default 7)
capability_name: stale_contracts
category: contracts
op: op.wb.stale_contracts
schema_version: wb-capability/v1
parameters:
  stale_days:
    type: int
    description: Days since last review (default 7)
    required: false
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
requires:
- obsidian
---
