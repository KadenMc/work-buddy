---
name: WebSearch subsystem
kind: integration
description: Provider-neutral general web search + retrieval — Jina default with a keyless ddgs fallback, behind a calendar/email-style provider seam, plus trafilatura extraction, evidence-card shaping, and a broker-admitted LOCAL_FAST relevance classify. Standalone and events-agnostic.
entry_points:
- work_buddy.websearch
- work_buddy.websearch.provider
- work_buddy.websearch.router
- work_buddy.websearch.capabilities
- work_buddy.websearch.classify
tags:
- websearch
- search
- retrieval
- provider-seam
- jina
- ddgs
- evidence-cards
aliases:
- web search subsystem
- websearch
- programmatic web search
- search the web
- retrieval subsystem
dev_notes: '**Classify uses the native broker path, not a self-wrap.** `classify_evidence` passes `priority=Priority.BACKGROUND` to `LLMRunner.call`; the local backend acquires the `broker.slot` itself (the broker is on the LLM path — see `architecture/inference/broker`). Do NOT re-introduce a `get_broker().slot()` wrapper around the classify: it would hold a second slot on a phantom profile while the real inference contends on `openai_compat:<model>`, which is both redundant and ineffective for yielding to interactive work. Admission/timeout for the classify is tuned via the broker profile under `inference.profiles.<key>`, not a websearch-local config block. **`.env` is not auto-loaded in the MCP server process** — the Jina key is resolved via `work_buddy.secret_env.read_secret_env` (env, then a repo-root `.env` scan) so a key written by the Settings fixer (which runs in the dashboard process) is visible where `web_search` actually runs. **Backends are Jina + ddgs only by design** — Serper/Brave/Mojeek/Tavily/Exa/SearXNG are seam-ready (one `providers/<name>.py` + a factory branch + config) but deliberately not built; SearXNG-style self-hosted scrapers are a dead-end category (engine-blocking). **No-store by default** — `web_search` is ephemeral; only `router.search(cache=True)` (a reuse consumer) persists, and only structured `SearchHit`s, never raw page text.'
---

## Why it exists

work-buddy needs **general programmatic web search** for arbitrary agent/instance needs (research, lookups, any user-defined watcher), not one use case. It is built as a **standalone, events-agnostic library** so the later Events arc plugs in as a thin adapter rather than a change here. Its public functions are the frozen contract those adapters call.

## Architecture

```
agents / capabilities / (future) Events Processor
   │
   ▼
router.search()                  # routing (jina → ddgs), fallback, opt-in cache
   │  SearchProvider protocol
   ▼
providers/{jina, ddgs_meta, fake}    # one backend = one provider
   │
   ├─ extract.py   (trafilatura / r.jina.ai reader)  → FetchResult
   ├─ cards.py     (SearchHit → EvidenceCard)         → compact, cited
   └─ classify.py  (LOCAL_FAST, BACKGROUND broker slot) → ClassifyResult
```

## Provider seam

`work_buddy.websearch.provider.SearchProvider` is a `@runtime_checkable` Protocol; `get_search_provider(name)` is the factory (config `websearch.provider`/`routing`, `enabled: false` short-circuit via `WebSearchProviderDisabled`, lazy adapter import, a `fake` backend). This mirrors `work_buddy/{email,calendar}/provider.py` 1:1. Typed `WebSearchError(error_kind)` subclasses (`…Disabled/Unavailable/RateLimited/Timeout/BadKey`) let the router and capability wrappers `isinstance`-classify.

## Backends

- **Jina `s.jina.ai`** — the reliable default. Returns full-page Markdown inline (`SearchHit.raw_text`), so extraction short-circuits; `r.jina.ai` doubles as the reader. Needs a bearer key (`websearch.jina.api_key_env`, default `JINA_API_KEY`); a missing key raises `WebSearchBadKey` and the router falls through to ddgs.
- **ddgs** — the no-key `$0` fallback. Hardened: a `ThreadPoolExecutor` wall-clock timeout (ddgs has a documented sync-hang bug), request spacing (`min_interval_s`), and rate-limit backoff. Snippet-only, so full text comes from `extract.py`.
- **fake** — deterministic in-memory fixtures for tests / dry runs, no network.

## Routing, fallback, cache

`router.search()` resolves `websearch.routing` (default `[jina, ddgs]`) in order; the first backend returning non-empty hits wins. A backend that errors (bad key / rate limit / timeout) or returns empty is skipped. If at least one backend responds cleanly with no results, the result is a legitimate empty list; only when **every** backend errors does it raise `WebSearchUnavailable`. Caching is **opt-in, off by default** — `router.search(cache=True)` stores structured hits in the artifact-managed `websearch-cache` (short TTL, `JsonRecordsStorage + PerRecordTtl + Delete`); raw page text is never persisted.

## Classify

`classify_evidence(question, cards)` judges *retrieved evidence* (not the open web) at `ModelTier.LOCAL_FAST`, `Priority.BACKGROUND`, via `LLMRunner.call` — the local backend's own broker slot provides admission (see dev_notes). Returns a structured `ClassifyResult`; defaults to `relevant=False` on any error so a watcher never fires on an inconclusive judgment.

## Capabilities

- `web_search` — routed search; returns `{ok, count, provider, hits:[…]}`. Ephemeral (no persistence).
- `web_search_health` — the active backend (first usable in the routing order) and its readiness.
- `web_fetch` — fetch + extract clean text for a URL (Jina reader when keyed, else trafilatura).

These are agent-internal (invoked via `wb_run`); there is no `/wb-*` slash command.

## Config

```yaml
websearch:
  enabled: true
  routing: [jina, ddgs]
  jina:  { api_key_env: JINA_API_KEY }
  ddgs:  { backend: auto, timeout_s: 10, min_interval_s: 2, max_retries: 3 }
  cache: { ttl_hours: 12 }
```

## Health / Settings

A non-core `websearch` component (category `integration`, `custom` health source) shows in the Settings tab with an opt-out toggle. Its `integrations/websearch/jina-api-key` requirement (severity `recommended` — ddgs works keyless) offers a Configure form that writes `JINA_API_KEY` via the standard secret fixer. The health probe checks the active backend (ddgs is always probeable; Jina needs the key).

## Events-integration seam (not built here)

When the Events spine exists, a `web_evidence` Processor and a semantic-LLM Condition call `search` / `to_evidence_cards` / `classify_evidence` directly — thin adapters in the events layer, no change to this subsystem.
