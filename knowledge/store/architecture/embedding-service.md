---
name: Embedding Service
kind: service
description: Local HTTP service on port 5124 providing dense vector embeddings for search and similarity; exposes a symmetric default model plus an asymmetric query/document pair via role-aware client wrappers.
entry_points:
- work_buddy.embedding.service
- work_buddy.embedding.client
tags:
- embedding
- vectors
- search
- similarity
- asymmetric
- symmetric
- leaf-ir
- leaf-mt
- dense-retrieval
aliases:
- embedding service
- dense vectors
- semantic search backend
- vector similarity
- embedding client
parents:
- architecture
- architecture
dev_notes: |-
  ## doc_count vs vector_count — read ``dense_eligible_docs``

  The IR index status output (``ir_index(action='status')``) exposes three count fields per source. Do **not** treat ``doc_count - vector_count`` as a "backlog" — it usually isn't. The right reading:

  - ``doc_count`` — total rows in the SQLite ``documents`` table for this source.
  - ``dense_eligible_docs`` — rows with non-empty ``dense_text``. This is the set ``_build_vectors_for_projection`` actually attempts to encode on the legacy single-projection path.
  - ``vector_count`` — rows in the ``.npz`` file.
  - ``pending_eligible`` (under ``vectors.<source>``) — ``dense_eligible_docs - vector_count``. **This** is the real backlog.

  ## Conversation source: tool-only spans are NOT indexed / NOT searchable via semantic

  ``conversation`` intentionally leaves ``dense_text`` empty for tool-only spans where ``user_texts[0]`` and ``asst_texts[0]`` are both empty (see ``work_buddy/ir/sources/conversations.py`` for the explicit rationale). Roughly HALF of conversation spans fall into this bucket in practice.

  Consequences for callers:

  - Those spans DO NOT participate in dense/semantic retrieval.
  - They DO NOT appear in ``context_search`` results on the conversation source when ``method='semantic'`` or ``method='keyword,semantic'``.
  - They DO still have ``display_text`` + metadata in the SQLite store, so ``method='substring'`` can still find them.

  This is a deliberate retrieval-quality choice — tool-result payloads are often verbose boilerplate that would dilute semantic search. But it IS a real limitation worth being explicit about: tool-result searching is a separate concern and not what the conversation dense index is for.

  When investigating "why is my index half-empty?", check ``pending_eligible`` first — if it's small, the encoder is keeping up and the doc/vector gap is expected. If tool-only spans ever become retrieval-valuable, the fix is in the ingestor (fall back to ``display_text`` when the user+assistant-first strategy yields empty), not in the indexer.

  ## Model registry

  Three models are registered in `work_buddy/embedding/service.py::_DEFAULT_MODELS`:

  - `leaf-mt` — `MongoDB/mdbr-leaf-mt`, **1024-d, eager. DEFAULT MODEL** (used by bare `embed()` calls). Symmetric, multi-task, general-purpose. Correct choice for query↔query or similar-length semantic comparisons.
  - `leaf-ir-query` — `MongoDB/mdbr-leaf-ir`, **768-d, eager**. The QUERY ENCODER of an asymmetric pair. Routed to by `embed_for_ir(..., role="query")`.
  - `leaf-ir` — `MongoDB/mdbr-leaf-ir-asym`, **768-d, lazy**. The PASSAGE/DOCUMENT ENCODER of the same pair. Routed to by `embed_for_ir(..., role="document")`. Note the key-name mismatch with the HuggingFace name: registry key is `leaf-ir`, HF model id is `...-asym`.

  The asymmetric pair produces 768-d vectors in a shared space — you CAN dot-product a query-encoded vector against a document-encoded vector to get query↔passage similarity. You CANNOT meaningfully compare a 768-d IR-pair vector with a 1024-d `leaf-mt` vector; the spaces are disjoint.

  ## Choosing the right entry point

  - Query↔query / similar-length comparisons → `embed(texts)` (resolves to symmetric `leaf-mt`).
  - Query↔document retrieval (short query against long passages) → `embed_for_ir(texts, role=...)`. Use `role="query"` for the query side, `role="document"` for indexing content. This is the correct call for any retrieval-augmented workflow where passages are being matched by short queries.
  - The `prompt_name` arg on `embed()` is for models whose HF cards document query/document prompts (leaf-ir does; leaf-mt does not). `embed_for_ir` sets this automatically and should be preferred over manual prompt handling.

  ## Knowledge search index architecture (shipped via t-3aff6976)

  `work_buddy/knowledge/index.py` now uses both model families in the role-appropriate way, after the rework in task t-3aff6976:

  - **Content dense (768-d, asymmetric).** Unit content is embedded once at build time via `embed_for_ir(role="document")` (→ `leaf-ir`). User queries in the content path are embedded with `embed_for_ir(role="query")` (→ `leaf-ir-query`). Content and query live in the same 768-d space; dot product is meaningful. The content text deliberately EXCLUDES aliases so query-shaped phrases don't dilute passage-shaped signal.
  - **Alias dense (1024-d, symmetric).** Each alias string (authored in `registry.py::search_aliases` or on `KnowledgeUnit.aliases`) is embedded separately via bare `embed()` (→ `leaf-mt`). Queries are embedded the same way. Alias-path similarity is max-pooled per doc across the doc's aliases — one strong alias hit wins, weak aliases don't drag.
  - **Fusion.** Three ranking signals — BM25 (unchanged), content-dense (768-d), alias-dense (1024-d max-pooled) — are fused via Reciprocal Rank Fusion through the `_rrf_fuse` helper. RRF is rank-based, so fusing across different vector spaces is safe. If either dense signal is unavailable (service down, cold model), it's dropped from fusion and the remaining signals are used.
  - **Disk persistence.** `work_buddy/knowledge/persistence.py` hashes `content_text` per unit and `(path, alias_text)` per alias, stores vectors in two .npz files at `data/cache/knowledge_index/{content,aliases}.npz`. On rebuild, only changed/new units re-embed. Warm restart: <1s vs ~150s cold. Model-key + CACHE_VERSION header in each file invalidates safely on format/model changes.

  **Three-signal fusion is fragile without alias coverage.** Capabilities with zero authored aliases lose ground to aliased competitors under RRF (three votes vs. two). For the rework to be a net win, every capability needs >=4 good aliases — see `registry.py` and the Phase 4 brittleness report (`data/report/*-phase4.md`). The baseline brittleness experiment, the post-rework regression, and the post-alias-backfill win are all archived under `data/report/*knowledge-search-brittleness*`.

  ## General IR engine: per-source multi-projection (shipped after the knowledge index)

  `work_buddy/ir/` — the engine that powers `context_search` over conversations, docs, chrome tabs, projects, and task notes — generalizes the same multi-signal pattern to any source via a **projection schema**:

  - A `Source` may declare `projection_schema() -> {name: ProjectionSpec}` (`work_buddy/ir/sources/base.py`). Each spec carries a `kind` (`"label"` → symmetric `leaf-mt`, `"passage"` → asymmetric `leaf-ir` doc/query pair) and a `pool` (`"none" | "max" | "mean"` for list-valued projections).
  - Per-doc, the `Document.projections` field maps each declared key to the text to encode. Projections are persisted in the `documents.projections` JSON column so re-encoding doesn't require re-parsing source files.
  - Vectors are stored per-source-per-projection at `work_buddy_ir.<source>.<projection>.npz`. The legacy unkeyed `work_buddy_ir.<source>.npz` is preserved for sources that declare no schema — conversation, docs, chrome, projects all stay on the single-projection path with no migration.
  - At query time, `engine.search` runs `score_query` per declared projection, encoding the query with the spec's matching encoder, and RRF-fuses BM25 with every projection ranking. `score_dense` aggregates pooled projections (`pool="max"|"mean"`) at query time.
  - First consumer: `TaskNoteSource` declares `line` (label, canonical task-line text from the master list) + `body` (passage, note body). RRF fuses three signals — BM25 over `line/title/body` fields plus the two dense rankings.

  This is the structural twin of the knowledge-system three-signal design above. Future migration of `work_buddy/knowledge/index.py` onto this engine is the natural next step (gated on the brittleness harness to avoid wb_search regression); doing so would collapse the two parallel implementations.

  ## Adding a new model

  Add an entry to `config.yaml` under `embedding.models` (or `_DEFAULT_MODELS` in `service.py` for the fallback). Required fields: HF name, dims, `eager` (bool). Eager-load only models on the critical path for interactive work; lazy-load large models (e.g. the 526 MB `leaf-ir` passage encoder) that are used only during indexing.

  ## Lazy models: cold-load timeout tolerance

  Indexing callers of lazy models MUST pass an extended ``timeout_s`` to ``embed()`` / ``embed_for_ir()``. The first request that hits a lazy model triggers a SentenceTransformer instantiation, which for large passage encoders can run into the tens of seconds. The default per-request timeout is ``max(30, len(texts) * 2)`` — too short for the cold load on small cache-miss batches. Without override, the build silently leaves ``has_content_vectors=False`` until something else triggers a rebuild.

  Reference implementation: ``work_buddy/knowledge/index.py`` defines ``_CONTENT_COLD_LOAD_TIMEOUT_S`` and the ``_build_content_vectors`` lambda passes ``timeout_s=max(_CONTENT_COLD_LOAD_TIMEOUT_S, len(batch) * 2)`` to ``embed_for_ir(role="document")``. Only the first batch ever spends the extended budget; subsequent batches see the warm model and finish fast. The cost when the service is genuinely down is one extended wait per build attempt instead of the default — acceptable since it is a per-boot one-shot.

  If you add a new lazy-model consumer, follow the same pattern. Tune the floor by measuring the cold-load time of the specific model on representative hardware and adding margin. Do NOT switch the model to eager just to avoid this — lazy loading for indexing-only models is the documented design intent (see Model registry / Adding a new model above).

  ## Don't assume bare `embed()` is right

  'Default model for all' is a tempting shortcut. Every new semantic-scoring site should ask: am I comparing query-shaped things to query-shaped things (use `leaf-mt`), or query-shaped to passage-shaped (use `embed_for_ir` with the correct role)? The wrong answer produces subtle quality loss, not visible errors.

  ## Key dev files

  - `work_buddy/embedding/service.py` — Flask service, `_DEFAULT_MODELS`, `ModelEntry`, lazy loader, `/embed` and `/similarity` handlers; `/ir/index` now also triggers a best-effort `dense.build_vectors` so hybrid search has vectors to score against.
  - `work_buddy/embedding/client.py` — `embed`, `embed_for_ir`, `similarity_search`, `hybrid_search`, `ir_search`, `ir_index`. ``embed()`` and ``embed_for_ir()`` accept ``timeout_s`` for indexing callers that need to absorb a lazy-model cold load — see 'Lazy models' section above.
  - `work_buddy/knowledge/index.py` — knowledge search index; three-signal RRF fusion (custom path).
  - `work_buddy/knowledge/persistence.py` — disk cache for content + alias vectors (hash-keyed, atomic writes, model+version headers).
  - `work_buddy/ir/sources/base.py` — `Document`, `Projection`, `ProjectionSpec`, `Source` protocol, `get_projection_schema` helper.
  - `work_buddy/ir/dense.py` — kind-aware `encode_query` / `encode_documents`; pool-aware `score_dense`; projection-aware `build_vectors` with shared `_build_vectors_for_projection` inner loop.
  - `work_buddy/ir/engine.py` — `search` runs BM25 plus one ranking per declared projection (or one legacy ranking) and RRF-fuses; per-result `projection_scores` diagnostic.
  - `work_buddy/ir/store.py` — `documents.projections` JSON column + in-place schema migration; `_npz_path` / `save_vectors` / `load_vectors` accept a `projection` arg.
