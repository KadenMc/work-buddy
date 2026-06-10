---
name: Index Rebuild
kind: capability
description: Incrementally (re)build the consolidated index (work_buddy/index/) into its separate db. Flag-gated — a no-op while index.enabled is false (ships inert); once the flag is flipped it refreshes the named partition (e.g. 'knowledge'), or all partitions when none is given (one-shot full rebuilds only — recurring freshness uses one per-partition job each, e.g. consolidated-knowledge-rebuild). Pass a partition for the per-partition sidecar jobs; omit it only for a deliberate dashboard/manual full rebuild.
capability_name: index_rebuild
category: context
op: op.wb.index_rebuild
schema_version: wb-capability/v1
parameters:
  partition:
    type: str
    description: 'Partition to (re)build (e.g. ''knowledge''); omit to build all partitions'
    required: false
  force:
    type: bool
    description: Rebuild from scratch (default False = incremental, content-hash diff)
    required: false
tags:
- context
- index
- consolidated
aliases:
- rebuild consolidated index
- consolidated index build
- refresh consolidated index
parents:
- context
---
