---
name: Namespace Lookup
kind: skill
description: Return the closest existing namespace tags to a single query. Designed for the 'did you mean?' check before minting a brand-new namespace — the agent calls this to confirm a proposed namespace isn't a near-duplicate of something that already exists.
category: tasks
op: op.wb.namespace_lookup
schema_version: wb-skill/v1
parameters:
  query:
    type: str
    description: Candidate namespace path or label (with or without leading '#')
    required: true
  limit:
    type: int
    description: Max results (default 5)
    required: false
skill_name: namespace_lookup
tags:
- tasks
- namespace
- lookup
aliases:
- did you mean namespace
- nearest namespace
- check namespace exists
- namespace duplicate check
parents:
- tasks
---
