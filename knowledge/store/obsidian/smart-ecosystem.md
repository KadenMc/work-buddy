---
name: Smart Connections Ecosystem
kind: integration
description: Smart Plugins ecosystem (9 plugins, Pro license) -- SmartEnv runtime, embedding, semantic search, memory pressure
tags:
- obsidian
- smart-connections
- embedding
- semantic-search
- eval_js
- transformers
- omnisearch
aliases:
- smart connections
- smart environment
- smart plugins
- smartenv
- omnisearch
parents:
- obsidian
- obsidian
---

Smart Plugins ecosystem integration (9 plugins, Pro license $1400+).

## Smart Environment (window.smart_env)

Shared runtime globally accessible. Key collections: smart_sources (51K+ documents), smart_blocks (229K+ sub-document items), embedding_models, smart_contexts (named context packs), smart_templates, event_logs (47 event types). All collections shared regardless of which plugin registered them -- Pro gates UI views, not data access.

## Embedding Model

TaylorAI/bge-micro-v2, 384 dimensions, 512 max tokens. Runs locally in Obsidian via Transformers.js in a hidden iframe (smart_embed_iframe). No external API calls.

## Integration Vectors

1. eval_js bridge (primary): Python -> HTTP POST -> plugin bridge -> JS eval -> SmartEnv
2. Direct disk (.smart-env/multi/*.ajson): Append-only JSON with embeddings per source, readable without Obsidian running
3. Omnisearch (lexical): globalThis.omnisearch.search(query) -> BM25 results up to 50

## Confirmed Capabilities

- Embed arbitrary text: em.embed('text') -> {vec: Float32Array(384), tokens}
- Batch embedding: em.embed_batch([{embed_input: '...'}, ...])
- Semantic nearest-neighbor: entities_vector_adapter.nearest(vec, {}) for both sources and blocks (~51 results default)
- Full embed-then-search pipeline confirmed end-to-end
- lookup_context action: dynamically loaded module, takes {hypotheticals, limit, env}, creates SmartContext with ranked results. Supports multi-query fusion.
- Smart Context: create via env.smart_contexts.new_context()

## Memory Pressure (Critical)

SmartEnv with large vault uses ~3.8GB of 4GB V8 heap limit (94%). Check `check_ready()["performance_memory"]["js_heap_used_mb"]` before heavy operations. Avoid rapid-fire embedding calls. SmartEnv takes 60-90+ seconds to fully load after Obsidian starts.

## Known Issues

- Iframe adapter lazy loading: embed() fails if iframe not initialized. Call em.load() first.
- Message queue stalling: Failed embed calls leave dead entries in em.message_queue blocking subsequent calls. Clear manually with reject+delete loop.
- No api.search on Smart Connections v4.4.0 (changed from older versions).
- connect_pro:error spam: "All tunnels dead" errors. Harmless if not using ChatGPT tunnel.
- __pycache__ indexing: Smart Connections tries to index Python cache dirs. Add to exclusion list.

## Stale Warning

All internal surfaces undocumented, bundled/minified. Runtime probing is the only discovery method. Pin to tested versions and use capability detection.

<<wb:obsidian/bridge>>
