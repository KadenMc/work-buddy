---
name: Resilience Framework
kind: system
description: Unified fault-mitigation foundation for guarded calls — propagating Deadline, outcome taxonomy, execution seam, composable strategy library, pipeline/registry, and the broker/Obsidian adapters.
summary: 'The unified fault-mitigation foundation: a guarded call returns an Outcome (classified by the outcome taxonomy), runs under a propagating Deadline, through a composable chain of strategies (Timeout/Retry/CircuitBreaker/Bulkhead/PriorityBulkhead/RateLimiter/Fallback) assembled by a pipeline builder. Supersedes the hand-rolled broker admission / @bridge_retry / DISABLED_CAPABILITIES protections with one model + shared telemetry. Fault mitigation only — durable execution / human-in-the-loop waits are out of scope.'
entry_points:
- work_buddy.resilience
- work_buddy.resilience.seam
- work_buddy.resilience.strategies
- work_buddy.resilience.pipeline
- work_buddy.inference.resilient_broker
- work_buddy.obsidian.resilient_bridge
tags:
- resilience
- fault-tolerance
- timeout
- retry
- circuit-breaker
- bulkhead
- rate-limiter
- deadline
- strategy
- pipeline
- telemetry
- outcome-taxonomy
- guarded-call
aliases:
- resilience framework
- guarded call
- resilience strategy
- resilience pipeline
- ResilienceStrategy
- circuit breaker
- bulkhead
- priority bulkhead
- outcome taxonomy
- guarded_call
parents:
- architecture
dev_notes: 'Strategies rely on the single-threaded asyncio event loop for state safety — no locks; mutations between awaits are atomic. Bulkhead / RateLimiter / PriorityBulkhead hold loop-bound state (Semaphore / Condition) — they assume a single stable event loop. CONSEQUENCE: do not reuse a stateful strategy instance across separate guarded_call_sync calls — that helper spins a fresh loop per call. The seam invariant: guarded_call never raises except for a declared passthrough exception. Retry must live at exactly one layer per failure domain (do not stack a framework Retry over @bridge_retry''s own retry). PriorityBulkheadStrategy is a faithful async port of broker._ProfileState — keep the strict-priority, non-preemptive, per-priority-queue semantics if you touch it. Design notes + AFK build decisions: .data/designs/resilience-framework/.'
---

