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

  ## Vector store durability: atomic writes + recovery sweep

  The per-source/per-projection ``.npz`` vector files are a regenerable cache (the source docs live in ``work_buddy_ir.db`` plus the JSONL sessions), so the contract is resilience and self-heal, not durability:

  - **Atomic writes.** ``work_buddy/ir/store.py::save_vectors`` writes through ``work_buddy/utils/npz_io.py::atomic_save_npz``: a sibling temp (``<name>.<pid>.tmp.npz``) written via an *open file object* (so numpy neither re-opens nor appends ``.npz``, and the fd can be ``fsync``'d), then ``os.replace``'d onto the canonical path with a short Windows-``PermissionError`` retry. A process killed mid-write can no longer truncate the canonical file into a 0-byte search outage. This matters on every write, not just the last one: the ``CHECKPOINT_ROWS`` loop in ``ir/dense.py`` calls ``save_vectors`` many times per large build.
  - **Corrupt-tolerant reads.** ``load_vectors`` goes through ``safe_load_npz``, which returns ``None`` (never raises) for a missing, zero-byte, or truncated file — catching ``EOFError`` / ``zipfile.BadZipFile`` / ``ValueError`` / ``OSError`` (``BadZipFile`` is listed explicitly; it does not subclass the others). A corrupt file therefore degrades to BM25-only and the next build regenerates, instead of raising and taking the source dark behind an ``/ir/index`` 500. The read is pure — it never mutates the filesystem.
  - **Startup recovery sweep.** ``recover_vector_store(cfg)`` runs in ``embedding/service.py::main()`` before serving. It quarantines a corrupt canonical file to ``<name>.corrupt`` (one forensic copy) and deletes orphaned ``*.tmp.npz`` temps whose writer PID is dead (``work_buddy/utils/process.py::is_process_alive``) or which are stale beyond ``ORPHAN_TEMP_MAX_AGE_S`` — while sparing a live writer's temp. The build runs in-process and the sweep runs before ``app.run``, so there is no in-process writer to race.

  ``work_buddy/knowledge/persistence.py`` shares the same ``atomic_save_npz`` primitive (passing a fixed temp name, since its cache dir is not swept). The ``.npz`` durability primitives live in ``utils/npz_io.py`` so both vector caches use one implementation; its header-gated loaders stay local because they bail on a model/version mismatch before materializing arrays.

  ## ``/ir/index`` error surfacing

  A reachable embedding service that fails an ``/ir/index`` request surfaces the **real** error, not a generic "service unavailable". ``client._request`` catches ``HTTPError`` (a subclass of ``URLError``, so it must be caught first) separately from connection failures, logs the status + body, and — with ``return_http_error=True`` (passed only by ``ir_index``) — returns ``{"error", "status"}`` rather than collapsing a 500 to ``None``. The ``ir_index`` dispatch then distinguishes: ``None`` → service unreachable (remediation points at the sidecar via ``utils/service_hints.py::sidecar_restart_command``, since the embedding service is a sidecar-supervised child, not a standalone scheduled task); an error envelope → surface the real ``/ir/index`` error; otherwise the status/build result. Every *other* client caller still gets ``None`` on any failure — the flag defaults off, so their graceful-degradation contract is unchanged.

  ## Adding a new model

  Add an entry to `config.yaml` under `embedding.models` (or `_DEFAULT_MODELS` in `service.py` for the fallback). Required fields: HF name, dims, `eager` (bool). Eager-load only models on the critical path for interactive work; lazy-load large models (e.g. the 526 MB `leaf-ir` passage encoder) that are used only during indexing.

  ## Lazy models: cold-load timeout tolerance

  Indexing callers of lazy models MUST pass an extended ``timeout_s`` to ``embed()`` / ``embed_for_ir()``. The first request that hits a lazy model triggers a SentenceTransformer instantiation, which for large passage encoders can run into the tens of seconds. The default per-request timeout is ``max(30, len(texts) * 2)`` — too short for the cold load on small cache-miss batches. Without override, the build silently leaves ``has_content_vectors=False`` until something else triggers a rebuild.

  Reference implementation: ``work_buddy/knowledge/index.py`` defines ``_CONTENT_COLD_LOAD_TIMEOUT_S`` and the ``_build_content_vectors`` lambda passes ``timeout_s=max(_CONTENT_COLD_LOAD_TIMEOUT_S, len(batch) * 2)`` to ``embed_for_ir(role="document")``. Only the first batch ever spends the extended budget; subsequent batches see the warm model and finish fast. The cost when the service is genuinely down is one extended wait per build attempt instead of the default — acceptable since it is a per-boot one-shot.

  If you add a new lazy-model consumer, follow the same pattern. Tune the floor by measuring the cold-load time of the specific model on representative hardware and adding margin. Do NOT switch the model to eager just to avoid this — lazy loading for indexing-only models is the documented design intent (see Model registry / Adding a new model above).

  ## Don't assume bare `embed()` is right

  'Default model for all' is a tempting shortcut. Every new semantic-scoring site should ask: am I comparing query-shaped things to query-shaped things (use `leaf-mt`), or query-shaped to passage-shaped (use `embed_for_ir` with the correct role)? The wrong answer produces subtle quality loss, not visible errors.

  ## Consolidated index: resident-matrix prewarm at startup

  The consolidated index (``work_buddy/index/``) serves search from per-(partition, projection) dense matrices held resident in this process (``index/resident.py`` — the generic ResidentCache the vault matrix and model registry reuse). They are lazy-loaded on a partition's first search, so a large partition (vault is ~88k×768) pays a tens-of-seconds load on that first query — long enough to exceed the request timeout, returning ``None`` and silently missing the consolidated index until something queries it again.

  ``main()`` prewarms them: when ``index.enabled``, a background daemon (``index/partitioned.py::start_prewarm`` -> ``prewarm_resident_matrices``) loads every BUILT partition's matrices up front, **largest partition first** (so the slowest-to-load, highest-cold-cost partitions are protected soonest). It goes through the same ``HybridSearcher._resident`` caches the serving path reads (no key drift), does NO model encode (pure SQLite read + numpy reshape, so it doesn't contend for the inference broker / GPU), and is idempotent with the consolidated-index idle evictor (``resident.start_idle_evictor``: warm -> serve -> release after the idle TTL -> re-warm on next query). It is gated — a disabled or unbuilt index warms nothing, and the daemon never blocks ``/health`` or query serving.

  Caveat: prewarm shrinks but does not eliminate the first-query cold window. Matrix loading is GIL-heavy (a ``b"".join`` over a partition's vector blobs), so on a contended boot — eager model load plus concurrent query-encodes competing for one GIL — warming all partitions can take minutes, and a query can still arrive while its partition is mid-warm. The **warming signal** (below) is what makes that residual window cheap to ride out instead of a hard cold-load stall.

  ## Consolidated index: warming signal (non-blocking cold serve)

  Closes the residual window the prewarm caveat describes: a query whose partition is not yet resident serves lexical-only NOW and tells the caller the dense side is warming, instead of blocking on the cold matrix load (or timing out to ``None``, which is indistinguishable from "service down").

  **Contract.** ``/index/search`` and ``/index/search_many`` take an optional request flag ``block_until_warm`` (default false) and may return, alongside ``results``, a ``warming`` list (the partitions whose dense matrix wasn't resident) and a ``retry_after_s`` estimate. Default (non-blocking) mode: a cold partition is searched lexical-only, the endpoint singleflights a background warm of it (``index/partitioned.py::warm_partitions_async`` — concurrent cold queries for the same partition spawn ONE warm, not N), and the partition is reported in ``warming``. ``block_until_warm=true`` (the retry) loads the matrix inline, returning full hybrid results with no ``warming`` field. The whole behavior is kill-switchable via ``index.warming_signal`` (default true), which reverts to the inline blocking load.

  **Non-blocking serving skips the cold encode too.** In non-blocking mode ``HybridSearcher`` peeks each projection via ``ResidentCache.get_if_cached`` (never loads) and query-encodes ONLY the warm projections: a cold projection's dense vector would be unused (its matrix is skipped), so encoding it would block on the query model for nothing — exactly the latency the warming path exists to avoid. Cold partition → no encode, no matrix load, just FTS5 + the warming flag.

  **Client distinguishes three states.** ``embedding/client.py::index_search`` / ``index_search_many`` take ``warm_retry``; on a ``warming`` response they wait ``min(retry_after_s, cap)`` then retry ONCE with ``block_until_warm=true`` and an extended timeout (mirrors ``knowledge/index.py::_CONTENT_COLD_LOAD_TIMEOUT_S``). This keeps ``None`` (service down → no retry, fall back), ``warming`` (cold → retry against the now-warming matrix), and a plain result (final) DISTINCT — the disambiguation a bare ``None`` could never make. The consumers (``knowledge/search.py``, ``dev/document.py``) opt in; their existing ``None``/empty → live-index fallback stays the *down* path.

  **Hot-path readiness invariant (load-bearing).** The readiness predicate (``UnifiedIndex.cold_partitions`` → ``IndexPartition.is_warm``) runs on every cold-eligible query, so it must stay O(projections) in RAM: it checks ONLY ``ResidentCache.is_cached()`` (a pure in-memory flag) and must NEVER call ``store.vector_count()`` — that's a ``COUNT(DISTINCT) … JOIN`` across the whole (all-partition) ``doc_vectors`` table, ~40s at vault scale, i.e. a 40-second query on the serving path. ``warm_eta_s`` likewise uses the cheap partition-indexed ``doc_count``, not ``vector_count``. The trade-off: a projection that legitimately has no vectors reads as "cold" forever (one redundant warm-retry, then graceful fallback) — deliberately accepted over probing vector counts per query.

  ## Key dev files

  - `work_buddy/embedding/service.py` — Flask service, `_DEFAULT_MODELS`, `ModelEntry`, lazy loader, `/embed` and `/similarity` handlers; `/ir/index` now also triggers a best-effort `dense.build_vectors` so hybrid search has vectors to score against.
  - `work_buddy/embedding/client.py` — `embed`, `embed_for_ir`, `similarity_search`, `hybrid_search`, `ir_search`, `ir_index`. ``embed()`` and ``embed_for_ir()`` accept ``timeout_s`` for indexing callers that need to absorb a lazy-model cold load — see 'Lazy models' section above. ``_request`` splits ``HTTPError`` from connection failures and takes ``return_http_error`` so ``ir_index`` can surface a real ``/ir/index`` error — see '``/ir/index`` error surfacing' above.
  - `work_buddy/knowledge/index.py` — knowledge search index; three-signal RRF fusion (custom path).
  - `work_buddy/knowledge/persistence.py` — disk cache for content + alias vectors (hash-keyed, model+version headers); atomic writes via ``utils/npz_io.atomic_save_npz``.
  - `work_buddy/utils/npz_io.py` — shared atomic ``.npz`` write (``atomic_save_npz``), corrupt-tolerant load (``safe_load_npz``), and the temp-naming convention the recovery sweep parses. Used by both ``ir/store.py`` and ``knowledge/persistence.py``.
  - `work_buddy/ir/sources/base.py` — `Document`, `Projection`, `ProjectionSpec`, `Source` protocol, `get_projection_schema` helper.
  - `work_buddy/ir/dense.py` — kind-aware `encode_query` / `encode_documents`; pool-aware `score_dense`; projection-aware `build_vectors` with shared `_build_vectors_for_projection` inner loop.
  - `work_buddy/ir/engine.py` — `search` runs BM25 plus one ranking per declared projection (or one legacy ranking) and RRF-fuses; per-result `projection_scores` diagnostic.
  - `work_buddy/ir/store.py` — `documents.projections` JSON column + in-place schema migration; `_npz_path` / `save_vectors` / `load_vectors` accept a `projection` arg. ``save_vectors`` is atomic and ``load_vectors`` is corrupt-tolerant (both via ``utils/npz_io``); ``recover_vector_store`` is the startup quarantine/orphan-temp sweep — see 'Vector store durability' above.
---

## Overview

A long-running sidecar service providing dense vector embeddings for work-buddy's search and similarity features. Exposes an HTTP API on `localhost:5124` with eager-loaded models, so interactive calls are fast.

## Endpoints

- `POST /embed` — embed a batch of texts, return vectors
- `POST /similarity` — cosine similarity between a query and candidate texts
- `POST /search` — BM25 + embedding hybrid search over candidates
- `POST /ir/search`, `POST /ir/index` — indexed IR search over registered sources
- `POST /vault/search`, `POST /vault/index` — vault semantic index search / build, run
  in-process so the resident vector matrix stays warm and the bulk encode shares the broker
  (see `architecture/vault-index`)
- `POST /index/search`, `POST /index/search_many` — consolidated-index hybrid search over one
  or more partitions (`/index/search_many` shares ONE query-encode across a batch of queries);
  `POST /index/build` — incremental build of a partition (or all) into the separate
  `db/index-consolidated` DB. Run in-process so the resident matrices stay warm and the bulk
  encode shares the broker (see `architecture/consolidated-index`)
- `GET /health` — liveness probe

## Client

`work_buddy/embedding/client.py` wraps the HTTP API:

- `embed(texts)` — plain batch embedding, uses the service's default model
- `embed_for_ir(texts, role="query"|"document")` — asymmetric IR encoding via the query↔document model pair
- `similarity_search(query, candidates)` — rank candidates by similarity
- `hybrid_search(query, candidates)` — BM25 + dense blend
- `ir_search(query)` — search a pre-built IR index
- `ir_index(action, source, ...)` — build or check an IR index
- `vault_search(query, ...)`, `vault_index(action, ...)` — vault semantic index search / build
  (see `architecture/vault-index`)
- `index_search(query, ...)`, `index_search_many(queries, ...)` — consolidated-index hybrid
  search over its partitions (see `architecture/consolidated-index`)

## Graceful degradation

Every client function returns `None` (or an empty list) when the service is unavailable. Callers must handle this — typically by falling back to BM25 alone or skipping the semantic step. Never assume the service is up; always handle the `None` path.

## Consumers

Knowledge search, IR conversation search, native vault search, task-triage similarity, and other semantic-scoring sites throughout the codebase all route through this service. Model loading happens once here; all callers share the loaded weights.

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
