---
name: Knowledge Index Rebuild
kind: capability
description: Rebuild the knowledge search index. Uses the persistent on-disk cache by default — unchanged units keep their cached vectors, so typical warm rebuilds are <1s. Pass force=true to purge the cache and re-embed everything (slow — 1-3 minutes for the full store).
capability_name: knowledge_index_rebuild
category: context
parameters:
  force:
    type: bool
    description: 'Purge the dense-vector cache before rebuilding. Re-embeds every unit. Default: False.'
    required: false
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
- context
---
