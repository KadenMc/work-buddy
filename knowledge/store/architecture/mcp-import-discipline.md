---
name: MCP Server Import Discipline
kind: concept
description: 'Critical safety constraint: why heavy library imports in capability callables deadlock the MCP server, and the correct pattern to avoid it'
summary: The MCP server process must never import heavy compute libraries (numpy, rank_bm25, sentence-transformers, sqlite3) in capability callables. They deadlock the asyncio event loop via Python's import lock. Route heavy compute through the embedding service HTTP API (localhost:5124).
tags:
- architecture
- mcp
- import
- deadlock
- asyncio
- embedding-service
- safety
aliases:
- import deadlock
- asyncio deadlock
- heavy imports
- embedding service
- _IN_SERVICE flag
- import lock
parents:
- architecture
- architecture
---

## Rule

The MCP server process must **never** import heavy compute libraries in capability callables. This includes `numpy`, `rank_bm25`, `sentence-transformers`, and `sqlite3` (via `ir.store`).

All heavy compute goes through the embedding service HTTP API (`localhost:5124`).

## Why: the deadlock mechanism

The MCP server uses `asyncio.to_thread()` to dispatch capability callables to a thread pool. If a callable does a deferred import of a heavy module (e.g., `from work_buddy.ir.engine import search`), the import triggers Python's per-module import lock. The main thread (running the asyncio event loop) may also need import locks for its own operations. Result: **permanent deadlock**.

### Step-by-step

```
1. Claude calls wb_run("context_search", ...)
2. Gateway submits callable to thread pool via asyncio.to_thread()
3. Worker thread starts executing the callable
4. Callable hits: from work_buddy.ir.engine import search
5. This triggers loading numpy, rank_bm25, sqlite3 — heavy C extensions
6. Python import system acquires per-module locks for each module in the chain
7. Main thread's event loop needs one of those locks (for internal lazy imports)
8. DEADLOCK: worker holds locks, waits for event loop; event loop waits for worker
```

This was discovered and fixed on April 6, 2026. The original symptom was `context_search` hanging for 30+ seconds on first request — debug checkpoints confirmed execution reached the function body but never completed the `ir.engine` import.

## The correct pattern

All heavy compute runs in the **embedding service** (`work_buddy/embedding/service.py`), which runs in its own process and already imports numpy/rank_bm25/sentence-transformers:

- **`/ir/search`** endpoint — runs BM25 scoring, dense retrieval, and RRF fusion
- **`/ir/index`** endpoint — builds/checks the search index

The MCP server's `_ir_search_dispatch` and `_ir_index_dispatch` call `client.ir_search()` and `client.ir_index()` — lightweight HTTP requests via `urllib`, no heavy imports.

## The `_IN_SERVICE` flag

The `_IN_SERVICE` flag in `ir/dense.py` lets the embedding service call models directly (avoiding HTTP self-calls) while external callers still use the HTTP API.

## Safe vs unsafe imports in capability callables

| Safe | Unsafe |
|------|--------|
| `urllib`, `json`, `pathlib` | `numpy`, `rank_bm25` |
| `work_buddy.config`, `work_buddy.paths` | `work_buddy.ir.store`, `work_buddy.ir.engine` |
| HTTP calls to embedding service | `sqlite3` (loaded by ir.store) |
| `work_buddy.obsidian.bridge` | `sentence_transformers` |

## Key files

- `work_buddy/mcp_server/registry.py` — capability registration (deadlock warnings in `_build_registry()` and `_context_capabilities()`)
- `work_buddy/embedding/service.py` — the correct home for heavy compute
- `work_buddy/ir/dense.py` — `_IN_SERVICE` flag
- `work_buddy/mcp_server/context_wrappers.py` — gateway-callable wrappers following the correct pattern

---

## Sibling rule: don't block the event loop

The deadlock story above is about heavy imports inside threaded callables. There is a closely related failure mode that does *not* deadlock but is just as bad: **synchronous blocking calls inside an `async def` tool handler in `work_buddy/mcp_server/tools/gateway.py`**. FastMCP runs the event loop single-threaded, so any sync call that takes more than a couple of seconds freezes every other request — including the 2-second `/health` poll from the sidecar supervisor. When that poll times out, the supervisor marks `mcp_gateway` unhealthy and restarts it, which drops every Claude Code SSE stream.

This was discovered on 2026-04-18: `wb_search` called `registry.search_registry(...)` directly instead of through `asyncio.to_thread`. The first call after a cold gateway boot materialized the registry (~19s of tool probes + knowledge index warm + Obsidian HTTP probe with a 10s timeout) on the event loop, blocked `/health`, and triggered a cascading auto-restart 2–3 minutes after every gateway start.

### Rule

Any `async def` tool handler registered on FastMCP must `await asyncio.to_thread(...)` around any call that touches:

- the registry (`registry.search_registry`, `registry.get_entry`, anything that transitively calls `get_registry()`)
- the knowledge store
- the filesystem beyond trivial reads
- an HTTP probe or another service over HTTP

If you want to read arguments and route, that's fine inline. If you want to *do work*, go through `to_thread`.

### Diagnostic recipe

To verify a new handler doesn't block the event loop:

1. Rebuild the registry on a running gateway: `mcp__work-buddy__wb_run("reload_capability_data")`
2. In another terminal, hammer `/health` at ~20 Hz: `while true; do curl -sm1 -o/dev/null -w "%{time_total}\n" http://localhost:5126/health; done`
3. Call your new tool once.
4. `/health` latency must stay under ~100ms throughout. A spike into seconds means you're blocking the event loop.

### Defenses already in place

- **Background warm-start**: `main_http()` in `mcp_server/server.py` fires a daemon thread that calls `get_registry()` immediately after bind. By the time the first real request lands, the registry is already built. This hides latency — but it does *not* excuse skipping `asyncio.to_thread` on new handlers, because `reload_capability_data` can rebuild at any time.
- **Slow-rebuild warning**: `_build_registry()` emits a `WARNING` to the main log when total rebuild exceeds 5s, with a per-section breakdown (`tool_probes`, `cap:*`, `knowledge_index`). Check the sidecar log if `/health` flakes — a noisy section there usually points at the culprit (often the Obsidian probe's 10s HTTP timeout when Obsidian is closed).
- **Fast-path socket check**: `compat._find_pids_on_port` shortcuts to a socket probe before shelling out to PowerShell (`Get-NetTCPConnection`). Pinned by a regression test in `tests/unit/test_compat_port_cleanup.py` — sidecar restart is ~15s on Windows, not 25–30s.