---

## Overview

A long-running sidecar service providing dense vector embeddings for work-buddy's search and similarity features. Exposes an HTTP API on `localhost:5124` with eager-loaded models, so interactive calls are fast.

## Endpoints

- `POST /embed` — embed a batch of texts, return vectors
- `POST /similarity` — cosine similarity between a query and candidate texts
- `POST /search` — BM25 + embedding hybrid search over candidates
- `POST /ir/search`, `POST /ir/index` — indexed IR search over registered sources
- `GET /health` — liveness probe

## Client

`work_buddy/embedding/client.py` wraps the HTTP API:

- `embed(texts)` — plain batch embedding, uses the service's default model
- `embed_for_ir(texts, role="query"|"document")` — asymmetric IR encoding via the query↔document model pair
- `similarity_search(query, candidates)` — rank candidates by similarity
- `hybrid_search(query, candidates)` — BM25 + dense blend
- `ir_search(query)` — search a pre-built IR index
- `ir_index(action, source, ...)` — build or check an IR index

## Graceful degradation

Every client function returns `None` (or an empty list) when the service is unavailable. Callers must handle this — typically by falling back to BM25 alone or skipping the semantic step. Never assume the service is up; always handle the `None` path.

## Consumers

Knowledge search, IR conversation search, Smart Connections ranking, task-triage similarity, and other semantic-scoring sites throughout the codebase all route through this service. Model loading happens once here; all callers share the loaded weights.

