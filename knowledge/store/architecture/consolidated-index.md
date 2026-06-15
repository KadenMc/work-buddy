---
name: Consolidated Index
kind: system
description: One unified lexical+dense hybrid search substrate (work_buddy/index/) serving every work-buddy corpus — knowledge units, vault chunks, conversation spans, Chrome tabs, summaries, projects, task notes — from a single SQLite DB. Composable ports (Partition/Encoder/ResidentCache), one shared store keyed by a partition column, builds serialized on a DB-wide writer gate, FTS5⊕dense RRF search with metadata filter pushdown. Flag-gated by index.enabled; each consumer routes via its own index.consumers.<name> gate.
tags:
- index
- consolidated
- search
- hybrid
- fts5
- dense-retrieval
- rrf
- sqlite
- partitions
- embeddings
aliases:
- consolidated index
- unified index
- index package
- work_buddy/index
- hybrid search index
- index partitions
parents:
- architecture
dev_notes: |-
  ## Single-writer DB → builds serialize on a DB-wide gate
  Every partition lives in ONE SQLite DB (`db/index-consolidated.db`), and SQLite allows
  one writer per DB. So `IndexBuilder` (`build.py`) takes TWO advisory locks (both
  heartbeated via `utils/index_lock`): the DB-wide `<db>.build` WRITER GATE, then the
  per-partition `<db>.<partition>` lock — always gate-then-partition, a fixed order so two
  builders cannot deadlock holding one each. Result: builds across all partitions AND all
  processes (sidecar refresh crons, the `/index/build` endpoint, the CLI, the dashboard
  seam) physically serialize. The `index_rebuild` op probes the gate read-only and
  self-skips while ANY build runs.

  The advisory lock self-heartbeats from a daemon thread (every `stale_after_s/3`) for the
  whole hold, so a multi-hour build never ages past the 1h stale window and looks
  abandoned. CAVEAT: a dead holder's PID can be reused by another process, making
  `is_locked` honor a stale lock until it ages past `stale_after_s` (3600s) — a build
  killed mid-flight (e.g. by a service restart) may leave a `*.lock` needing manual
  removal. Never SIGKILL a build; let it finish or self-skip.

  ## Writes ride out contention; backfill is resumable
  `IndexStore._connect` sets WAL + a 30s busy timeout, and every write method is wrapped
  in `_write_retry` (bounded backoff on `database is locked`). Safe because each write is
  one small idempotent connect→commit→close transaction. Non-lock `OperationalError`s
  surface immediately. The vector backfill (`build._encode_missing`) encodes→writes in
  256-doc batches (`_ENCODE_MISSING_BATCH`), not one terminal commit — durable progress
  every batch, short writer holds, resumes from the last batch after an interruption.
  Builds never lose committed progress (per-item + per-batch commits).

  ## Object model is inheritance-free (Protocols + injection)
  `Partition`, and the encode/provider seams, are `Protocol`s; `ResidentCache` takes an
  injected loader. Optional partition methods (`projection_schema`, `hydrate`) are read via
  module-level helpers (`get_projection_schema`, `hydrate`) so a partition that omits them
  still works — no base class, no forced overrides. Domains register a lazy `() -> Partition`
  factory at import (`domain → index`, never the reverse), so the engine never imports a
  domain at module load.

  ## Activation pathway (operational)
  A consumer goes live by flipping `index.consumers.<name>: true` in `config.local.yaml`
  (master `index.enabled` must already be true). Re-pointing the agent_docs/knowledge
  search additionally needs a one-time forced `knowledge` rebuild so partition-side
  metadata (category/severity) lands — content-hash-unchanged units won't auto-trigger.
  The embedding-service process must restart to load edited `partition.py` BEFORE that
  rebuild writes the new metadata. A forced rebuild re-embeds the whole partition (slow on
  a small GPU) and serializes behind any running build via the gate — never `force=true` a
  partition that is already built unless you specifically need a metadata refresh.

  ## Key dev files
  - `work_buddy/index/store.py` — `IndexStore`: schema, WAL + busy-timeout + `_write_retry`,
    connection-per-op, FTS5 + float16 blob vectors + change ledger + meta/versioning.
  - `work_buddy/index/build.py` — `IndexBuilder`: diff → per-item parse/upsert/encode →
    prune → version bump; the two-lock `_lock_ctx`; batched resumable backfill.
  - `work_buddy/index/partition.py` — the `Partition` Protocol + lazy registry.
  - `work_buddy/index/partitions/ir_source.py` — wraps the 5 IR sources as partitions
    (`coverage` + `lifecycle()` folded into the change-key; `lifecycle_state` metadata).
  - `work_buddy/index/{search,fusion,recency,resident,encode,model,config,partitioned}.py`.
  - `work_buddy/index/ab.py` — blind-A/B relevance harness used to validate each partition.
  - `work_buddy/indexing/adapters/index_consolidated.py` — the status/bulk-build seam adapter.
---

## Overview

One unified search substrate (`work_buddy/index/`) serving lexical⊕dense hybrid search over
**every** work-buddy corpus from a single SQLite DB. It generalizes the per-domain index
patterns — whole-unit knowledge ranking, the IR engine's multi-source `Document`/`Projection`
model, and the vault index's warm-resident SQLite+FTS5 serving — into one composable engine.

