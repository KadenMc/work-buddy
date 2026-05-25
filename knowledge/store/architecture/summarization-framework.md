---
name: Summarization Framework
kind: concept
description: 'Composition-based summarization — Source × Strategy × Store with a shared refresh orchestrator. Two compositions today: conversation sessions (layered disclosure → durable store) and Chrome tabs (flat extraction → TTL cache).'
summary: 'Protocol-based composition framework for content summarization. `Summarizer = Source × Strategy × Store` — three pluggable axes plus a shared core (refresh orchestrator, composer, construction-time coherence checks, provenance stamping). Stored result is always a tree (`SummaryNode`); flat extraction is depth-1, layered disclosure carries children with `source_ref` pointers. Two real compositions: conversation_session (layered, durable SQLite) and chrome_page (flat, TTL cache). New domains add a `Source` adapter, pick a strategy + store, register a binding factory.'
tags:
- summarization
- composition
- llm
- framework
- progressive-disclosure
aliases:
- summarization system
- summary framework
- Source Strategy Store
- summarizer composer
parents:
- architecture
---

A protocol-based composition framework for content summarization, modeled on the artifact system. `Summarizer = Source × Strategy × Store` — three pluggable axes plus a written-once shared core (refresh orchestrator, composer, construction-time coherence checks, provenance stamping).

## The three axes

- **`Source`** — domain adapter. `discover(window) -> [(item_id, freshness_token)]`, `render(item_id) -> prompt text`. Per domain (sessions, web pages, event streams). May declare `BATCHED` and implement `render_batch`.
- **`SummaryStrategy`** — output-shape adapter. Owns system prompt + output JSON schema + `parse(structured_output, raw) -> SummaryNode`. Per output shape (layered disclosure, flat extraction).
- **`Store`** — persistence + staleness adapter. `is_fresh`, `select_stale`, `save`, `load`, `record_error`. Two implementations: `DurableSummaryStore` (SQLite, version-stamped) and `TtlCacheStore` (wraps `work_buddy.llm.cache`).

Provenance (model / backend / version / timestamp stamping) is uniform and baked into the core — not a pluggable axis. The framework's `Store` is responsible for caching; `LLMRunner` is called without `cache_ttl_minutes` to avoid double-caching.

## Tree-shaped record invariant

Every stored summary is a `SummaryNode` tree: `{summary, source_ref?, children: [], extra}`. Flat extraction = depth-1 (root only, empty `children`, null `source_ref`). Layered disclosure = root + children, each child carrying a `source_ref` pointer to exact source events. The durable store schema persists arbitrary-depth trees with a `source_ref` slot on every node — the structural foundation that the progressive-disclosure layer (IR indexing of summaries + `drill_tree` navigation) consumes.

## Composer + coherence

`Summarizer(source=, strategy=, store=)` validates coherence at construction. Today's checks: `LAYERED` strategy requires `PERSISTS_TREE` store; `FLAT` strategy requires `PERSISTS_FLAT` or `PERSISTS_TREE`; `BATCHED` must be declared on both source and strategy or neither. Violations raise `IncoherentComposition`.

At each `refresh` / `refresh_one` the composer re-bridges the strategy's `prompt_version` / `schema_version` into the store via `set_strategy_versions`, so a version bump invalidates stored rows on the next staleness check.

## Current compositions

| Composition | Source | Strategy | Store | Used by |
|---|---|---|---|---|
| `conversation_session` | `SessionSource` | `LayeredDisclosureStrategy` | `DurableSummaryStore(namespace="conversation_session")` | dashboard `/api/chats/<id>/topics`, `/wb-session-identify`, `claude_session_summary` collector, the `conversation_observability_summarize` MCP capability, the sidecar `conversation-observability-summarize` job |
| `chrome_page` | `ChromeSource` (per-call) | `FlatExtractionStrategy` (BATCHED) | `TtlCacheStore(key_prefix="summarize_tab", ttl=30m)` | `chrome_infer._summarize_tabs`, `pipelines/chrome.py` |

## Consumers / search surface

Summaries are made searchable and drillable by two separate but coordinated layers:

- **IR `summary` source** (`work_buddy/ir/sources/summary.py`) — emits one Document per `SummaryNode` row. BM25 fields (`title` 1.75x, `summary` 1.0x, `keywords` 2.0x) plus a combined dense_text. Rebuilt by the `summary-index-rebuild` sidecar job every 5 minutes; built ad-hoc via `ir_index(source="summary")`.
- **`summary_search`** capability (`work_buddy/summarization/funnel.py`) — the coarse-to-fine retrieval funnel. Stage 1 ranks summary nodes; stage 2 drills via `session_search` (or any registered per-namespace drill handler). See `summarization/summary_search`.
- **`drill_tree`** capability (`work_buddy/disclosure/`) — the unified navigation contract. `domain="summary"` walks the per-node tree at three depths (index / summary / full). See `disclosure/`.

A new summarizable domain becomes searchable + drillable as soon as it ships a composition: no per-domain IR source or navigator to write.

## Adding a new composition

