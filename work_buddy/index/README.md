# `work_buddy/index/` — consolidated index (flag-gated, INERT by default)

One SQLite + FTS5 + float16-blob-vector store across **partitions** (knowledge, vault
chunks, conversation, projects, chrome, summary, task_note), served warm in-service and
scheduled by the inference broker. Designed to subsume `knowledge/index.py`,
`vault_index/`, and `ir/`.

**Status: inert.** Nothing on the live hot path uses this. `index.enabled` defaults
`false`; the live knowledge/vault/IR indexes remain the system of record. This package
builds into a **separate** DB (`db/index-consolidated`) and is exercised only by the A/B
harness. Activating it (flipping the flag + re-pointing `agent_docs`/`search`) and
retiring the bespoke indexes are **deliberate, reviewed, post-A/B steps** — see
`.data/designs/index-consolidation/{DESIGN.md, CLASS-ARCHITECTURE.md, AFK-DECISIONS.md}`.

## Layers (all composition + Protocols; inheritance-free)

| Module | Role |
|---|---|
| `model.py` | `Document`, `Projection`, `ProjectionSpec`, `Hit`, `Query`, enums, `content_hash` |
| `config.py` | `IndexConfig`/`PartitionConfig` (the `index.enabled` flag; per-partition `rrf_k`) |
| `fusion.py` | `rrf_fuse` (parity-lift of `ir.store.rrf_fuse`) |
| `recency.py` | epoch-based recency bias over `Hit`s |
| `resident.py` | generic `ResidentCache[T]` (injected loader) + registry + one evictor |
| `store.py` | `IndexStore` — documents + standalone FTS5(title/body/tags) + `(doc_id,projection,sub)` blob vectors |
| `encode.py` | `EmbeddingProvider` seam (`LocalProvider`/`LmStudioProvider` + `ProviderRouter`) + `BrokeredEncoder` + `score_dense` |
| `search.py` | `HybridSearcher` (FTS5 ⊕ dense ⊕ RRF ⊕ recency ⊕ filters) + `MultiQueryFuser` |
| `build.py` | `IndexBuilder` (incremental, content-hash diff, advisory-locked, batched BACKGROUND encode) |
| `partition.py` | `Partition` Protocol + lazy `PartitionRegistry` |
| `partitions/` | IR-source wrapper + bootstrap (the only domain-importing wiring) |
| `partitioned.py` | `IndexPartition` + `UnifiedIndex` facade (federates via RRF) |
| `ab.py` | A/B harness vs the live knowledge index |

Partition adapters live with their domains: `knowledge/partition.py`,
`vault_index/partition.py`; IR sources via `index/partitions/ir_source.py`. The engine
core imports **no** domain — partitions register into it (`domain → index`).

## How to enable (for testing, after a service restart)

1. Set `index.enabled: true` in `config.local.yaml` (per-partition `rrf_k` optional).
2. Restart the embedding service so `/index/search` + `/index/build` load.
3. Build:  `POST /index/build {"partition": "knowledge"}`  (or in-process
   `UnifiedIndex(config=load_index_config()).build("knowledge")`).
4. Query:  `POST /index/search {"query": "...", "partitions": ["knowledge"]}`.
5. A/B vs the live index:  `python -m work_buddy.index.ab`.

The consolidated index is also visible in the dashboard's index panel as the
`consolidated` index (its `bulk_build` is a no-op while the flag is off).