The resilience framework (`work_buddy/resilience/`) is the unified foundation for fault mitigation across **guarded calls** — any call that can be slow or fail. It supersedes three independently hand-rolled protections (the inference broker's admission control, the Obsidian bridge's `@bridge_retry`, the capability registry's `DISABLED_CAPABILITIES`) with one model: standard patterns, one vocabulary, shared observability. Built following Polly v8 / resilience4j.

## Core concepts

- **Outcome / OutcomeKind** — a guarded call returns an `Outcome`: a value XOR an error, plus an `OutcomeKind` (the outcome taxonomy): `SUCCESS`, `TIMEOUT`, `REJECTED` (shed before execution), `TRANSIENT_FAILURE`, `TERMINAL_FAILURE`, `PARTIAL`. The kind carries `is_retryable` / `counts_toward_circuit_trip` so retry and circuit-breaker logic never re-inspect the underlying exception.
- **Deadline** — an absolute monotonic stop-time that propagates down a nested call tree; each layer clamps its timeout to the remaining budget and never extends a parent. `derive_attempt()` yields per-attempt sub-budgets.
- **ResilienceContext** — per-call state: operation key, the Deadline, a call identity (with a parent link), and a typed-key property bag. Threaded explicitly and also published in a `ContextVar`, so synchronous code in a worker thread (via `asyncio.to_thread`, which snapshots context) can read it.
- **The execution seam** — `guarded_call(operation_key, fn, ...)` runs `fn` through a chain of strategies, classifies the result, emits telemetry, and returns an `Outcome`. A guarded call never raises to signal a classified failure; two deliberate exceptions: a failure signalled by *return value* (`result_classifier`) and a declared *passthrough* exception — a control-flow signal re-raised untouched (e.g. `ObsidianPostWriteUncertain`, which the gateway's verify-then-decide path needs as a raised exception).
- **ResilienceStrategy** — the callback-wrapper protocol every primitive implements: `execute(nxt, ctx) -> Outcome`.

## Strategy library

Six composable strategies in `strategies.py`: `TimeoutStrategy`, `RetryStrategy` (exponential backoff + full jitter), `CircuitBreakerStrategy` (closed/open/half-open, consecutive-failure count), `BulkheadStrategy` (flat concurrency cap), `PriorityBulkheadStrategy` (priority-aware admission — INTERACTIVE/WORKFLOW/BACKGROUND; the native-async port of the inference broker's per-profile algorithm), `RateLimiterStrategy` (token bucket), `FallbackStrategy`.

## Composition

`ResiliencePipelineBuilder` assembles strategies (declaration order = outermost-first; canonical order: overall Timeout -> RateLimiter/Bulkhead -> Retry -> CircuitBreaker -> per-attempt Timeout). `ResiliencePipeline.execute` runs a call through them. `ResiliencePipelineRegistry` (`get_pipeline_registry()`) holds named, lazily-built pipelines. **One hard rule: retry at exactly one layer per failure domain.**

## Adapters and consumers

Existing systems *participate* without being rewritten at their call sites: `work_buddy/inference/resilient_broker.py` (`guarded_broker_call`) and `work_buddy/obsidian/resilient_bridge.py` (`guarded_bridge_call`, `build_obsidian_pipeline`) map broker / Obsidian errors onto the taxonomy, propagate the deadline, and emit unified `guard.*` telemetry. The `@bridge_retry` decorator (`work_buddy/obsidian/retry.py`) is itself a thin framework consumer — each decorated call runs a `RetryStrategy → _BridgeHealthGate → call` chain via `guarded_call_sync`, so decorated capabilities share the same foundation as ad-hoc adapter calls. There is one retry loop in the codebase for the Obsidian failure domain (the one-retry-layer rule, structurally).

The **MCP gateway** is a framework consumer too: every `wb_run` capability dispatch runs through `guarded_call` under a `wb_run:<capability>` operation key (`work_buddy/mcp_server/dispatch_resilience.py`). It composes an operation-derived `TimeoutStrategy` (capabilities declare a `timeout_seconds` scalar or a `(params) -> seconds` policy; bridge-dependent capabilities run unbounded) and, for `requires=["obsidian"]` capabilities, a shared `obsidian_bridge` `CircuitBreakerStrategy`. The gateway registers the process's telemetry listeners at startup (an `InMemoryMetrics` recorder plus a logging listener), so every guarded call — gateway dispatch and `@bridge_retry` alike — records metrics and logs a `guard.*` line.

## Telemetry

`guarded_call` emits `CallCompleted`; strategies emit `CircuitStateChanged` and `LoadShed`. Listeners registered via `register_listener` receive every event; `InMemoryMetrics` is the default in-process recorder.

## Scope boundary

The framework is **fault mitigation only**. Durable execution and human-in-the-loop waits (consent prompts, the conductor, the retry-queue's cross-restart durability) are a separate discipline — a slow human is not a fault — and must not be folded in.

## State

The framework, strategy library, pipeline/registry, both adapters, `@bridge_retry`'s live wiring, and the MCP gateway dispatch (timeout + telemetry + Obsidian circuit breaker) are built and verified (unit-tested plus a live integration smoke test against the real Obsidian bridge). The Obsidian bridge is now governed at runtime by the gateway's `obsidian_bridge` breaker rather than by a build-time `DISABLED_CAPABILITIES` flip; that set still hard-disables capabilities whose *genuinely-absent* dependencies (calendar, hindsight, thunderbird, ...) cannot appear within the session.

The remaining live migration is the broker's synchronous LM Studio call-sites (embedding provider + LLM backends) onto `guarded_broker_call` — gated on a sync→async conversion of the inference path.
