---
name: Hot-Path Discipline
kind: concept
description: 'Why store reads and request-serving surfaces must stay cheap: the two root causes of multi-second dashboard stalls (once-only work repeated on the hot path; unbounded external/blocking calls) and the fix shapes (memoize-on-change-signal, per-process schema gate, connection-share/batch, stale-while-revalidate + prewarm, deadline-bound external calls).'
tags:
- performance
- hot-path
- caching
- memoization
- stale-while-revalidate
- prewarm
- n-plus-one
- sqlite
- dashboard
- store
aliases:
- hot path discipline
- store read performance
- dashboard latency
- memoize on mtime
- stale while revalidate
- prewarm cache
- n+1 store opens
- once-only work
parents:
- architecture
dev_notes: |-
  ## Diagnosis discipline

  - Profile to find the *dominant* cost; don't optimize by hypothesis. Decompose aggregates -- a store-open's wall time is dominated by the config parse, not the migration step, so attributing it to the migration runner sends the fix to the wrong place.
  - When a fix underperforms its predicted gain, re-profile rather than rationalize.
  - A per-surface sweep (one profiling pass per dashboard view) catches under-fixes that a spot-fix leaves behind -- several views shared one un-memoized config reader.

  ## Caching hazards

  - **Process-lifetime gates** assume the resource isn't deleted out-of-band -- safe for live DBs; a hazard only if a test deletes and recreates a file at the same path (tests use fresh tmp paths, so each migrates once).
  - **Test isolation:** module-level caches persist across tests; key on a per-test-distinct identity (path, repo_root) or weak-ref throwaways. In-memory / empty-path resources must not collide in a path-keyed cache.
  - **load->mutate->save:** `chat_collector._load_cache` returns a dict the caller mutates then saves; the memo returns a shallow copy so added keys don't poison it, and the save bumps the file mtime to invalidate the next read.
  - **Concurrency:** single-flight the background refresh (a `_refreshing` flag) so a stale key doesn't spawn many refreshers; last-writer-wins on a deterministic global is harmless.
---
Work that lands on a hot path -- every store connection open, or every request to a serving surface (dashboard handler, MCP capability, sidecar job) -- must be cheap. Two root causes account for the multi-second stalls this discipline prevents.

## The two root patterns

**1. Once-only work repeated on the hot path** -- work whose result can't change between calls, redone every time:
- Parsing config files on every DB open (`config.load_config` and `paths` data-root resolution; both are reached from a store's `_db_path` on every `get_connection`).
- Running schema-ensure / migrations on every `get_connection`.
- Re-aggregating a large dataset in Python on every request.
- Re-reading + parsing a whole cache file on every request.

Fix shape: **do it once, remember the result** -- memoize, gate, or cache, with a correct invalidation signal.

**2. Unbounded external/blocking calls on the hot path** -- calls into something slow and outside the process:
- Subprocess spawns (scheduled-task / `gh` / `tailscale` checks).
- Obsidian-bridge / network calls, which carry intermittent multi-second latency spikes.

Caching does not help the first (uncached) call, and an unbounded external dependency can stall a handler arbitrarily. Fix shape: **bound it with a deadline AND move it off the request path** (background refresh + serve last-known). See `architecture/resilience` for the deadline machinery.

## Fix shapes

- **Memoize on a change-signal.** Key an expensive pure result on the source's identity (file mtime, schema version). A hit then means *provably unchanged* -- no staleness window. Prefer this over a TTL whenever the source exposes a cheap change-signal.
- **Per-process "already did it" gate.** For idempotent-but-not-free setup (schema-ensure, migrations), run it once per resource per process via a module-level `set` keyed on the resource path.
- **Share one connection, or batch the query.** For an N+1 over a store, thread one connection through the loop (heterogeneous reads) or collapse it to a single `WHERE id IN (...)` (homogeneous reads).
- **Stale-while-revalidate + prewarm.** For expensive builds with no cheap change-signal (health/requirement sweeps, git-activity scans, system-state): serve the last snapshot immediately, refresh on a single-flight background thread, and pre-warm at startup. A plain TTL cache that *rebuilds synchronously on expiry* still stalls one request per cycle -- stale-while-revalidate does not.
- **Deadline-bound external calls** so a slow dependency can't stall the handler.

## Current applications

- Config-parse memo (mtime-keyed): `config.load_config`, `paths._load_paths_section`.
- Schema-ensure gates (per-path `_schema_ready` set): `get_connection` in `threads/store`, `conversation_observability/db`, `summarization/db`, and the Settings store. Settings keys readiness by resolved database path so each database migrates once per process without coupling separate profile or test paths.
- Connection-share / batch: `threads/render`, `projects/activity`, `obsidian/tasks/store.get_many`.
- Stale-while-revalidate + prewarm: `dashboard/api.get_system_state`, `control/graph.build_graph`, the `dashboard/service` requirements snapshot and its startup pre-warm thread; `projects/activity` git cache.
- Source-mtime cache: `llm/claude_code_usage/aggregator` (DB mtime), `collectors/chat_collector._load_cache` ((mtime, size)).

## Guardrails

- Don't reintroduce per-call config parsing or per-open schema work; don't revert `conn=` threading to per-row opens; don't put a synchronous bridge/subprocess/embedding call on a request path without a deadline and an off-path refresh.
- A point-fix is not a pattern-fix: when you fix one instance, grep for sibling call sites of the same shape.
- Memoize only pure values; if callers mutate the returned value, return a copy.
- Respect the dashboard smart-refresh contract (`architecture/event-bus`): faster endpoints and SSE push compound. The regression test `test_no_wholesale_loader_calls_in_event_handlers` enforces part of it.
- Add a regression test that proves the cache short-circuits -- a latency regression is invisible to ordinary correctness tests.

## See also

- `architecture/migrations` -- the migration runner; its audit hash is memoized because `run()` is on the store hot path.
- `architecture/event-bus` -- SSE push and the smart-refresh contract; the natural cache-invalidation channel.
- `architecture/resilience` -- deadlines/timeouts for bounding external calls.
