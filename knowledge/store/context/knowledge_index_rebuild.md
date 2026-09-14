---
name: Knowledge Index Rebuild
kind: skill
description: Rebuild the knowledge search index. Uses the persistent on-disk cache by default — unchanged units keep their cached vectors, so typical warm rebuilds are <1s. Pass force=true to purge the cache and re-embed everything (slow — 1-3 minutes for the full store).
category: context
op: op.wb.knowledge_index_rebuild
schema_version: wb-skill/v1
parameters:
  force:
    type: bool
    description: 'Purge the dense-vector cache before rebuilding. Re-embeds every unit. Default: False.'
    required: false
skill_name: knowledge_index_rebuild
tags:
- context
- knowledge
- index
- rebuild
aliases:
- rebuild index
- reindex knowledge
- embedding index
- knowledge index
- rebuild search
parents:
- context
---
