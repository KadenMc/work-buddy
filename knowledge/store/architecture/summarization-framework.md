---
name: Summarization Framework
kind: concept
description: 'Composition-based summarization — Source × Strategy × Store with a shared refresh orchestrator. Two compositions today: conversation sessions (layered disclosure → durable store) and Chrome tabs (flat extraction → TTL cache).'
summary: 'Protocol-based composition framework for content summarization. `Summarizer = Source × Strategy × Store` — three pluggable axes plus a shared core (refresh orchestrator, composer, construction-time coherence checks, provenance stamping). Stored result is always a tree (`SummaryNode`); flat extraction is depth-1, layered disclosure carries children with `source_ref` pointers. Two real compositions: conversation_session (layered, durable SQLite) and chrome_page (flat, TTL cache). New domains add a `Source` adapter, pick a strategy + store, register a binding factory.'
tags:
- allow-transient-labels
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
dev_notes: |-
  ### Adding a new Store

  Store implementations must guarantee `is_fresh` and `select_stale` use the same private staleness predicate — otherwise the orchestrator's "check fresh, then save with same token" cycle can race. `DurableSummaryStore._is_stale_row` is the canonical pattern.

  Declare the right capabilities. `PERSISTS_TREE` means "can save arbitrary-depth trees"; `PERSISTS_FLAT` means "depth-1 only." A store that can do both should declare both (the coherence check picks whichever the strategy needs). INCREMENTAL strategies require `apply_incremental` on the store; declaring the strategy capability without implementing the method raises `IncoherentComposition` at construction.

  ### Bridging strategy versions into the store

  The store evaluates staleness against the strategy's `prompt_version` / `schema_version`. To bridge them, implement a `set_strategy_versions(prompt_version, schema_version)` method on the store — `Summarizer.__post_init__` and every `refresh`/`refresh_one` call invoke it. Stores without this method use whatever defaults they carry.

  ### Incremental algorithm — finalization boundary

  `IncrementalLayeredStrategy.is_finalized(span_end, total_turns)` returns True when the live tail has moved at least `finalization_distance_turns` (default 10) past a topic's span_end. `_compute_finalized_count` walks prior topics in order and stops at the first non-finalized; once a topic is finalized, it never re-enters the LLM prompt — only its compressed title/summary/keywords carry through as context. The fresh-tail boundary is `max(span_end of ALL prior topics) + 1`, not just finalized ones — otherwise the trailing topic's covered turns would be re-fed.

  ### Pathway selection

  `_resolve_per_call_budget()` reads `conversation_observability.summaries.per_call_budget_tokens` (default 32k tracking frontier_fast). Pathway selector routes to chunked when the predicted fresh-tail input exceeds `pathway_threshold_ratio × budget` (default 0.85). The chunked path estimates tokens-per-turn from a 10-turn probe, picks a turn-count per chunk that fits the remaining budget after compressed-prior-topic context, and accumulates topics across chunks (each chunk sees the previous chunk's emissions as compressed context).

  ### Model chain wiring

  `default_llm_caller` reads `conversation_observability.summaries.model_chain` and resolves tier-name strings into `ModelTier` enums via `_resolve_model_chain`. First entry is the primary tier, rest are passed to `LLMRunner.call(escalate_to=...)`. `escalate_on` covers the standard transient errors (TIMEOUT, CONTEXT_EXCEEDED, RATE_LIMITED, MALFORMED_RESPONSE, BACKEND_UNAVAILABLE, EMPTY_CONTENT, MODEL_NOT_AVAILABLE); permanent errors (BAD_REQUEST, SCHEMA_VIOLATION) do NOT escalate. Unknown tier names are warned and skipped; empty resolved chain falls back to `[frontier_fast]`.

  ### Queue worker — cadence and bounds

  The worker (`summarization/worker.py`) is invoked by the `summarization-worker.md` sidecar job every 5 minutes (offset 3 minutes from `conversation-observability-refresh`). One tick drains up to `worker_tick_limit` (default 20) entries FIFO over the eligible (cooldown-passed) subset. Eligibility is enforced at dequeue time, not enqueue time, so an actively-changing session that re-enqueues during cooldown stays in the queue but doesn't pre-empt other work. The daily-budget circuit-breaker computes today's spend by summing `agents/*/llm_costs.jsonl` entries with `trace_id` prefix `summarization.`; when it trips, the worker returns `budget_paused: true` until the next day. `bypass_cooldown=True` and `bypass_budget=True` flags exist for explicit user-triggered refresh via the `summarization_worker_tick` MCP op.

  ### Provenance is core, not an axis

  Unlike the artifact system where Provenance is a pluggable axis (some artifacts session-tagged, some not), summarization provenance is uniform (model / backend / four version ints / timestamp). Adding a fourth axis here would be speculative — wait for a real consumer that needs it.

  ### Batch path

  The orchestrator dispatches to `_run_refresh_batch` when `BATCHED` is in the composed capabilities. Source provides `render_batch`; the orchestrator labels each item (`## Item N: <id>`) and concatenates into one user prompt; strategy provides `parse_batch(structured_output, raw, item_ids) -> [SummaryNode | None]`. Items missing from the response are recorded as errors but the rest are saved.

  ### TtlCacheStore wraps work_buddy.llm.cache

  The TTL store does not invent caching — it wraps the existing `llm/cache.py` (`get` / `put`). Freshness tokens are `{"hash": <sha256>, "text": <content>}`; the `text` field enables `llm.cache`'s SimHash fuzzy fallback. The `strategy_version_tag` derives the `system_hash` so a strategy prompt-version bump invalidates the cache.

  ### IR source / disclosure id mapping

  The IR `summary` source uses `doc_id = "{namespace}:{item_id}:n{ordinal}"`. The `drill_tree` `summary` domain uses `node_id = "{namespace}:{item_id}#n{ordinal}"` (or the bare `{namespace}:{item_id}` for the root). The `:n` vs `#n` difference is intentional: doc_ids use the same separator throughout (`:` runs uniformly), but node_ids need an unambiguous boundary between the item id (which may itself contain colons, e.g. session UUIDs don't, but generic item_ids might) and the ordinal. Consumers translate one to the other by replacing the last `:n` with `#n`.
---

A protocol-based composition framework for content summarization, modeled on the artifact system. `Summarizer = Source × Strategy × Store` — three pluggable axes plus a written-once shared core (refresh orchestrator, composer, construction-time coherence checks, provenance stamping).

## The three axes

- **`Source`** — domain adapter. `discover(window) -> [(item_id, freshness_token)]`, `render(item_id) -> prompt text`. Per domain (sessions, web pages, event streams). May declare `BATCHED` and implement `render_batch`. Sources for INCREMENTAL strategies additionally provide `total_turns(item_id)`, `render_from(item_id, from_turn)`, and `render_range(item_id, from_turn, to_turn)` so the algorithm can slice fresh-tail input without re-feeding finalized history.
- **`SummaryStrategy`** — output-shape adapter. Owns system prompt + output JSON schema + `parse(structured_output, raw) -> SummaryNode`. Per output shape (layered disclosure, flat extraction, incremental layered).
- **`Store`** — persistence + staleness adapter. `is_fresh`, `select_stale`, `save`, `load`, `record_error`. INCREMENTAL strategies additionally require `apply_incremental` for the merge step. Two implementations: `DurableSummaryStore` (SQLite, version-stamped, tree-shaped) and `TtlCacheStore` (wraps `work_buddy.llm.cache`).

Provenance (model / backend / version / timestamp stamping) is uniform and baked into the core — not a pluggable axis. The framework's `Store` is responsible for caching; `LLMRunner` is called without `cache_ttl_minutes` to avoid double-caching.

## Tree-shaped record invariant

Every stored summary is a `SummaryNode` tree: `{summary, source_ref?, children: [], extra}`. Flat extraction = depth-1 (root only, empty `children`, null `source_ref`). Layered disclosure = root + children, each child carrying a `source_ref` pointer to exact source events. Incremental layered = same shape; each refresh preserves finalized children verbatim and rewrites only the trailing-onward region. The durable store schema persists arbitrary-depth trees with a `source_ref` slot on every node.

## Composer + coherence

`Summarizer(source=, strategy=, store=)` validates coherence at construction. Today's checks: `LAYERED` strategy requires `PERSISTS_TREE` store; `FLAT` strategy requires `PERSISTS_FLAT` or `PERSISTS_TREE`; `BATCHED` must be declared on both source and strategy or neither; `INCREMENTAL` requires the source to provide `render_from` / `total_turns` (duck-typed) and the store to provide `apply_incremental`. Violations raise `IncoherentComposition`.

At each `refresh` / `refresh_one` the composer re-bridges the strategy's `prompt_version` / `schema_version` into the store via `set_strategy_versions`, so a version bump invalidates stored rows on the next staleness check.

## Current compositions

| Composition | Source | Strategy | Store | Used by |
|---|---|---|---|---|
| `conversation_session` | `SessionSource` | `LayeredDisclosureStrategy` (v1; legacy callers) OR `IncrementalLayeredStrategy` (v2; queue worker) | `DurableSummaryStore(namespace="conversation_session")` — selection_version=1 for v1, 2 for v2 | dashboard `/api/chats/<id>/topics`, `/wb-session-identify`, `claude_session_summary` collector, `session_summary_get` MCP capability, the `summarization-worker` sidecar job |
| `chrome_page` | `ChromeSource` (per-call) | `FlatExtractionStrategy` (BATCHED) | `TtlCacheStore(key_prefix="summarize_tab", ttl=30m)` | `chrome_infer._summarize_tabs`, `pipelines/chrome.py` |

The lazy singleton `get_session_summarizer()` always builds v1; the worker explicitly constructs v2 via `build_session_summarizer(use_incremental=True)`. The split keeps legacy v1-shape callers (tests, query helpers) on v1 strategy without flipping under them when the production flag changes.

## Consumers / search surface

Summaries are made searchable and drillable by two separate but coordinated layers:

- **IR `summary` source** (`work_buddy/ir/sources/summary.py`) — emits one Document per `SummaryNode` row. BM25 fields (`title` 1.75x, `summary` 1.0x, `keywords` 2.0x) plus a combined dense_text. Rebuilt by the `summary-index-rebuild` sidecar job every 5 minutes; built ad-hoc via `ir_index(source="summary")`.
- **`summary_search`** capability (`work_buddy/summarization/funnel.py`) — the coarse-to-fine retrieval funnel. Stage 1 ranks summary nodes; stage 2 drills via `session_search` (or any registered per-namespace drill handler). See `summarization/summary_search`.
- **`drill_tree`** capability (`work_buddy/disclosure/`) — the unified navigation contract. `domain="summary"` walks the per-node tree at three depths (index / summary / full). See `disclosure/`.

A new summarizable domain becomes searchable + drillable as soon as it ships a composition: no per-domain IR source or navigator to write.

## Adding a new composition

1. Implement a `Source` for the domain (`discover` + `render`, optionally `render_batch` if `BATCHED`, optionally `render_from`/`total_turns` if `INCREMENTAL`).
2. Pick (or implement) a `SummaryStrategy` — flat, layered, or incremental layered.
3. Pick a `Store` — durable for version-stamped, TTL for cache-style.
4. Build a binding factory `build_<name>_summarizer() -> Summarizer` in the consumer package.
5. Optional: surface read/write shims in the consumer package if existing call-sites depend on a legacy API.

Unit-test the composition by injecting a stub LLM via `as_caller(stub_fn)` — the framework normalizes legacy bare-dict-returning stubs.

## Key files

- `work_buddy/summarization/protocol.py` — `SummaryNode`, `Source`/`SummaryStrategy`/`Store` Protocols, `Provenance`, `SummaryCapability` (LAYERED, FLAT, BATCHED, INCREMENTAL, PERSISTS_TREE, PERSISTS_FLAT, VERSION_STAMPED, TTL_EVICTED), `LLMCaller`, exceptions.
- `work_buddy/summarization/summarizer.py` — `Summarizer` composer + `RefreshReport`.
- `work_buddy/summarization/orchestrator.py` — `run_refresh` (per-item + batch + incremental paths), `as_caller`, `default_llm_caller` (config-driven model chain), provenance assembly.
- `work_buddy/summarization/strategies.py` — `LayeredDisclosureStrategy`, `IncrementalLayeredStrategy`, `FlatExtractionStrategy`.
- `work_buddy/summarization/stores.py` — `DurableSummaryStore` (with `apply_incremental`), `TtlCacheStore`.
- `work_buddy/summarization/incremental.py` — `refresh_one_incremental` + `build_incremental_prompt` + pathway selection (single-call vs chunked) + per-tier budget resolution.
- `work_buddy/summarization/queue.py` + `worker.py` — SQLite-backed queue + worker tick with cooldown + daily-budget circuit-breaker.
- `work_buddy/summarization/funnel.py` — `summary_search` coarse-to-fine funnel + default drill handler.
- `work_buddy/summarization/db.py` + `schema.py` — durable SQLite (WAL, idempotent schema; tree-shaped `summary_items` + `summary_nodes` tables + `summarization_queue`).
- `work_buddy/conversation_observability/summarizer_binding.py` — `SessionSource`, `build_session_summarizer`.
- `work_buddy/collectors/chrome_summarizer_binding.py` — `ChromeSource`, `build_chrome_summarizer`, `summarize_tabs` (the public Chrome entry).
- `work_buddy/ir/sources/summary.py` — IR adapter for the per-node summary store.
- `work_buddy/disclosure/summary_tree.py` — `SummaryTreeDrillable` for the unified `drill_tree` capability.

Tests: `tests/unit/test_summarization_framework.py`, `tests/unit/test_summarization_store.py`, `tests/unit/test_incremental_strategy.py`, `tests/unit/test_summarization_queue.py`, `tests/unit/test_chrome_summarization.py`, `tests/unit/test_conversation_observability_summaries.py`, `tests/unit/test_ir_summary_source.py`, `tests/unit/test_summarization_funnel.py`, `tests/unit/test_disclosure.py`.
