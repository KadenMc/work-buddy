---
name: Memory Prune
kind: capability
description: Delete memories from the bank. CONSENT-GATED, IRREVERSIBLE. Call with no args to list documents for review. Then provide document_id to delete a specific document's memories, or memory_type to bulk-delete a category (world/experience/observation).
capability_name: memory_prune
category: memory
op: op.wb.memory_prune
schema_version: wb-capability/v1
parameters:
  document_id:
    type: str
    description: Delete a specific document and its derived memories
    required: false
  memory_type:
    type: str
    description: 'Bulk delete by type: world, experience, or observation'
    required: false
tags:
- memory
- prune
aliases:
- forget memories
- delete memory bank
- clear memory
- remove memories
- prune hindsight
- wipe memories
parents:
- memory
requires:
- hindsight
---