**Flag-gated: inert until `index.enabled` is true** (default false). It builds into its own
separate DB and nothing routes to it until a consumer's gate is flipped, so it can be present
and kept fresh without changing live search behavior.

## Object model (composable ports)

- **Partition** (`partition.py`) — the source PORT (a `Protocol` + lazy factory registry). A
  domain adapter that `discover()`s indexable items (with a change-detection signal) and
  `parse()`s each into one or more `Document`s. Seven are registered: `knowledge`, `vault`,
  `conversation`, `projects`, `chrome`, `summary`, `task_note` (knowledge → `KnowledgePartition`,
  vault → `VaultChunkPartition`, the five IR sources → `IRSourcePartition`).
- **Document** (`model.py`) — `fields` (→ BM25/FTS), `projections` (→ dense; scalar or pooled
  list), `metadata` (JSON, filterable), `display_text`, `content_hash` (change detection),
  `timestamp` (recency).
- **Encoder** (`encode.py`) — kind-aware dense encode through the embedding service (label →
  symmetric `leaf-mt`; passage → asymmetric `leaf-ir` query/document pair).
- **ResidentCache** (`resident.py`) — warm per-`(partition, projection)` dense matrices for
  fast serving, version-invalidated on each build.

## Store

One **single-writer** SQLite DB (`db/index-consolidated.db`) holds all partitions in shared
tables keyed by a `partition` column: `documents`, `doc_fts` (standalone FTS5 over
title/body/tags), `doc_vectors` (float16 blobs, FK-cascaded to documents), `indexed_items`
(the mtime/content-hash change ledger), and `index_meta` (KV — per-partition `build_version`).
A fresh connection per operation (WAL, foreign keys on). Because one DB has many would-be
writers across processes, writes use a busy timeout plus backoff-retry (see dev notes).

## Build

`IndexBuilder` (`build.py`) runs an **incremental, resumable** build of one partition:
discover → diff by change-key (content-hash default, or mtime) → for each changed item delete
its old docs, parse, upsert, encode projections → prune deleted items → bump `build_version`
and invalidate the partition's resident matrices. It runs under a DB-wide writer gate + the
per-partition lock, so concurrent builds (across partitions or processes) serialize safely
rather than colliding on the shared DB. Default is incremental (`force=false`).

## Search

`HybridSearcher` (`search.py`) fuses three signals: FTS5 `bm25()` (title/body/tags column
weights, per-partition via `fts_weights`) ⊕ per-projection dense cosine (over the resident
matrix) ⊕ **Reciprocal Rank Fusion** (per-partition `rrf_k`). Metadata filters are **pushed
down** (filter-then-rank, so a tight filter still returns a full top-N), with optional recency
bias and an optional per-source diversity cap (`max_per_source`). `UnifiedIndex.search` /
`search_many` federate a query across partitions.

## Config + flags

```yaml
index:
  enabled: false                   # master kill-switch — whole index inert when false
  consumers:                       # per-consumer routing gates; a consumer routes here
    agent_docs: false              #   only when index.enabled AND its gate are true
  partitions:
    <name>:
      rrf_k: 20                    # per-partition fusion constant
      recency: false               # recency bias on/off
      coverage: active             # "active" (working set) | "all" (incl. archived/closed)
      fts_weights: null            # FTS5 bm25 (title, body, tags) column weights; null = default (3,1,2)
      max_per_source: null         # cap top-k hits per source document (diversity); null = uncapped
```

`coverage` selects how much of a source's history a partition indexes; query-time
`Query.filters` then narrow within it (so one corpus serves both retrospective and
live-work queries). See `HISTORY-PARTITION-COVERAGE` design notes.

`fts_weights` overrides the default title-leaning FTS column weights for a partition whose
`title` field is not its most relevant text — e.g. vault, where the "title" is a navigational
heading breadcrumb, so it ranks body content above it. `max_per_source` caps how many top-k
hits may share one source document so a chunk-heavy file can't flood results; it is
score-guarded, so a genuinely dominant document with no competitive alternative keeps its slots.

## Capabilities, crons, endpoints

- **`index_rebuild`** (`context/index-rebuild`) — incremental per-partition build; self-skips
  while any index build is running.
- **Five `index-<partition>-refresh` sidecar crons** keep the active partitions fresh at
  corpus-matched cadences (knowledge/chrome `*/15`, summary `*/30`, conversation hourly,
  vault every 6h); one job per partition by design.
- **Embedding-service endpoints** `/index/search`, `/index/search_many`, `/index/build`, with
  `index_search` / `index_search_many` clients (see `architecture/embedding-service`).
- **Status/dashboard seam** — registered as the `consolidated` index in `work_buddy/indexing/`
  (the index-agnostic status + bulk-build adapter).

## Relationship to the per-domain indexes

The partitions wrap the same sources the legacy indexes serve — `KnowledgePartition` over the
knowledge store, `IRSourcePartition` over the IR sources, `VaultChunkPartition` over the vault
chunker. Each consumer is re-pointed onto the consolidated partition behind its own flag and
validated (blind A/B) before the corresponding legacy index is retired. See
`architecture/vault-index`, `architecture/knowledge-system`, and `context/index-rebuild`.