1. Implement a `Source` for the domain (`discover` + `render`, optionally `render_batch` if `BATCHED`).
2. Pick (or implement) a `SummaryStrategy` — flat or layered.
3. Pick a `Store` — durable for version-stamped, TTL for cache-style.
4. Build a binding factory `build_<name>_summarizer() -> Summarizer` in the consumer package.
5. Optional: surface read/write shims in the consumer package if existing call-sites depend on a legacy API.

Unit-test the composition by injecting a stub LLM via `as_caller(stub_fn)` — the framework normalizes legacy bare-dict-returning stubs.

## Key files

- `work_buddy/summarization/protocol.py` — `SummaryNode`, `Source`/`SummaryStrategy`/`Store` Protocols, `Provenance`, `SummaryCapability`, `LLMCaller`, exceptions.
- `work_buddy/summarization/summarizer.py` — `Summarizer` composer + `RefreshReport`.
- `work_buddy/summarization/orchestrator.py` — `run_refresh` (per-item + batch paths), `as_caller`, `default_llm_caller`, provenance assembly.
- `work_buddy/summarization/strategies.py` — `LayeredDisclosureStrategy`, `FlatExtractionStrategy`.
- `work_buddy/summarization/stores.py` — `DurableSummaryStore`, `TtlCacheStore`.
- `work_buddy/summarization/funnel.py` — `summary_search` coarse-to-fine funnel + default drill handler.
- `work_buddy/summarization/db.py` + `schema.py` — durable SQLite (WAL, idempotent schema; tree-shaped `summary_items` + `summary_nodes` tables).
- `work_buddy/conversation_observability/summarizer_binding.py` — `SessionSource`, `build_session_summarizer`.
- `work_buddy/collectors/chrome_summarizer_binding.py` — `ChromeSource`, `build_chrome_summarizer`, `summarize_tabs` (the public Chrome entry).
- `work_buddy/ir/sources/summary.py` — IR adapter for the per-node summary store.
- `work_buddy/disclosure/summary_tree.py` — `SummaryTreeDrillable` for the unified `drill_tree` capability.

Tests: `tests/unit/test_summarization_framework.py`, `tests/unit/test_summarization_store.py`, `tests/unit/test_chrome_summarization.py`, `tests/unit/test_conversation_observability_summaries.py`, `tests/unit/test_ir_summary_source.py`, `tests/unit/test_summarization_funnel.py`, `tests/unit/test_disclosure.py`.

## Dev notes

### Adding a new Store

Store implementations must guarantee `is_fresh` and `select_stale` use the same private staleness predicate — otherwise the orchestrator's "check fresh, then save with same token" cycle can race. `DurableSummaryStore._is_stale_row` is the canonical pattern.

Declare the right capabilities. `PERSISTS_TREE` means "can save arbitrary-depth trees"; `PERSISTS_FLAT` means "depth-1 only." A store that can do both should declare both (the coherence check picks whichever the strategy needs).

### Bridging strategy versions into the store

The store evaluates staleness against the strategy's `prompt_version` / `schema_version`. To bridge them, implement a `set_strategy_versions(prompt_version, schema_version)` method on the store — `Summarizer.__post_init__` and every `refresh`/`refresh_one` call invoke it. Stores without this method use whatever defaults they carry.

### Provenance is core, not an axis

Unlike the artifact system where Provenance is a pluggable axis (some artifacts session-tagged, some not), summarization provenance is uniform (model / backend / four version ints / timestamp). Adding a fourth axis here would be speculative — wait for a real consumer that needs it.

### Batch path

The orchestrator dispatches to `_run_refresh_batch` when `BATCHED` is in the composed capabilities. Source provides `render_batch`; the orchestrator labels each item (`## Item N: <id>`) and concatenates into one user prompt; strategy provides `parse_batch(structured_output, raw, item_ids) -> [SummaryNode | None]`. Items missing from the response are recorded as errors but the rest are saved.

### TtlCacheStore wraps work_buddy.llm.cache

The TTL store does not invent caching — it wraps the existing `llm/cache.py` (`get` / `put`). Freshness tokens are `{"hash": <sha256>, "text": <content>}`; the `text` field enables `llm.cache`'s SimHash fuzzy fallback. The `strategy_version_tag` derives the `system_hash` so a strategy prompt-version bump invalidates the cache.

### Conv_obs legacy tables

`session_summaries` and `topic_summaries` in `conversation_observability/schema.py` remain defined but are no longer written — they predate the framework and are retained as a zero-effort rollback path. Removing them is a separate, follow-up change after the new store has proven out.

### IR source / disclosure id mapping

The IR `summary` source uses `doc_id = "{namespace}:{item_id}:n{ordinal}"`. The `drill_tree` `summary` domain uses `node_id = "{namespace}:{item_id}#n{ordinal}"` (or the bare `{namespace}:{item_id}` for the root). The `:n` vs `#n` difference is intentional: doc_ids use the same separator throughout (`:` runs uniformly), but node_ids need an unambiguous boundary between the item id (which may itself contain colons, e.g. session UUIDs don't, but generic item_ids might) and the ordinal. Consumers translate one to the other by replacing the last `:n` with `#n`.
