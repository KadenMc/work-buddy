---
name: Index Rebuild
kind: capability
description: Incrementally (re)build the consolidated index (work_buddy/index/) into its separate db. Flag-gated — a no-op while index.enabled is false; when enabled it refreshes the named partition (including native journal, projects, contracts, or personal_knowledge), or all partitions when none is given. Database partitions snapshot durable search events, build and verify source/index parity, then acknowledge exactly that snapshot; interruption leaves replayable lag. The result carries wb.search-outbox-delivery/v1 readiness evidence. Pass force:true only for a deliberate partition backfill. The op self-skips while any build holds the shared writer gate.
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
    description: Full partition backfill using the same build-verify-ack delivery boundary (default False = incremental replay/content diff)
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
