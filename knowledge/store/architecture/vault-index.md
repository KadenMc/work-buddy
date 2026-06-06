---
name: Vault Semantic Index
kind: system
description: Native chunk-level semantic search over configured Markdown roots ("vaults"), in work-buddy's own processes (not Obsidian's heap). Heading-aware chunker → SQLite chunk store with float16 vector blobs + FTS5 → hybrid lexical⊕dense (RRF) search served warm from the embedding service. Reached via the vault_search / vault_index / vault_config capabilities and a 5-minute incremental cron.
entry_points:
- work_buddy.vault_index
- work_buddy.indexing
tags:
- vault
- semantic-index
- search
- chunker
- fts5
- dense-retrieval
- hybrid
- rrf
- embeddings
- sqlite
- vault_search
- vault_index
- vault_config
aliases:
- vault index
- semantic indexer
- vault semantic search
- embeddings index
- chunk index
parents:
- architecture
dev_notes: |-
  ## Chunk identity + storage internals

  - **`doc_id` is a SHA-1 of the identity tuple** `(source_path, heading_path, dup_index,
    split_index)` — NOT the human-readable `Chunk.key` (a heading containing `#`/`(1)`/`:`
    could forge a key collision; the JSON-encoded tuple cannot). `chunk_key` is kept as a
    diagnostic column.
  - **Vectors are float16 blobs in `chunk_vectors`**, one row per chunk, `PRAGMA
    foreign_keys=ON` with `ON DELETE CASCADE` to `chunks` — deleting a chunk drops its vector.
    This is deliberately NOT the IR engine's sidecar-`.npz` mechanism: per-chunk blobs make an
    incremental write O(1) (one row), where a monolithic vector file forces a full load+rewrite.
  - **`chunks_fts` is an external-content FTS5 table** over `chunks(text, embed_input)`, kept in
    sync by AFTER INSERT/UPDATE/DELETE triggers plus `PRAGMA recursive_triggers=ON` (so an
    `INSERT OR REPLACE` fires the delete trigger and can't leave a stale lexical entry). The
    indexer's explicit delete-then-reinsert for changed files is the primary sync contract;
    recursive-triggers is the backstop. `_migrate` backfills the index once for a pre-existing
    `chunks` table (guarded by an `index_meta` flag).
  - A covering index `idx_chunks_vault_doc(vault_id, doc_id)` lets the per-vault status
    aggregates run index-only (no scan of the big text columns) — keeps the status panel
    sub-second at ~85k chunks.

  ## Resident matrix cache (`dense_cache.py`)

  Dense search needs an `(N×dim)` float32 matrix. It is lazy-loaded on first search,
  **version-invalidated** via the `index_meta` counter `build_version:vault` (a build bumps it;
  `get_matrix` compares and reloads on mismatch), and **released after an idle TTL** by a
  background evictor thread. Home is the long-lived embedding-service process, so the matrix
  stays warm across queries; an idle vault doesn't pin the ~hundreds-of-MB matrix.

  ## In-service host + broker priority

  `vault_search` / `vault_index build` POST to the embedding service's `/vault/search` and
  `/vault/index` endpoints (mirroring `ir_index → /ir/index`), so they run with
  `ir.dense._IN_SERVICE=True`: query encoding hits the loaded model directly (no HTTP
  self-call — `search.py` encodes via `ir.dense.encode_query`), and the bulk encode shares the
  one `LocalInferenceBroker` at BACKGROUND priority, yielding to interactive searches. The
  `python -m work_buddy.vault_index` CLI builds in its own process (own broker) — for manual/dev
  builds.

  ## Advisory lock + reconciliation safety

  `build_all` holds a per-DB advisory lock (`utils/index_lock.py`: O_EXCL lockfile, PID-liveness
  + stale-age reclaim, heartbeat per encode checkpoint); the 5-min cron's `is_locked` read-probe
  skips a run already in progress. Reconciliation is **per-vault and reachability-gated**: an
  unreachable vault (path absent/unreadable) is warned-once and its chunks are KEPT — pruning runs
  only inside the `reachable=True` branch, so an offline drive or a vault dropped from config never
  silently loses chunks. Per-file commits make a crash resumable (re-treat as new/changed; never
  falsely pruned).

  ## The index-agnostic seam (`work_buddy/indexing/`)

  A thin observability layer over all of work-buddy's indexes: an `Index` protocol
  (`status() -> IndexStatus` of `PartitionStatus`es; `lock_key`; `bulk_build`), a name→Index
  `registry`, `status.aggregate_status()`, and adapters for `ir` (one partition per source),
  `vault_index` (one partition per vault, sourced from `vault_index.status.effective_vault_configs`),
  and `knowledge` (counts-only). `aggregate_status` degrades a failing adapter to a
  `health="error"` partition so one bad index never blanks the panel. The dashboard
  `/api/embeddings` consumes it.

  ## Why this exists

  The prior approach delegated vault semantic search to the Smart Connections Obsidian plugin,
  whose index loaded fully into Obsidian's capped V8 heap and OOM'd the editor. This subsystem runs
  entirely outside Obsidian — disk-backed SQLite, a process-isolated lazy matrix — so search never
  competes with the editor's memory.
---
## Overview

Chunk-level semantic search over one or more configured Markdown roots ("vaults" — any indexed
directory, Obsidian or not). Runs in work-buddy's own processes: a heading-aware chunker, a
disk-backed SQLite store, and hybrid lexical⊕dense retrieval. The index DB is a **derived,
disposable cache** — the Markdown files are the source of truth, so a lost DB is rebuilt, never
recovered.

## Pipeline

```
FilesystemSource.discover()   multi-root walk; dot-dir skip; include/exclude globs; mtime staleness
  → MarkdownHandler (chunker)  heading-structural, code-fence aware; breadcrumb-prefixed embed_input;
                               max-size paragraph-first splitter; non-overlapping leaf sections
  → store (SQLite)             chunks + float16 vector blobs + FTS5 lexical index + indexed_items
  → embedding service          dense encode (leaf-ir document model), optional LM Studio peer offload
  → hybrid search              FTS5 bm25 ⊕ dense cosine, RRF-fused
```

A content-handler registry keys handlers by file extension (Markdown-only by default); an
unclaimed extension is skipped. `Chunk` is the stable contract between handlers and the rest.

## Search

`vault_index.search.search(query, *, top_k, method, vault_id, recency)` returns IR-compatible
result dicts (`doc_id, score, bm25_score, dense_score, source="vault_index", display_text,
metadata`). `method` ∈ `hybrid` (default) / `lexical` / `dense`. Lexical is SQLite FTS5
(`bm25()`, disk-backed — no in-RAM corpus); dense is cosine over the resident vector matrix;
the two are fused with Reciprocal Rank Fusion. **Graceful degradation:** if the embedding
service is unavailable the query encoder returns `None` and search falls back to lexical-only —
never an error.

## Capabilities + cron

- `vault_search` — hybrid search → markdown results (degrades to in-process lexical if the
  service is down).
- `vault_index` — `status` (per-vault counts, read locally so it works with the service down) /
  `build` (incremental reconcile + encode, skips if a build holds the lock).
- `vault_config` — add/update/remove a vault in `vault_index.vaults` (writes `config.local.yaml`).
- A sidecar cron (`sidecar_jobs/vault-index-rebuild.md`) calls `vault_index build` every 5 minutes;
  incremental, fast when nothing changed.

## Configuration (multi-vault)

`vault_index.vaults` is a mapping keyed by stable id: `{id: {path, include, exclude}}`. Empty (the
default) synthesizes a single vault from `vault_root`. The id is the stable identity (survives a
path change) and namespaces chunk ids. Editing config takes effect on the next build — no restart.

## Dashboard

Settings › **Embeddings** shows a read-only **System** table (IR + knowledge indexes) and an
editable **Your vaults** table (per-vault counts + a gear to edit path/globs, add/remove). Backed
by `/api/embeddings` over the index-agnostic seam.

## Key files

- `work_buddy/vault_index/{chunker,handlers,source,store,indexer,dense,dense_cache,search,status}.py`
- `work_buddy/vault_index/__main__.py` — manual/dev build CLI.
- `work_buddy/indexing/` — the index-agnostic status seam (IR / vault / knowledge adapters).
- `work_buddy/mcp_server/ops/vault_ops.py` — the three capability dispatchers.
- `work_buddy/embedding/service.py` — the in-service `/vault/search` + `/vault/index` host.

See `architecture/embedding-service` for the dense-encode backend and `architecture/inference/broker`
for the BACKGROUND-priority admission control the bulk encode rides on.
