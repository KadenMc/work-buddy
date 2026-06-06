---
name: Vault Search
kind: capability
description: Hybrid (lexical + dense) semantic search over your indexed vault(s) — notes and repos chunked at heading level, running natively in work-buddy (not Obsidian / Smart Connections). Returns ranked chunk excerpts with bm25 and dense scores. Served warm from the embedding service's resident matrix; degrades to lexical-only (FTS5) if the embedding service is down.
capability_name: vault_search
category: context
op: op.wb.vault_search
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    description: Natural-language search query.
    required: true
  top_k:
    type: int
    description: Max results to return (default 10).
    required: false
  method:
    type: str
    description: "Retrieval method: 'hybrid' (default), 'lexical' (FTS5 only), or 'dense' (vectors only)."
    required: false
  vault_id:
    type: str
    description: Restrict to a single vault by its configured id (optional; omit to search all).
    required: false
  recency:
    type: bool
    description: Apply a recency bias to ranking (default false).
    required: false
tags:
- context
- vault
- search
- semantic
aliases:
- search vault
- vault search
- search my notes
- semantic search notes
- search notes
parents:
- context
---

Native chunk-level semantic search over the vault semantic index. Pairs
with `vault_index` (build/status). Use `vault_search` to retrieve relevant note/repo
passages; use `vault_index` action=status to see what is and isn't indexed.
