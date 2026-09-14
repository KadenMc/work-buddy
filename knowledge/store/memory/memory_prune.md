---
name: Memory Prune
kind: skill
description: Delete memories from the bank. CONSENT-GATED, IRREVERSIBLE. Call with no args to list documents for review. Then provide document_id to delete a specific document's memories, or memory_type to bulk-delete a category (world/experience/observation).
category: memory
op: op.wb.memory_prune
schema_version: wb-skill/v1
parameters:
  document_id:
    type: str
    description: Delete a specific document and its derived memories
    required: false
  memory_type:
    type: str
    description: 'Bulk delete by type: world, experience, or observation'
    required: false
skill_name: memory_prune
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