## Optional: LM Studio offload for bulk document encoding

Bulk document encoding (the big passage-side model, ``leaf-ir`` / ``snowflake-arctic-embed-m-v1.5``) can route through LM Studio instead of loading locally — moves ~500 MB of RSS off the main machine, optionally via LM Link to a remote compute device. Opt-in per model via ``embedding.models.<key>.provider: lmstudio`` in config.

When enabled, the call path is:

```
ir.dense._encode_bulk_direct
  → work_buddy.embedding.providers.lmstudio.encode
    → LocalInferenceBroker.slot(profile=f"lmstudio:{model_id}", priority=BACKGROUND, ...)
      → httpx POST to LM Studio /v1/embeddings
```

Query-side encoding (``leaf-ir-query``, ``leaf-mt``) is NOT offloaded — query latency is user-facing and the network hop would hurt.

On LM Studio errors, per-model ``on_error: fallback | fail`` decides the behavior. ``fallback`` (default) drops to the in-process sentence-transformers path. Measured drift between Q8 GGUF and fp32 sentence-transformers is ~0.0002 cosine, so mixed-provenance vectors in the same index cluster correctly.

See ``architecture/inference/broker`` for the admission-control / priority / metrics layer, and ``features/lmstudio-offload-setup`` for the end-to-end setup procedure (GGUF download, metadata audit, drift test, config flip).

## Key files

- `work_buddy/embedding/service.py` — Flask service, model registry, lazy loading. ``_get_model()`` uses a per-entry Condition so a cold load of one model doesn't block concurrent access to another.
- `work_buddy/embedding/client.py` — HTTP client with role-aware wrappers.
- `work_buddy/embedding/providers/lmstudio.py` — optional LM Studio provider (broker-wrapped).
- `work_buddy/embedding/__main__.py` — service entry point (launched by the sidecar).
