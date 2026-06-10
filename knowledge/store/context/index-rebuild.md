---
name: Index Rebuild
kind: capability
description: Incrementally (re)build the consolidated index (work_buddy/index/) into its separate db. Flag-gated — a no-op while index.enabled is false (ships inert); once the flag is flipped it refreshes the named partition (e.g. 'knowledge') or all partitions. Drives the consolidated-index-rebuild sidecar job that keeps the activated dev-document scan's knowledge search current.
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
